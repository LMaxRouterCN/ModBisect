# SPDX-License-Identifier: MPL-2.0
"""UI 主窗口: 信号枢纽与状态机调度(全程序唯一有"时间"概念的一层)。

设计要点:
- UI 线程零阻塞: 扫描/启停/还原/会话恢复/快照/修补全部走 _Worker
  (threading 执行 + Qt 信号回投, 跨线程 emit 自动 queued 到 UI 线程)
- 两层状态机: UiState(外壳调度)与 engine.Phase(纯计算)并存,
  本层只调度不计算, 引擎只计算不调度 —— 计算与调度彻底解耦
- watcher / processmon 的回调线程只发信号, 所有状态推进都在 UI 线程
- 会话持久化: 引擎每次状态变化(apply 完成/答案归算)后 save_session
- v0.2: 表头拼音排序 / 双击级联启停 / 依赖画框 / 快照系统 /
  修补系统 / 右侧按钮列(判决非模态化) / 调试直通 / UI 偏好持久化
- 时序边角: apply 期间用户提前启动游戏 → 同样跟踪(不倒回等待提示);
  crash 事件晚于游戏退出到达 → JUDGING 状态仍计入本轮崩溃标志
"""

from __future__ import annotations

import os
import threading
from enum import Enum

from PySide6.QtCore import QByteArray, QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..config import AppConfig, save_config
from ..depgraph import DependencyGraph
from ..engine import Action, Answer, BisectEngine, Phase
from ..executor import ApplyReport, Executor
from ..processmon import ProcessMonitor
from ..repair import find_candidates, patch_modid
from ..scanner import ScanResult, scan_mods_dir
from ..session import RestoreResult, list_sessions, restore_session, save_session
from ..snapshots import (create_snapshot, list_snapshots, load_snapshot,
                         snapshot_check, snapshot_target)
from ..sortkey import name_key, version_key
from ..watcher import Watcher
from .deptree import DepTreePanel
from .dialogs import RepairDialog, SnapshotDialog, VerdictDialog
from ..engine import ScanSpec  # v0.4: 卷帘规格(装配见 _build_engine)
from PySide6.QtWidgets import QComboBox, QSpinBox  # v0.4: 模式/步长
from .panels import DepPanel
from .style import BORDER, GOLD, GREEN, ORANGE, TEXT_DIM, YELLOW


class UiState(Enum):
    """UI 外壳状态(与 engine.Phase 分属两层)。"""
    IDLE = "idle"                 # 未扫描
    SCANNING = "scanning"         # 扫描/恢复进行中
    READY = "ready"               # 已装配, 可开始或继续
    APPLYING = "applying"         # 正在把计划 diff 到磁盘(或还原/开关/快照中)
    WAIT_LAUNCH = "wait_launch"   # 已提示, 等用户启动游戏
    WAIT_GAME = "wait_game"       # 游戏运行中(procmon 跟踪)
    JUDGING = "judging"           # 等待右列判决按钮作答
    DONE = "done"                 # 终局报告已展示


# 测试窗口期(崩溃信号只有在这些状态下才计入本轮)
_TESTING_STATES = (UiState.APPLYING, UiState.WAIT_LAUNCH,
                   UiState.WAIT_GAME, UiState.JUDGING)


class _Worker(QObject):
    """通用后台任务: threading 执行, 结果经 Qt 信号回 UI 线程。

    调用方以集合持引用防 GC(v0.2 多任务可并发: 修补在途时快照仍可发起,
    单引用会被新任务顶掉导致旧 worker 信号断链); 回投后由包装回调自摘。
    """

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


# 行身份角色: 排序后行号会漂移, 表格项携带 jar base_name 是唯一可靠身份
_JAR_ROLE = Qt.ItemDataRole.UserRole + 1


class _KeyItem(QTableWidgetItem):
    """带排序键的表格项(需求 1): __lt__ 按 UserRole 键比较, 与显示文本解耦。

    文本排序对版本号("1.10.2" < "1.9.4" 错)与中文名(Unicode 码点序)都不可靠;
    键在填充时预计算(拼音 / 数字分段 / 复合元组), 点击表头时 Qt 逐对调本方法。
    """

    def __lt__(self, other) -> bool:
        return (self.data(Qt.ItemDataRole.UserRole)
                < other.data(Qt.ItemDataRole.UserRole))


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
        self._apply_busy = False  # v0.3.1: 磁盘写在途旗标(轻操作不走状态机)
        self._procmon: ProcessMonitor | None = None
        self._current_plan = None                   # 本轮计划(提示/判决按钮用)
        self._last_report: ApplyReport | None = None
        self._crash_flag = False                    # 本轮内是否见过崩溃文件
        self._state = UiState.IDLE
        self._workers: set[_Worker] = set()         # 在途任务引用(完成自摘)

        # ---- v0.2: 依赖画框 / 级联缓存 / 修补忽视表 ----
        self._panel: DepPanel | None = None         # 依赖画框(扫描后创建)
        self._panel_base = ""                       # 画框当前展示的 jar
        self._rev_adj: dict[str, set[str]] = {}     # 反向邻接(级联直查缓存)
        self._ignored_missing: set[tuple[str, str]] = set()  # 已忽视缺失(进程级)

        self.setWindowTitle("ModBisect — MC 问题 Mod 二分排查")
        self.resize(1280, 700)  # v0.3: 左树占宽, 主窗加宽
        self._build_ui()
        self._apply_state()
        self._restore_ui_prefs()  # UI 偏好: 几何/表头/最后目录(需求 6)

    # ---------------------------------------------------------------- UI 构建

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QHBoxLayout(central)   # v0.2: 左主区 + 右按钮列(需求 7)

        # ---- 左主区(纵向) ----
        # v0.3: 左侧依赖树(QSplitter 可拖分栏: 左树 | 中主区)
        self._tree = DepTreePanel()
        main_w = QWidget()
        root = QVBoxLayout(main_w)  # root 装进 main_w(splitter 右半)
        self._root_layout = root  # 依赖画框动态插拔需要(扫描后插到状态行下)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._tree)
        split.addWidget(main_w)
        split.setStretchFactor(0, 0)  # 左树弹性 0: 挤压时主区优先保宽
        split.setStretchFactor(1, 1)
        split.setHandleWidth(4)       # 直角窄把手
        split.setSizes([240, 1040])   # 初始: 树 240px
        split.setStyleSheet(  # 局部样式: 色值与 style.py 常量同源
            f"QSplitter::handle {{ background: {BORDER}; }}"
            f"QSplitter::handle:hover {{ background: {GOLD}; }}")
        outer.addWidget(split, stretch=1)

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

        # 行2: 进度指示(主控按钮已移驻右列)
        row2 = QHBoxLayout()
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
        # 布局占位: 依赖画框(_ensure_panel)在首次扫描后插入状态行与表格之间
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["状态", "名称", "最后修改", "版本", "元数据来源", "嫌疑"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionsMovable(True)    # 列序可拖(随 saveState 持久化, 需求 6)
        header.setSectionsClickable(True)  # 点击表头排序(需求 1)
        self._table.setSortingEnabled(True)
        header.setSortIndicator(1, Qt.SortOrder.AscendingOrder)  # 默认名称升序
        self._table.cellDoubleClicked.connect(self._on_cell_double)  # 需求 3/4
        self._table.itemSelectionChanged.connect(self._on_table_selection)  # v0.3: 左树联动
        root.addWidget(self._table, stretch=3)

        # 日志区(只读; 上限防长会话内存增长)。
        # 控件名与日志方法刻意区分: 实例属性赋值会遮蔽同名类方法
        # (self._log=msg 方法), 历史踩坑, 勿合并
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        root.addWidget(self._log_view, stretch=1)

        # ---- 右按钮列(需求 7: 动作集中 + 判决非模态化) ----
        side = QVBoxLayout()
        side.setSpacing(6)
        # v0.4: 排查模式(经典二分/卷帘四向) + 卷帘步长(仅卷帘可编辑)
        mode_row = QHBoxLayout()
        self._combo_mode = QComboBox()
        for _t, _d in (
                ("经典二分", "bisect"),
                ("卷帘·顶到底 禁用", "top_disable"),
                ("卷帘·顶到底 启用", "top_enable"),
                ("卷帘·底到顶 禁用", "bottom_disable"),
                ("卷帘·底到顶 启用", "bottom_enable")):
            self._combo_mode.addItem(_t, _d)
        self._combo_mode.setToolTip(
            "模式在点击「开始排查」时生效, 会话中途更改不影响进行中的排查;"
            "启用方向首轮会把其余 mod 全部压禁(建议先建快照)")
        _idx = self._combo_mode.findData(self._cfg.ui_scan_mode)
        # 先回填索引再接线: 防初始 setCurrentIndex 触发信号时
        # _spin_chunk 尚未创建(初始化次序防御)
        self._combo_mode.setCurrentIndex(max(0, _idx))
        self._spin_chunk = QSpinBox()
        self._spin_chunk.setRange(1, 50)
        self._spin_chunk.setValue(self._cfg.ui_scan_chunk)
        self._spin_chunk.setToolTip("卷帘每轮卷动的 mod 个数")
        self._spin_chunk.valueChanged.connect(self._on_chunk_changed)
        self._combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._combo_mode, stretch=1)
        mode_row.addWidget(self._spin_chunk)
        side.addLayout(mode_row)
        self._on_mode_changed(self._combo_mode.currentIndex())
        # 会话主控(从行2移驻, 右列 = 全部动作的家)
        self._btn_start = QPushButton("开始排查")
        self._btn_start.clicked.connect(self._on_start)
        self._btn_start.setMinimumHeight(44)
        side.addWidget(self._btn_start)
        self._btn_abort = QPushButton("中止排查")
        self._btn_abort.clicked.connect(self._on_abort)
        self._btn_abort.setMinimumHeight(36)
        side.addWidget(self._btn_abort)
        side.addSpacing(12)
        # 快照(需求 5)
        self._btn_snap_new = QPushButton("创建快照")
        self._btn_snap_new.clicked.connect(self._on_snapshot_create)
        side.addWidget(self._btn_snap_new)
        self._btn_snap_restore = QPushButton("恢复快照…")
        self._btn_snap_restore.clicked.connect(self._on_snapshot_restore)
        side.addWidget(self._btn_snap_restore)
        side.addSpacing(12)
        # 调试直通(需求 10): 免启动游戏直接判决, 测试者专用
        self._btn_debug = QPushButton("就当我开关过游戏了")
        self._btn_debug.setToolTip("调试: 跳过真实启动/退出, 直接进入本轮判决")
        self._btn_debug.clicked.connect(self._on_debug_fake_game)
        side.addWidget(self._btn_debug)
        side.addStretch(1)  # 判决组沉底(视觉与"轮次进行"区隔)
        # 判决组(需求 7: 替代原模态弹窗, 仅 JUDGING 态显示)
        self._btn_judge_present = QPushButton("问题还在\n(出在当前启用的里)")
        self._btn_judge_present.setMinimumHeight(52)
        self._btn_judge_present.clicked.connect(self._on_judge_present)
        side.addWidget(self._btn_judge_present)
        self._btn_judge_absent = QPushButton("问题消失了\n(出在刚被禁用的里)")
        self._btn_judge_absent.setMinimumHeight(52)
        self._btn_judge_absent.clicked.connect(self._on_judge_absent)
        side.addWidget(self._btn_judge_absent)
        self._btn_judge_skip = QPushButton("跳过基准确认\n(我确定 bug 存在)")
        self._btn_judge_skip.setMinimumHeight(52)
        self._btn_judge_skip.clicked.connect(self._on_judge_skip)
        side.addWidget(self._btn_judge_skip)
        self._btn_judge_retest = QPushButton("重新测试本轮\n(还原本轮再测一次)")
        self._btn_judge_retest.setMinimumHeight(52)
        self._btn_judge_retest.clicked.connect(self._on_judge_retest)
        side.addWidget(self._btn_judge_retest)
        for b in (self._btn_judge_present, self._btn_judge_absent,
                  self._btn_judge_skip, self._btn_judge_retest):
            b.setVisible(False)  # 仅 JUDGING 态显示

        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setMinimumWidth(190)
        side_w.setMaximumWidth(220)
        outer.addWidget(side_w)

        self.setCentralWidget(central)

    # ---------------------------------------------------------------- 状态机

    def _apply_state(self) -> None:
        """按 UiState 刷新按钮可用性/可见性与文案。"""
        s = self._state
        idle_like = (UiState.IDLE, UiState.READY, UiState.DONE)
        self._btn_scan.setEnabled(s in idle_like)
        self._btn_pick.setEnabled(s in idle_like)
        self._btn_resume.setEnabled(s in idle_like)
        self._btn_start.setEnabled(s is UiState.READY and self._scan is not None)
        self._btn_abort.setEnabled(
            s in (UiState.APPLYING, UiState.WAIT_LAUNCH, UiState.WAIT_GAME,
                  UiState.JUDGING))  # v0.2: JUDGING 也可中止(对等原弹窗关闭路径)
        # 快照与自由开关: 仅空闲态(防磁盘态与引擎推理脱钩)
        self._btn_snap_new.setEnabled(s in idle_like and self._scan is not None)
        self._btn_snap_restore.setEnabled(s in idle_like and self._scan is not None)
        # 调试直通: 仅等待启动态(WAIT_GAME 走 procmon 真实路径)
        self._btn_debug.setEnabled(s is UiState.WAIT_LAUNCH)
        # 判决组: 仅 JUDGING 可见; 跳过按钮再限基准轮(引擎侧另有二次防御)
        judging = s is UiState.JUDGING
        for b in (self._btn_judge_present, self._btn_judge_absent,
                  self._btn_judge_skip, self._btn_judge_retest):
            b.setVisible(judging)
        if judging and self._current_plan is not None:
            self._btn_judge_skip.setVisible(
                self._current_plan.phase is Phase.BASELINE)
        # 开始按钮文案: 恢复的会话(引擎在场) = 继续; 全新扫描 = 开始
        if s is UiState.READY:
            self._btn_start.setText(
                "继续排查" if self._engine is not None else "开始排查")

    def _set_state(self, s: UiState, status: str = "") -> None:
        self._state = s
        # 清崩溃警告着色(该着色仅由 _enter_judging 按需设置)
        self._lbl_status.setStyleSheet("")
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

    def _restore_ui_prefs(self) -> None:
        """启动恢复 UI 偏好(需求 6): 窗口几何 / 表头状态 / 最后目录。"""
        if self._cfg.ui_window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(
                self._cfg.ui_window_geometry.encode("ascii")))
        if self._cfg.ui_header_state:
            self._table.horizontalHeader().restoreState(QByteArray.fromBase64(
                self._cfg.ui_header_state.encode("ascii")))
        if self._cfg.ui_last_mods_dir:
            self._dir_edit.setText(self._cfg.ui_last_mods_dir)
    # ---------------------------------------------------------------- 通用 worker

    def _spawn(self, fn, *args, on_done, on_fail) -> None:
        """后台任务: 集合持引用防 GC(并发安全), 回投后自摘。"""
        w = _Worker(fn, *args)
        self._workers.add(w)

        def _done(res, w=w):
            self._workers.discard(w)
            on_done(res)

        def _failed(err, w=w):
            self._workers.discard(w)
            on_fail(err)

        w.done.connect(_done)
        w.fail.connect(_failed)
        w.start()

    def _on_task_fail(self, err: str) -> None:
        """通用后台任务失败(轻任务: 快照创建等): 日志 + 回 READY。"""
        self._log(f"[错误] 后台任务失败: {err}")
        self._set_state(UiState.READY, f"操作失败: {err}")

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
            self._ensure_panel()       # 画框随图拆除
            self._tree.set_graph(None, res.jars)  # v0.3 树随空图回空态
            self._build_reverse_adj()
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
        self._ensure_panel()          # 画框换血(新图新 jar 集)
        self._tree.set_graph(self._graph, res.jars)  # v0.3 树换血(常驻对象注入新图)
        self._build_reverse_adj()     # 级联缓存重建
        self._ignored_missing.clear() # 新扫描重置修补忽视表(目录可能已换)
        self._refresh_table()
        self._refresh_indicators()
        self._set_state(UiState.READY, "就绪。点「开始排查」进入基准轮")
        self._run_repair_check()      # 缺失依赖修补提议(需求 8)

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
        self._ensure_panel()       # 画框随新图换血
        self._tree.set_graph(self._graph, rr.scan.jars)  # v0.3 树换血
        self._build_reverse_adj()  # 级联缓存重建(忽视表保留: 同会话延续)
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

    # ---------------------------------------------------------------- 表格

    def _mk_item(self, text: str, key, base: str | None = None) -> _KeyItem:
        """造排序键表格项; base 给状态列携带(行身份, 排序后行号漂移)。"""
        it = _KeyItem(text)
        it.setData(Qt.ItemDataRole.UserRole, key)
        if base:
            it.setData(_JAR_ROLE, base)
        return it

    def _refresh_table(self) -> None:
        """刷新表格: 禁用行灰显, 嫌疑行黄点(键排序, 需求 1/2)。

        v0.3.1 双模自选: 行身份(JAR_ROLE)集与当前 jar 集一致时
        就地更新(只换 item, 行不清 → 选中高亮天然保留; 级联/
        快照恢复/会话轮/同集重扫全部受益); 不一致自动全量重建。
        两模式统一: 身份锚恢复选中(排序重放行号漂移后按身份重
        选, 多选降级为末位); 信号屏蔽期间树不闪空态。
        """
        if self._scan is None:
            self._table.setRowCount(0)
            return
        suspects = self._engine.suspects if self._engine else frozenset()
        before = self._selected_bases()  # 身份锚: 刷新前选中集
        header = self._table.horizontalHeader()
        sort_col = header.sortIndicatorSection()
        sort_order = header.sortIndicatorOrder()
        jars = self._scan.jars
        cur = []  # 就地可行性: 现有行身份集(item 缺失记 None)
        for r in range(self._table.rowCount()):
            it = self._table.item(r, 0)
            cur.append(it.data(_JAR_ROLE) if it is not None else None)
        jar_bases = {j.base_name for j in jars}
        can = (self._table.rowCount() == len(jars)
               and len(set(cur)) == len(cur) and set(cur) == jar_bases)
        self._table.blockSignals(True)  # 屏蔽中间噪声: 树不闪空态
        try:
            self._table.setSortingEnabled(False)  # 填充期禁排序(逐行插入会跳行)
            self._table.setRowCount(len(jars))  # 就地=同数无操作; 重建=扩缩行
            for row, j in enumerate(jars):
                self._fill_row(row, j, suspects)
            self._table.setSortingEnabled(True)
            if can:
                self._table.sortItems(sort_col, sort_order)  # 键变强制重排
            else:
                header.setSortIndicator(sort_col, sort_order)  # 重放用户当前排序
        finally:
            self._table.blockSignals(False)
        # 身份锚恢复选中(全量重建清选区 / 就地排序漂移 → 按身份重选)
        rows_by_base = {}
        for r in range(self._table.rowCount()):
            it = self._table.item(r, 0)
            if it is not None:
                b = it.data(_JAR_ROLE)
                if b:
                    rows_by_base[b] = r
        for b in sorted(before):
            r = rows_by_base.get(b)
            if r is not None:
                self._table.selectRow(r)
        if self._panel is not None and self._panel.isVisible():
            self._panel.refresh()  # 画框状态同步(启停变化后)
        if self._tree is not None:
            self._tree.refresh()  # v0.3 左树状态着色同步(启停变化后)

    def _fill_row(self, row: int, j, suspects) -> None:
        """单行填充(全量/就地共用): v0.3.1 从 _refresh_table 抽取。"""
        nkey = name_key(j.label)  # 名称拼音键(需求 1)
        items = [
            self._mk_item("启用" if j.enabled else "禁用",
                          (j.enabled, nkey), base=j.base_name),
            self._mk_item(j.label, nkey),
            self._mk_item(j.mtime_str, (j.mtime, nkey)),  # 需求 2: 最后修改
            self._mk_item(j.version, (version_key(j.version), nkey)),
            self._mk_item(j.source, (j.source, nkey)),
            self._mk_item("●" if j.base_name in suspects else "",
                          (j.base_name in suspects, nkey)),
        ]
        if not j.enabled:
            for it in items:  # 禁用行整体灰显
                it.setForeground(QColor(TEXT_DIM))
        else:
            items[0].setForeground(QColor(GREEN))  # 启用状态绿(需求 9)
        items[5].setForeground(QColor(YELLOW))     # 嫌疑标记黄(需求 9)
        for i, it in enumerate(items):
            self._table.setItem(row, i, it)

    def _selected_bases(self) -> set[str]:
        """当前选中行的身份集(刷新前后选中恢复的锚, v0.3.1)。"""
        out = set()
        for idx in self._table.selectionModel().selectedRows():
            it = self._table.item(idx.row(), 0)
            if it is not None:
                b = it.data(_JAR_ROLE)
                if b:
                    out.add(b)
        return out

    # ---------------------------------------------------------------- 依赖画框

    def _ensure_panel(self) -> None:
        """(重)创建依赖画框: 重扫后图与 jar 集是新对象, 画框必须换血。"""
        if self._panel is not None:
            self._root_layout.removeWidget(self._panel)
            self._panel.deleteLater()
            self._panel = None
            self._panel_base = ""
        if self._graph is None or self._scan is None:
            return
        self._panel = DepPanel(self._graph, self._scan.jars)
        self._panel.closed.connect(self._on_panel_closed)
        # 定位: 状态行之后, 表格之前(布局项索引 3)
        self._root_layout.insertWidget(3, self._panel)

    def _on_panel_closed(self) -> None:
        self._panel_base = ""

    def _show_dep_panel(self, base: str) -> None:
        """画框开关: 同条目再双击 = 收起(需求 4)。"""
        if self._panel is None:
            return
        if self._panel_base == base and self._panel.isVisible():
            self._panel.setVisible(False)
            self._panel_base = ""
            return
        self._panel_base = base
        self._panel.show_for(base)
    # ---------------------------------------------------------------- 自由开关(级联, 需求 3)

    def _build_reverse_adj(self) -> None:
        """预计算反向邻接(提供者 jar → 依赖它的 jar 集), 双击级联 O(V) 直查。

        正向边(j 依赖 d)在 depgraph; 反向闭包是调度侧查询(级联启停专属),
        不污染纯图层的 API。
        """
        self._rev_adj = {}
        if self._graph is None:
            return
        for j, deps in self._graph.jar_deps.items():
            for d in deps:
                for p in self._graph.providers.get(d, ()):
                    self._rev_adj.setdefault(p, set()).add(j)

    def _dependents_closure(self, base: str) -> set[str]:
        """base + 全部传递依赖它的 jar 集(反向 BFS, 需求 3)。"""
        out = {base}
        frontier = [base]
        while frontier:
            cur = frontier.pop()
            for nxt in self._rev_adj.get(cur, ()):
                if nxt not in out:
                    out.add(nxt)
                    frontier.append(nxt)
        return out

    # ---------------------------------------------------------------- 左树联动(v0.3)

    def _on_table_selection(self) -> None:
        """选中行变化(单击/键盘移动) → 左树联动渲染该 jar 的依赖树。

        只读联动: 不碰状态机/不弹画框; 行身份恒由状态列携带。
        v0.3.1: 刷新期信号屏蔽, 身份锚恢复选中的 selectRow 会重新
        触发本槽(树锚定回选中行); 空选中早退保留为兜底。
        """
        if self._tree is None:
            return
        sel = self._table.selectionModel().selectedRows()
        if not sel:
            return
        it = self._table.item(sel[0].row(), 0)
        if it is None:
            return
        base = it.data(_JAR_ROLE)
        if base:
            self._tree.show_for(base)

    def _on_cell_double(self, row: int, col: int) -> None:
        """双击分发: 状态列 = 级联启停(需求 3); 其他列 = 依赖画框(需求 4)。"""
        item = self._table.item(row, 0)  # 行身份恒由状态列携带
        if item is None:
            return
        base = item.data(_JAR_ROLE)
        if not base:
            return
        if col == 0:
            self._toggle_cascade(base)
        else:
            self._show_dep_panel(base)

    def _toggle_cascade(self, base: str) -> None:
        """双击状态格: 级联切换启停(需求 3)。

        级联集 = base + 传递依赖它的(两方向同集, 对称):
        - 禁用: 与引擎闭包同向(禁它会拖死整条依赖链)
        - 启用: 字面执行"同步改变依赖它的"(连带拉起, 免缺依赖)
        仅空闲态可用; 会话进行中请先中止(防磁盘态与引擎推理脱钩)。
        """
        if self._apply_busy:  # v0.3.1: 在途开关防连点(轻操作无门控)
            return
        if self._state not in (UiState.IDLE, UiState.READY, UiState.DONE):
            self._log("[开关] 排查进行中, 请先中止会话再自由开关")
            return
        if self._scan is None or self._executor is None:
            return
        jar = next((j for j in self._scan.jars if j.base_name == base), None)
        if jar is None:
            return
        cascade = self._dependents_closure(base)
        cur = {j.base_name for j in self._scan.jars if j.enabled}
        if jar.enabled:
            target = cur - cascade   # 禁: 级联拖死
            verb = "禁用"
        else:
            target = cur | cascade   # 启: 级联拉起
            verb = "启用"
        names = sorted(cascade)
        self._log(f"[开关] {verb} {jar.label}, 级联 {len(cascade)} 个: "
                  + ", ".join(names[:8]) + ("…" if len(names) > 8 else ""))
        self._lbl_status.setText(f"正在{verb} {jar.label}(级联)…")  # v0.3.1: 轻操作不碰状态机
        self._apply_busy = True  # v0.3.1: 在途旗标(spawn 前置位, 回投清)
        self._spawn(self._executor.apply, frozenset(target),
                    on_done=self._on_cascade_done, on_fail=self._on_apply_fail)

    def _on_cascade_done(self, report: ApplyReport) -> None:
        """自由开关完成(与会话轮共用 executor, 不驱动引擎)。

        v0.3.1: 轻操作不碰状态机(按钮零闪烁), 原态自保持;
        表格就地刷新(选中保留, 见 _refresh_table 双模自选)。
        """
        self._apply_busy = False  # 旗标先清(report 失败路径同样需要)
        if not report.ok:
            self._handle_apply_failure(report)
            return
        self._refresh_table()
        # 保持原态(级联不改变会话语义), 只刷状态行文本
        self._set_state(self._state, f"开关完成, 改名 {len(report.renamed)} 个文件")

    # ---------------------------------------------------------------- 快照(需求 5)

    def _on_snapshot_create(self) -> None:
        """创建当前状态快照: 全部 mod 启停状态全集落盘。"""
        if self._scan is None:
            return
        self._spawn(create_snapshot, self._scan,
                    on_done=self._on_snapshot_created,
                    on_fail=self._on_task_fail)

    def _on_snapshot_created(self, path: str | None) -> None:
        if path:
            self._log(f"[快照] 已保存: {os.path.basename(path)}")
        else:
            self._log("[快照] 保存失败(目录不可写?)")

    def _on_snapshot_restore(self) -> None:
        """恢复快照: 选档 → 一致性检查确认 → 幂等 diff 到目标态。"""
        if self._apply_busy:  # v0.3.1: 开关在途, 延后恢复(磁盘写互斥)
            self._log("[开关] 启停切换进行中, 稍后再恢复快照")
            return
        if self._scan is None or self._executor is None:
            return
        items = list_snapshots()
        if not items:
            QMessageBox.information(self, "恢复快照", "没有可用的快照")
            return
        dlg = SnapshotDialog(items, self)
        if not dlg.exec() or dlg.selected is None:
            return
        data = load_snapshot(dlg.selected)
        if data is None:
            QMessageBox.warning(self, "恢复快照", "快照文件损坏或版本不兼容")
            return
        warns = snapshot_check(data, self._scan)
        if warns:
            ret = QMessageBox.warning(
                self, "恢复快照",
                "快照与当前目录存在差异:\n\n" + "\n".join(warns) + "\n\n仍然恢复?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret is not QMessageBox.StandardButton.Yes:
                return
        target = snapshot_target(data, self._scan)
        self._log(f"[快照] 恢复: {os.path.basename(dlg.selected)}"
                  f"(目标启用 {len(target)} 个)")
        self._set_state(UiState.APPLYING, "正在恢复快照…")
        self._spawn(self._executor.apply, target,
                    on_done=self._on_snapshot_restored,
                    on_fail=self._on_apply_fail)

    def _on_snapshot_restored(self, report: ApplyReport) -> None:
        if not report.ok:
            self._handle_apply_failure(report)
            return
        self._refresh_table()
        self._log(f"[快照] 恢复完成, 改名 {len(report.renamed)} 个文件")
        self._set_state(UiState.READY, "已恢复到快照状态")

    # ---------------------------------------------------------------- 修补(需求 8)

    def _run_repair_check(self) -> None:
        """扫描后: 缺失依赖逐条反查 → 用户裁决 → 后台改写(成功自动重扫)。

        修补在途即 return(剩余缺失交给重扫后的下一轮检查, 串行收敛);
        失败也记入忽视表(防"重扫→再弹→再失败"死循环)。
        """
        if not self._graph or not self._graph.missing:
            return
        pending = [(b, m) for b, m in self._graph.missing
                   if (b, m) not in self._ignored_missing]
        for decl_base, missing_id in pending:
            cands, layer = find_candidates(missing_id, self._scan, decl_base)
            if not cands:
                self._log(f"[修补] {decl_base} 缺失依赖 {missing_id}: "
                          "没有文件名匹配的候选")
                continue
            dj = next((j for j in self._scan.jars
                       if j.base_name == decl_base), None)
            matches = []
            for c in cands:
                cj = next((j for j in self._scan.jars
                           if j.base_name == c), None)
                if cj and cj.mods:
                    matches.append((c, cj.label, cj.mods[0].modid))
            if not matches:
                continue
            dlg = RepairDialog(dj.label if dj else decl_base, missing_id,
                               matches, layer, self)
            dlg.exec()
            if dlg.choice is None:
                self._ignored_missing.add((decl_base, missing_id))
                self._log(f"[修补] 已忽视: {decl_base} 的缺失依赖 {missing_id}")
                continue
            cj = next((j for j in self._scan.jars
                       if j.base_name == dlg.choice), None)
            if cj is None or not cj.mods:
                continue
            old_id = cj.mods[0].modid
            # 禁用态文件也修: 按实际磁盘路径改写
            path = cj.current_path(self._cfg.disabled_suffix)
            self._log(f"[修补] 改写 {dlg.choice}: modId {old_id} → {missing_id}…")
            self._spawn(patch_modid, path, old_id, missing_id,
                        on_done=lambda r: self._on_repair_done(
                            r, dlg.choice, missing_id),
                        on_fail=lambda e: self._on_repair_fail(
                            e, (dlg.choice, missing_id)))
            return  # 修补在途: 剩余缺失交给重扫后的下一轮检查

    def _on_repair_done(self, result: str | None, base: str,
                        missing_id: str) -> None:
        """修补回投: 空闲态自动重扫; 会话已开跑则不打断(改 jar 不影响
        启停推理, 只是新图要等下次扫描才接通边)。"""
        if result is None:
            if self._state in (UiState.IDLE, UiState.READY, UiState.DONE):
                self._log(f"[修补] {base} 已改为 {missing_id}, 自动重新扫描…")
                self._on_scan()  # 重扫重建图(新 missing 继续处理, 串行收敛)
            else:
                self._ignored_missing.add((base, missing_id))
                self._log(f"[修补] {base} 已改为 {missing_id}, "
                          "但会话进行中不打断 — 结束后请手动重新扫描")
        else:
            self._ignored_missing.add((base, missing_id))
            self._log(f"[修补] {base} 失败: {result}")

    def _on_repair_fail(self, err: str, key: tuple[str, str]) -> None:
        self._ignored_missing.add(key)
        self._log(f"[修补] 后台改写异常: {err}")
    # ---------------------------------------------------------------- 判决(右列按钮, 需求 7)

    def _enter_judging(self) -> None:
        """进入判决态: 右列按钮接管(原模态弹窗退役)。"""
        self._set_state(UiState.JUDGING, "本轮结束, 请在右侧按钮作答")
        if self._crash_flag:
            # 崩溃警告: 状态行橙色加粗(替代原模态红横幅, 信息同源)
            self._lbl_status.setText(
                "警告: 本轮游戏发生崩溃, 观察结果可能无效 — 建议重新测试本轮")
            self._lbl_status.setStyleSheet(
                f"color: {ORANGE}; font-weight: bold;")

    def _on_judge_present(self) -> None:
        self._submit_answer(Answer.PRESENT)

    def _on_judge_absent(self) -> None:
        self._submit_answer(Answer.ABSENT)

    def _on_judge_skip(self) -> None:
        # 跳过仅基准轮合法(引擎侧有二次防御, 其他轮按钮不可见)
        self._submit_answer(Answer.SKIP)

    def _on_judge_retest(self) -> None:
        """本轮作废: 引擎不推进, 同计划幂等重走(磁盘已是目标态, 零改名)。"""
        if self._state is not UiState.JUDGING:
            return
        self._log("[轮] 本轮作废, 重新测试")
        self._begin_round()

    def _submit_answer(self, answer) -> None:
        if self._state is not UiState.JUDGING:
            return
        # 用户在崩溃警告下仍作答 = 用户自主确认本轮信号有效
        action = self._engine.report(
            answer, self._last_report.actual_disabled, crashed=False)
        save_session(self._scan, self._engine)
        if action is Action.DONE:
            self._finish()
            return
        # NEXT_PLAN(推进)/RETEST_SAME(引擎侧作废): 统一重走一轮
        self._begin_round()

    # ---------------------------------------------------------------- 调试直通(需求 10)

    def _on_debug_fake_game(self) -> None:
        """调试通道: 就当我开关过游戏了 — 跳过启动/退出直接进判决。"""
        if self._state is not UiState.WAIT_LAUNCH:
            return
        self._log("[调试] 跳过游戏启动, 直接进入判决")
        self._enter_judging()

    # ---------------------------------------------------------------- 轮次循环

    def _build_engine(self) -> BisectEngine:
        """按模式下拉装配引擎(经典二分 / 卷帘四向, v0.4)。

        锁序 = 当前表格显示序滤 W 成员(初始启用集)的冻结快照:
        原生禁用的可选 DLC / 多版本共存不在 W, 帘带永不触碰
        [长期记忆: 008]; 开排查后改排序不影响本序。
        """
        mode = self._combo_mode.currentData()
        if mode == "bisect":
            return BisectEngine(self._graph)
        # W 锚 = graph.universe(扫描时刻冻结的初始启用集, 不可变):
        # 不读 self._scan.jars 的实时 enabled 位 —— 就地刷新会把该位
        # 同步成磁盘实时态, 会话中途不再等于会话 W(b1 教训)
        w_anchor = self._graph.universe
        order: list[str] = []
        for r in range(self._table.rowCount()):
            it = self._table.item(r, 0)
            if it is not None:
                b = it.data(_JAR_ROLE)
                if b in w_anchor:
                    order.append(b)
        from_top = not mode.startswith("bottom")
        if not from_top:
            order.reverse()  # 底到顶: 显示序反转即卷动序
        spec = ScanSpec(order=tuple(order), chunk=self._spin_chunk.value(),
                        enable=mode.endswith("enable"), from_top=from_top)
        return BisectEngine(self._graph, spec)

    def _on_mode_changed(self, idx: int) -> None:
        """模式切换: 持久化 + 步长旋钮仅卷帘模式可编辑。"""
        mode = self._combo_mode.itemData(idx)
        self._cfg.ui_scan_mode = mode if mode else "bisect"
        self._spin_chunk.setEnabled(self._cfg.ui_scan_mode != "bisect")

    def _on_chunk_changed(self, v: int) -> None:
        """步长变更: 持久化(会话中更改不影响已冻结的卷动序)。"""
        self._cfg.ui_scan_chunk = v

    def _on_start(self) -> None:
        if self._apply_busy:  # v0.3.1: 开关在途, 延后开始(磁盘写互斥)
            self._log("[开关] 启停切换进行中, 稍后再开始排查")
            return
        if self._state is not UiState.READY or self._scan is None:
            return
        if self._engine is None:
            self._engine = self._build_engine()
            _m = self._combo_mode.currentData()
            _tag = "经典二分" if _m == "bisect" else (
                "卷帘·" + ("顶到底 " if _m.startswith("top")
                           else "底到顶 ")
                + ("启用" if _m.endswith("enable") else "禁用")
                + f", 每轮 {self._spin_chunk.value()} 个")
            self._log(f"[会话] 开始排查({_tag}): "
                      f"嫌疑单元 {self._engine.suspect_count}")
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
        self._apply_busy = False  # v0.3.1: worker 异常路径清旗标
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
                        "游戏运行中…(结束后请在右侧按钮作答)")

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
        self._enter_judging()  # 判决交右列按钮(非模态)

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
                               UiState.WAIT_GAME, UiState.JUDGING):
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

    # ---------------------------------------------------------------- 关窗

    def closeEvent(self, event) -> None:
        """关窗收尸: 拆 watchdog 线程, 持久化 UI 偏好(需求 6)。"""
        self._watcher_teardown()
        if self._procmon is not None:
            self._procmon.stop()
        try:
            # 三件套: 窗口几何 / 表头(列宽列序排序) / 最后目录 → config.json
            self._cfg.ui_window_geometry = bytes(
                self.saveGeometry().toBase64()).decode("ascii")
            self._cfg.ui_header_state = bytes(
                self._table.horizontalHeader().saveState().toBase64()
            ).decode("ascii")
            self._cfg.ui_last_mods_dir = self._dir_edit.text().strip()
            save_config(self._cfg)
        except (OSError, ValueError):
            pass  # 偏好持久化失败不阻关窗(下次用默认布局)
        super().closeEvent(event)