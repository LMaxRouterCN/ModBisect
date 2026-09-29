# SPDX-License-Identifier: MPL-2.0
"""UI 主窗口: 信号枢纽与状态机调度(全程序唯一有"时间"概念的一层)。

设计要点:
- UI 线程零阻塞: 扫描/启停/还原/会话恢复全部走 _Worker
  (threading 执行 + Qt 信号回投, 跨线程 emit 自动 queued 到 UI 线程)
- 两层状态机: UiState(外壳调度)与 engine.Phase(纯计算)并存,
  本层只调度不计算, 引擎只计算不调度 —— 计算与调度彻底解耦
- watcher / processmon 的回调线程只发信号, 所有状态推进都在 UI 线程
- 会话持久化: 引擎每次状态变化(apply 完成/答案归算)后 save_session
- 时序边角: apply 期间用户提前启动游戏 → 同样跟踪(不倒回等待提示);
  crash 事件晚于游戏退出到达 → JUDGING 状态仍计入本轮崩溃标志
"""

from __future__ import annotations

import threading
from enum import Enum

from PySide6.QtGui import QColor
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..config import AppConfig
from ..depgraph import DependencyGraph
from ..engine import Action, BisectEngine
from ..executor import ApplyReport, Executor
from ..processmon import ProcessMonitor
from ..scanner import ScanResult, scan_mods_dir
from ..session import RestoreResult, list_sessions, restore_session, save_session
from ..watcher import Watcher
from .dialogs import JudgeDialog, VerdictDialog


class UiState(Enum):
    """UI 外壳状态(与 engine.Phase 分属两层)。"""
    IDLE = "idle"                 # 未扫描
    SCANNING = "scanning"         # 扫描/恢复进行中
    READY = "ready"               # 已装配, 可开始或继续
    APPLYING = "applying"         # 正在把计划 diff 到磁盘(或还原中)
    WAIT_LAUNCH = "wait_launch"   # 已提示, 等用户启动游戏
    WAIT_GAME = "wait_game"       # 游戏运行中(procmon 跟踪)
    JUDGING = "judging"           # 判决弹窗打开中
    DONE = "done"                 # 终局报告已展示


# 测试窗口期(崩溃信号只有在这些状态下才计入本轮)
_TESTING_STATES = (UiState.APPLYING, UiState.WAIT_LAUNCH,
                   UiState.WAIT_GAME, UiState.JUDGING)


class _Worker(QObject):
    """通用后台任务: threading 执行, 结果经 Qt 信号回 UI 线程。"""

    done = Signal(object)   # fn(*args) 的返回值
    fail = Signal(str)      # 异常描述

    def __init__(self, fn, *args, parent: QObject | None = None):
        super().__init__(parent)
        self._fn = fn
        self._args = args

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self.done.emit(self._fn(*self._args))
        except Exception as e:  # 后台任务边界: 失败上报而非带崩进程
            self.fail.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    """主窗口: 全事件链的枢纽与唯一调度者。"""

    def __init__(self, cfg: AppConfig):
        super().__init__()
        self._cfg = cfg
        # ---- 会话上下文(扫描/恢复成功后装配) ----
        self._scan: ScanResult | None = None
        self._graph: DependencyGraph | None = None
        self._executor: Executor | None = None
        self._engine: BisectEngine | None = None    # None = 会话未开始
        self._watcher: Watcher | None = None
        self._procmon: ProcessMonitor | None = None
        self._current_plan = None                   # 本轮计划(弹窗/提示用)
        self._last_report: ApplyReport | None = None
        self._crash_flag = False                    # 本轮内是否见过崩溃文件
        self._state = UiState.IDLE
        self._active_worker: _Worker | None = None  # 引用持有, 防 GC 回收

        self.setWindowTitle("ModBisect — MC 问题 Mod 二分排查")
        self.resize(960, 640)
        self._build_ui()
        self._apply_state()

    # ---------------------------------------------------------------- UI 构建

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        # 行1: 目录选择与扫描
        row1 = QHBoxLayout()
        self._dir_edit = QLineEdit()
        self._dir_edit.setPlaceholderText("选择 .minecraft 的 mods 目录…")
        self._dir_edit.setReadOnly(True)
        row1.addWidget(self._dir_edit, stretch=1)
        self._btn_pick = QPushButton("浏览…")
        self._btn_pick.clicked.connect(self._on_pick_dir)
        row1.addWidget(self._btn_pick)
        self._btn_scan = QPushButton("扫描")
        self._btn_scan.clicked.connect(self._on_scan)
        row1.addWidget(self._btn_scan)
        self._btn_resume = QPushButton("恢复会话…")
        self._btn_resume.clicked.connect(self._on_resume_session)
        row1.addWidget(self._btn_resume)
        root.addLayout(row1)

        # 行2: 会话控制与进度指示
        row2 = QHBoxLayout()
        self._btn_start = QPushButton("开始排查")
        self._btn_start.clicked.connect(self._on_start)
        row2.addWidget(self._btn_start)
        self._btn_abort = QPushButton("中止排查")
        self._btn_abort.clicked.connect(self._on_abort)
        row2.addWidget(self._btn_abort)
        row2.addStretch(1)
        self._lbl_round = QLabel("—")
        row2.addWidget(self._lbl_round)
        self._lbl_suspects = QLabel("—")
        row2.addWidget(self._lbl_suspects)
        root.addLayout(row2)

        # 状态行
        self._lbl_status = QLabel("请选择 mods 目录并扫描")
        root.addWidget(self._lbl_status)

        # mod 表格(全量重建, 几百行量级无需差量更新)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["状态", "名称", "版本", "元数据来源", "嫌疑"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._table, stretch=3)

        # 日志区(只读; 上限防长会话内存增长)
        # 日志区(只读; 上限防长会话内存增长)。控件名与日志方法刻意区分:
        # 实例属性赋值会遮蔽同名类方法(self._log=msg 方法), 历史踩坑, 勿合并
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        root.addWidget(self._log_view, stretch=1)

        self.setCentralWidget(central)

    # ---------------------------------------------------------------- 状态机

    def _apply_state(self) -> None:
        """按 UiState 刷新按钮可用性与文案。"""
        s = self._state
        idle_like = (UiState.IDLE, UiState.READY, UiState.DONE)
        self._btn_scan.setEnabled(s in idle_like)
        self._btn_pick.setEnabled(s in idle_like)
        self._btn_resume.setEnabled(s in idle_like)
        self._btn_start.setEnabled(s is UiState.READY and self._scan is not None)
        self._btn_abort.setEnabled(
            s in (UiState.APPLYING, UiState.WAIT_LAUNCH, UiState.WAIT_GAME))
        # 开始按钮文案: 恢复的会话(引擎在场) = 继续; 全新扫描 = 开始
        if s is UiState.READY:
            self._btn_start.setText(
                "继续排查" if self._engine is not None else "开始排查")

    def _set_state(self, s: UiState, status: str = "") -> None:
        self._state = s
        if status:
            self._lbl_status.setText(status)
        self._apply_state()

    def _log(self, msg: str) -> None:
        self._log_view.appendPlainText(msg)  # 引用改名后的控件(勿回退)

    def _refresh_indicators(self) -> None:
        """轮次/嫌疑数标签。"""
        if self._engine is None:
            self._lbl_round.setText("—")
            self._lbl_suspects.setText("—")
        else:
            self._lbl_round.setText(f"轮次 {self._engine.round_index}")
            self._lbl_suspects.setText(
                f"剩余嫌疑 {self._engine.suspect_count} 单元")

    def _refresh_table(self) -> None:
        """全量重建表格: 禁用行灰显, 嫌疑行加红点。"""
        if self._scan is None:
            self._table.setRowCount(0)
            return
        suspects = self._engine.suspects if self._engine else frozenset()
        jars = sorted(self._scan.jars, key=lambda j: j.base_name.lower())
        self._table.setRowCount(len(jars))
        for row, j in enumerate(jars):
            items = [
                QTableWidgetItem("启用" if j.enabled else "禁用"),
                QTableWidgetItem(j.label),
                QTableWidgetItem(j.version),
                QTableWidgetItem(j.source),
                QTableWidgetItem("●" if j.base_name in suspects else ""),
            ]
            if not j.enabled:
                for it in items:  # 禁用行整体灰显
                    it.setForeground(QColor(128, 128, 128))
            items[4].setForeground(QColor(211, 47, 47))  # 嫌疑红点
            for i, it in enumerate(items):
                self._table.setItem(row, i, it)

    # ---------------------------------------------------------------- 通用 worker

    def _spawn(self, fn, *args, on_done, on_fail) -> None:
        """启动后台任务并持引用(防 GC); 结果/异常回投 UI 线程。"""
        w = _Worker(fn, *args)
        w.done.connect(on_done)
        w.fail.connect(on_fail)
        self._active_worker = w
        w.start()

    # ---------------------------------------------------------------- 扫描

    def _on_pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择 mods 目录")
        if d:
            self._dir_edit.setText(d)

    def _on_scan(self) -> None:
        d = self._dir_edit.text().strip()
        if not d:
            QMessageBox.information(self, "提示", "请先选择 mods 目录")
            return
        self._set_state(UiState.SCANNING, "扫描中…")
        self._spawn(scan_mods_dir, d, self._cfg,
                    on_done=self._on_scan_done, on_fail=self._on_scan_fail)

    def _on_scan_done(self, res: ScanResult) -> None:
        self._scan = res
        self._dir_edit.setText(res.mods_dir)
        for w in res.warnings:
            self._log(f"[扫描] {w}")
        self._log(f"[扫描] {len(res.jars)} 个 jar, 实例根: {res.instance_root}")
        self._watcher_teardown()

        w_jars = [j for j in res.jars if j.enabled]  # W = 初始启用集
        if not w_jars:
            self._engine = self._graph = self._executor = None
            self._refresh_table()
            self._refresh_indicators()
            self._set_state(
                UiState.READY, "扫描完成, 但没有处于启用状态的 mod")
            return
        self._log(f"[扫描] 初始启用 {len(w_jars)} 个(=工作域 W), "
                  f"初始禁用 {len(res.jars) - len(w_jars)} 个出局")
        self._graph = DependencyGraph(w_jars, self._cfg.ignore_modids)
        for w in self._graph.warnings:
            self._log(f"[依赖图] {w}")
        if self._graph.multi_units:
            self._log(f"[依赖图] {len(self._graph.multi_units)} 组强制绑定单元"
                      "(互为唯一依赖, 物理上不可分)")
        self._executor = Executor(res.jars, self._cfg)
        self._engine = None  # 全新扫描 → 会话未开始
        self._refresh_table()
        self._refresh_indicators()
        self._set_state(UiState.READY, "就绪。点「开始排查」进入基准轮")

    def _on_scan_fail(self, err: str) -> None:
        self._set_state(UiState.IDLE, f"扫描失败: {err}")

    # ---------------------------------------------------------------- 会话恢复

    def _on_resume_session(self) -> None:
        items = list_sessions()
        if not items:
            QMessageBox.information(self, "恢复会话", "没有可恢复的历史会话")
            return
        names = [f"{it['created']} | {it['mods_dir']} | {it['rounds']} 轮"
                 for it in items]
        text, ok = QInputDialog.getItem(
            self, "恢复会话", "选择要恢复的会话:", names, 0, False)
        if not ok:
            return
        self._set_state(UiState.SCANNING, "恢复会话中(重扫+重放)…")
        self._spawn(restore_session, items[names.index(text)]["path"], self._cfg,
                    on_done=self._on_resume_done, on_fail=self._on_resume_fail)

    def _on_resume_done(self, rr: RestoreResult) -> None:
        if not rr.ok:
            self._set_state(UiState.IDLE, f"恢复失败: {rr.reason}")
            QMessageBox.warning(self, "恢复会话", rr.reason)
            return
        # 重放产物装配; executor 以"当前磁盘态"为期望态(恢复态=中断时的中间态)
        self._scan = rr.scan
        self._graph = rr.graph
        self._engine = rr.engine
        self._executor = Executor(rr.scan.jars, self._cfg)
        self._dir_edit.setText(rr.scan.mods_dir)
        self._watcher_teardown()
        self._refresh_table()
        self._refresh_indicators()
        self._log(f"[会话] 恢复成功: {len(rr.engine.history)} 轮历史已重放")
        self._set_state(UiState.READY, "会话已恢复。点「继续排查」进入下一轮")

    def _on_resume_fail(self, err: str) -> None:
        self._set_state(UiState.IDLE, f"恢复失败: {err}")

    # ---------------------------------------------------------------- watcher 生命周期

    def _watcher_teardown(self) -> None:
        if self._watcher is not None:
            self._watcher.disarm()
            self._watcher = None

    def _watcher_ensure(self) -> None:
        """为当前实例根建立并武装 watcher(重复调用先拆旧)。"""
        self._watcher_teardown()

        self._watcher = Watcher(self._scan.instance_root)
        self._watcher.game_launched.connect(self._on_game_launched)
        self._watcher.crash_detected.connect(self._on_crash)
        self._watcher.arm()
        self._log("[观察] 已监听 logs/latest.log 与 crash-reports/")

    # ---------------------------------------------------------------- 轮次循环

    def _on_start(self) -> None:
        if self._state is not UiState.READY or self._scan is None:
            return
        if self._engine is None:
            self._engine = BisectEngine(self._graph)
            self._log(f"[会话] 开始排查: 嫌疑单元 {self._engine.suspect_count}")
        else:
            if self._engine.verdict is not None:
                # 恢复的会话已终局(引擎所有 DONE 路径必设 verdict,
                # 进行中路径恒为 None): 重走终局展示, 不产生幽灵计划
                self._log("[会话] 该会话已结案, 重新展示排查结果")
                self._finish()
                return
            self._log("[会话] 继续已恢复的会话")
        self._watcher_ensure()
        self._begin_round()

    def _begin_round(self) -> None:
        """一轮之始: 取计划 → 后台 diff → 提示启动。

        幂等入口: RETEST/RETEST_SAME 都重走这里(同计划, diff 零改名)。
        """
        plan = self._engine.current_plan
        self._current_plan = plan
        self._crash_flag = False
        self._refresh_indicators()
        self._refresh_table()
        self._log(f"[轮 {plan.index}] {plan.prompt}")
        self._set_state(UiState.APPLYING, "正在切换 mod 启停…")
        self._spawn(self._executor.apply, plan.target_enabled,
                    on_done=self._on_apply_done, on_fail=self._on_apply_fail)

    def _on_apply_done(self, report: ApplyReport) -> None:
        if not report.ok:
            self._handle_apply_failure(report)
            return
        self._last_report = report
        self._refresh_table()
        save_session(self._scan, self._engine)  # 状态入轮, 落盘防中断
        if self._state is UiState.WAIT_GAME:
            # 用户在 apply 期间就已启动游戏(procmon 已跟踪): 不倒回等待提示
            self._log(f"[轮 {self._current_plan.index}] 游戏已提前启动, 继续跟踪")
            return
        self._set_state(
            UiState.WAIT_LAUNCH,
            f"{self._current_plan.prompt} — 请现在启动游戏, 测完正常关闭")

    def _on_apply_fail(self, err: str) -> None:
        self._set_state(UiState.READY, f"启停切换异常: {err}")
        QMessageBox.critical(
            self, "错误", f"启停切换异常: {err}\n建议重新扫描目录")

    def _handle_apply_failure(self, report: ApplyReport) -> None:
        """apply 明确失败(篡改/占用): 会话作废, 提示重扫。"""
        for e in report.errors:
            self._log(f"[错误] {e}")
        # 图/引擎/执行器全部失真 → 作废(表格保留展示)
        self._engine = self._graph = self._executor = None
        self._watcher_teardown()
        self._refresh_indicators()
        self._set_state(UiState.READY, "启停失败, 会话已作废")
        QMessageBox.warning(
            self, "启停失败",
            "\n".join(report.errors) +
            "\n\n目录可能被外部修改或游戏正在占用文件。\n请重新扫描后再开始。")

    # ---------------------------------------------------------------- 游戏生命周期

    def _on_game_launched(self) -> None:
        if self._state not in (UiState.WAIT_LAUNCH, UiState.APPLYING):
            return  # 非测试窗口期的启动(用户自己玩), 与排查无关
        # ProcessMonitor 每轮新实例(上一轮的已在 game_exited 后自行了结)
        self._procmon = ProcessMonitor(
            self._cfg.game_main_class,
            self._cfg.process_bind_grace_ms,
            self._cfg.launch_debounce_ms,
            self._cfg.fallback_poll_interval_ms,
        )
        self._procmon.status.connect(self._on_procmon_status)
        self._procmon.game_exited.connect(self._on_game_exited)
        self._procmon.start_tracking()
        self._set_state(UiState.WAIT_GAME,
                        "游戏运行中…(结束后会自动弹出判定)")

    def _on_procmon_status(self, msg: str) -> None:
        if self._state is UiState.WAIT_GAME:
            self._lbl_status.setText(msg)

    def _on_crash(self, name: str) -> None:
        if self._state in _TESTING_STATES:
            # JUDGING 也计入: crash 事件可能晚于游戏退出到达(race 兜底)
            self._crash_flag = True
            self._log(f"[警告] 检测到崩溃报告: {name}(本轮判定将附带警告)")

    def _on_game_exited(self) -> None:
        if self._state is not UiState.WAIT_GAME:
            return
        self._set_state(UiState.JUDGING, "游戏已结束, 请回答本轮结果")
        dlg = JudgeDialog(self._current_plan, self._crash_flag,
                          sorted(self._last_report.actual_disabled), self)
        if dlg.exec():
            self._submit_answer(dlg.answer)     # accept = 有效答案
            return
        if dlg.retest:
            # 本轮作废: 引擎不推进, 同计划幂等重走(磁盘已是目标态, 零改名)
            self._log("[轮] 本轮作废, 重新测试")
            self._begin_round()
            return
        self._on_abort()  # 直接关窗 = 放弃回答 → 中止询问

    def _submit_answer(self, answer) -> None:
        # 用户在崩溃横幅下仍给出答案 = 用户确认本轮信号有效(其自主判断)
        action = self._engine.report(
            answer, self._last_report.actual_disabled, crashed=False)
        save_session(self._scan, self._engine)
        if action is Action.DONE:
            self._finish()
            return
        # NEXT_PLAN(推进)/RETEST_SAME(作废): 引擎侧状态已定, 统一重走一轮
        self._begin_round()

    # ---------------------------------------------------------------- 终局与还原

    def _finish(self) -> None:
        self._set_state(UiState.DONE, "排查结束")
        self._refresh_table()   # 嫌疑标记已收敛到罪魁
        self._watcher_teardown()
        self._log(f"[终局] {self._engine.verdict.title}")
        dlg = VerdictDialog(self._engine.verdict, self)
        dlg.exec()
        if dlg.restore_requested:
            self._set_state(UiState.APPLYING, "正在还原所有 mod…")
            self._spawn(self._executor.restore_initial,
                        on_done=self._on_restore_done,
                        on_fail=self._on_apply_fail)
        else:
            self._engine = None  # 终局后不再继续; 重新开始 = 重新扫描
            self._set_state(UiState.READY, "排查结束, mod 状态保持当前")

    def _on_restore_done(self, report: ApplyReport) -> None:
        self._refresh_table()
        if report.ok:
            self._engine = None
            self._log(f"[还原] 完成, 共还原 {len(report.renamed)} 个文件")
            self._set_state(UiState.READY, "已还原到初始状态")
        else:
            self._set_state(UiState.READY, "还原部分失败, 详见日志")
            QMessageBox.warning(self, "还原", "\n".join(report.errors))

    # ---------------------------------------------------------------- 中止

    def _on_abort(self) -> None:
        if self._state not in (UiState.APPLYING, UiState.WAIT_LAUNCH,
                               UiState.WAIT_GAME):
            return
        box = QMessageBox(self)
        box.setWindowTitle("中止排查")
        box.setText("中止本轮排查会话?")
        b_restore = box.addButton("中止并还原全部 mod",
                                  QMessageBox.ButtonRole.YesRole)
        b_keep = box.addButton("中止但保持当前状态",
                               QMessageBox.ButtonRole.NoRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (b_restore, b_keep):
            return
        self._watcher_teardown()
        if self._procmon is not None:
            self._procmon.stop()  # 游戏若在跑, 线程在其退出后自行了结
        self._engine = None
        if clicked is b_restore:
            self._set_state(UiState.APPLYING, "正在还原所有 mod…")
            self._spawn(self._executor.restore_initial,
                        on_done=self._on_restore_done,
                        on_fail=self._on_apply_fail)
        else:
            self._set_state(UiState.READY, "已中止(mod 状态保持当前)")

    def closeEvent(self, event) -> None:
        """关窗收尸: 拆 watchdog 线程, 防进程悬挂。"""
        self._watcher_teardown()
        if self._procmon is not None:
            self._procmon.stop()
        super().closeEvent(event)