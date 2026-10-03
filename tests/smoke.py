# SPDX-License-Identifier: MPL-2.0
"""UI 冒烟测试(offscreen): 无头环境驱动主窗口跑真实事件链。

运行: python tests/smoke.py   (退出码 0 = 通过)

覆盖(全部走真实代码路径, 仅必要替身, 见模块 docstring 末尾):
  构建主窗口(v0.2 布局) → 真扫描(8 个假 jar, m3 依赖 m0 构造级联)
  → 表头拼音排序 → 双击级联禁用/启用(m0 拖死/拉起 m3)
  → 依赖画框展开/收起 → 快照建/改状态/恢复
  → 开始排查 → 仿真启动/退出 → 右列判决按钮(非模态)
  → 二分轮真实改名 → 调试直通按钮 → 四轮全链走到确诊终局
  → 弹窗单元直构直验 → 关窗收尸(UI 偏好持久化逻辑真跑)。

测试替身(冒烟无真人按按钮, 必须替代):
  1. SnapshotDialog.exec / VerdictDialog.exec 桩: 模态 exec 无头无人
     作答会永久阻塞; 桩分别模拟"选第一个快照恢复"与"保持当前状态"。
  2. _wait_state 泵内 0.02s 休眠: 等待后台线程信号回投的物理必然,
     仅测试脚本允许(产品代码零硬编码休眠)。

进程/文件系统/watchdog/procmon 全部真实运行 —— 冒烟的意义就在真实。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# [长期记忆: 017] Windows 控制台 GBK 兜底: 中文输出防 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8")
# offscreen 必须先于任何 Qt 导入
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                      # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import modbisect.snapshots as snap_mod            # noqa: E402
import modbisect.ui.app as app_mod                # noqa: E402
from modbisect import session as session_mod      # noqa: E402
from modbisect.config import AppConfig            # noqa: E402
from modbisect.engine import Phase, Verdict       # noqa: E402
from modbisect.ui.app import MainWindow           # noqa: E402
from modbisect.ui.dialogs import (RepairDialog, SnapshotDialog,  # noqa: E402
                                  VerdictDialog)

# closeEvent 持久化打桩: 冒烟绝不写真实 config.json
# (cfg 字段赋值照常发生, 持久化逻辑可被断言)
app_mod.save_config = lambda cfg: None

# 行身份角色(与 app.py 同源; 冒烟按身份定位行, 不依赖行号)
_JAR_ROLE = app_mod._JAR_ROLE

_PASS, _FAIL = 0, 0


def check(name: str, cond, detail: str = "") -> None:
    """单条断言(带计数, 汇总决定退出码)。"""
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def make_jar(path: str, modid: str, deps: tuple[str, ...] = ()) -> None:
    """测试 jar(可选 mandatory 依赖, 构造级联场景)。"""
    toml = f'modLoader="javafml"\n[[mods]]\nmodId="{modid}"\nversion="1.0"\n'
    for d in deps:
        toml += f'[[dependencies.{modid}]]\nmodId="{d}"\n'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/mods.toml", toml)


def _wait_state(win, want: str, timeout: float) -> None:
    """事件泵等待 UiState 跳转; 超时静默返回, 由后续断言暴露。"""
    deadline = time.monotonic() + timeout
    app = QApplication.instance()
    while time.monotonic() < deadline:
        app.processEvents()
        if win._state.value == want:
            return
        time.sleep(0.02)  # 测试专用: 等待后台线程信号回投


def _wait_status(win, needle: str, timeout: float) -> None:
    """事件泵等待状态行文本出现 needle; 超时静默(断言暴露)。

    v0.3.1: 级联不再翻转状态机 → wait_state 失效; 开关入口同步
    写"正在…"文本(覆盖旧文案 = 隔出等待边界), 完成回投写"开关
    完成" — 以文本为级联完成信号, 磁盘断言不再竞态。
    """
    deadline = time.monotonic() + timeout
    app = QApplication.instance()
    while time.monotonic() < deadline:
        app.processEvents()
        if needle in win._lbl_status.text():
            return
        time.sleep(0.02)  # 测试专用: 事件泵步进

def _row_of(win, base: str) -> int:
    """按行身份(JAR_ROLE)定位行号(排序后行号漂移, 身份恒定)。"""
    for r in range(win._table.rowCount()):
        it = win._table.item(r, 0)
        if it is not None and it.data(_JAR_ROLE) == base:
            return r
    return -1


def _disabled_on_disk(mods: str) -> list[str]:
    return sorted(n for n in os.listdir(mods) if n.endswith(".disabled"))


def _wait_snapshots(n: int, timeout: float = 5.0) -> None:
    """事件泵等待快照目录出现 n 个文件(worker 落盘是异步的)。"""
    deadline = time.monotonic() + timeout
    app = QApplication.instance()
    while time.monotonic() < deadline:
        app.processEvents()
        d = snap_mod.SNAPSHOTS_DIR
        files = ([f for f in os.listdir(d) if f.endswith(".json")]
                 if os.path.isdir(d) else [])
        if len(files) >= n:
            return
        time.sleep(0.02)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="modbisect-smoke-")
    # 会话/快照落盘重定向到临时目录(防污染真实 sessions/ 与 snapshots/)
    session_mod.SESSIONS_DIR = os.path.join(tmp, "sessions")
    snap_mod.SNAPSHOTS_DIR = os.path.join(tmp, "snapshots")
    cfg = AppConfig()
    app = QApplication([])
    try:
        # ---- 1. 构建窗口(IDLE) + v0.2 布局 ----
        print("[smoke] 构建主窗口…")
        win = MainWindow(cfg)
        win.show()  # offscreen show 零成本; show 后 close 必发 closeEvent
        app.processEvents()
        check("初始 IDLE", win._state.value == "idle")
        check("IDLE 可扫描", win._btn_scan.isEnabled())
        check("IDLE 不可开始", not win._btn_start.isEnabled())
        labels = [win._table.horizontalHeaderItem(i).text()
                  for i in range(win._table.columnCount())]
        check("六列表头", labels == ["状态", "名称", "最后修改", "版本",
                                    "元数据来源", "嫌疑"], str(labels))
        hh = win._table.horizontalHeader()
        check("默认名称列升序", hh.sortIndicatorSection() == 1)
        check("列可拖动(列序持久化前提)", hh.sectionsMovable())
        check("右列判决组隐藏", not win._btn_judge_present.isVisible())
        check("调试按钮常亮(v0.5.3, IDLE 态)", win._btn_debug.isEnabled())
        check("冻结/解冻门控(IDLE 禁)", not win._btn_freeze.isEnabled()
              and not win._btn_unfreeze.isEnabled())
        check("左树空态(v0.3)", win._tree._title.text() == "依赖树"
              and win._tree._up_tree.topLevelItemCount() == 0)

        # ---- 2. 扫描(m3 依赖 m0: 级联场景) ----
        mods = os.path.join(tmp, "mods")
        os.makedirs(mods)
        for i in range(8):
            deps = ("mod0",) if i == 3 else ()
            make_jar(os.path.join(mods, f"m{i}.jar"), f"mod{i}", deps)
        win._dir_edit.setText(mods)
        print("[smoke] 扫描…")
        win._on_scan()
        _wait_state(win, "ready", 5.0)
        check("扫描后 READY", win._state.value == "ready")
        check("8 jar 入表", win._table.rowCount() == 8)
        check("引擎未装配(全新扫描)", win._engine is None)
        mtime_txt = win._table.item(_row_of(win, "m0.jar"), 2).text()
        check("mtime 列填充", re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", mtime_txt) is not None, mtime_txt)

        # ---- 3. 拼音排序: 名称列(默认升序) ----
        app.processEvents()
        order = [win._table.item(r, 0).data(_JAR_ROLE)
                 for r in range(win._table.rowCount())]
        check("默认名称拼音升序", order == [f"m{i}.jar" for i in range(8)],
              str(order))
        # ---- 3.5 左树联动(v0.3): selectRow 走真实信号链 ----
        t = win._tree
        win._table.selectRow(_row_of(win, "m0.jar"))
        app.processEvents()
        check("单击选中联动左树", "mod0" in t._title.text())
        up_texts = [t._up_tree.topLevelItem(k).text(0)
                    for k in range(t._up_tree.topLevelItemCount())]
        check("m0 上树=依赖它的 m3", len(up_texts) == 1 and "mod3" in up_texts[0],
              str(up_texts))
        check("m0 下树=无依赖", t._down_tree.topLevelItemCount() == 1
              and "无" in t._down_tree.topLevelItem(0).text(0))
        win._table.selectRow(_row_of(win, "m3.jar"))
        app.processEvents()
        check("m3 下树=依赖 m0", t._down_tree.topLevelItemCount() == 1
              and "mod0" in t._down_tree.topLevelItem(0).text(0))
        check("m3 上树=无人依赖", t._up_tree.topLevelItemCount() == 1
              and "无" in t._up_tree.topLevelItem(0).text(0))
        # 树内容不随未选中的级联操作改变(只读联动)
        # (m3 行仍选中: 下树 m0 应显示 [启用])
        down_text = t._down_tree.topLevelItem(0).text(0)
        check("m3 下树 m0 状态=启用", "启用" in down_text, down_text)

        # ---- 4. 级联禁用: 双击 m0 状态格(m3 依赖 mod0 → 陪葬) ----
        print("[smoke] 双击级联开关…")
        win._on_cell_double(_row_of(win, "m0.jar"), 0)
        _wait_status(win, "开关完成", 5.0)
        dis = _disabled_on_disk(mods)
        check("级联禁用拖死依赖者",
              dis == ["m0.jar.disabled", "m3.jar.disabled"], str(dis))

        # ---- 5. 状态列排序: 禁用(False)排前 ----
        hh.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        app.processEvents()
        first2 = [win._table.item(r, 0).data(_JAR_ROLE) for r in range(2)]
        check("状态列排序禁用聚首", set(first2) == {"m0.jar", "m3.jar"},
              str(first2))
        hh.setSortIndicator(1, Qt.SortOrder.AscendingOrder)  # 回名称序

        # ---- 6. 级联启用: 再双击 m0(连带拉起 m3) ----
        win._table.selectRow(_row_of(win, "m3.jar"))  # v0.3.1: 确立选中前提
        app.processEvents()
        win._on_cell_double(_row_of(win, "m0.jar"), 0)
        _wait_status(win, "开关完成", 5.0)
        check("级联启用拉起依赖者", _disabled_on_disk(mods) == [])
        # v0.3: 级联开关后树状态同步(refresh 事件链;
        # 就地突变与 watcher rescan 两时序分支同构收敛于"启用")
        app.processEvents()
        it0 = t._down_tree.topLevelItem(0)
        check("级联后树状态同步=启用", it0 is not None and "启用" in it0.text(0),
              (it0.text(0) if it0 is not None else "EMPTY"))

        # v0.3.1: 就地更新 = 选中保留 + 树锚定(信号屏蔽, refresh 兜底)
        check("级联后 m3 行仍选中",
              win._table.item(_row_of(win, "m3.jar"), 0).isSelected())
        check("级联后树锚定不漂移", "mod3" in t._title.text())

        # ---- 7. 依赖画框: 双击名称列展开 / 再点收起 ----
        win._on_cell_double(_row_of(win, "m0.jar"), 1)
        app.processEvents()
        check("画框展开", win._panel.isVisible() and win._panel_base == "m0.jar")
        texts = " | ".join(l.text() for l in
                           win._panel._scroll.widget().findChildren(QLabel))
        check("画框含依赖者 m3", "依赖它的(1)" in texts and "mod3" in texts,
              texts[:160])
        win._on_cell_double(_row_of(win, "m0.jar"), 1)  # 同条目再双击 = 收起
        app.processEvents()
        check("画框收起", not win._panel.isVisible())

        # ---- 8. 快照: 建 → 改状态 → 恢复 ----
        print("[smoke] 快照建/恢复…")
        win._on_snapshot_create()
        _wait_snapshots(1)
        snaps = snap_mod.list_snapshots()
        check("快照已落盘", len(snaps) == 1, str(snaps))
        win._on_cell_double(_row_of(win, "m1.jar"), 0)  # 禁 m1(无级联)
        _wait_status(win, "开关完成", 5.0)
        check("快照前改状态(禁 m1)", _disabled_on_disk(mods) == ["m1.jar.disabled"])
        # 恢复弹窗替身: 模拟"选第一个快照恢复"(无警告 → 不弹确认框)
        def _stub_snap_exec(self) -> bool:
            self._on_ok()
            return True
        SnapshotDialog.exec = _stub_snap_exec
        win._on_snapshot_restore()
        _wait_state(win, "ready", 5.0)
        check("快照恢复回全集启用", _disabled_on_disk(mods) == [])
        del SnapshotDialog.exec  # 移除桩, 回落 QDialog.exec 原实现

        # ---- 9. 开始排查 → 基准轮(全启用目标 = 零改名) ----
        print("[smoke] 开始排查(基准轮)…")
        win._on_start()
        _wait_state(win, "wait_launch", 10.0)
        check("基准轮 WAIT_LAUNCH", win._state.value == "wait_launch")
        check("基准轮零改名", _disabled_on_disk(mods) == [])
        check("会话已落盘", os.path.isdir(session_mod.SESSIONS_DIR) and
              any(n.startswith("session-")
                  for n in os.listdir(session_mod.SESSIONS_DIR)))
        check("watcher 已武装", win._watcher is not None)
        check("调试按钮可用(wait_launch)", win._btn_debug.isEnabled())
        check("冻结/解冻可用(wait_launch)", win._btn_freeze.isEnabled()
              and win._btn_unfreeze.isEnabled())

        # ---- 10. 仿真游戏生命周期 → 右列判决(非模态) ----
        print("[smoke] 仿真启动→退出→右列判决…")
        win._on_game_launched()  # 模拟 watcher 的 game_launched 信号
        check("进入 WAIT_GAME", win._state.value == "wait_game")
        check("procmon 跟踪中", win._procmon is not None
              and win._procmon.tracking)
        # v0.5.2: WAIT_GAME 冻结 = 纯记录(JVM 锁盘, 不动盘面, 下轮修正)
        win._table.selectRow(_row_of(win, "m3.jar"))
        app.processEvents()
        win._on_freeze()
        app.processEvents()
        check("WAIT_GAME 冻结纯记录", win._state.value == "wait_game"
              and win._engine.frozen == frozenset({"m3.jar"})
              and _disabled_on_disk(mods) == [])
        win._on_unfreeze()
        app.processEvents()
        check("WAIT_GAME 解冻复原嫌疑",
              win._engine.frozen == frozenset()
              and set(win._engine.suspects) == set(win._engine.universe))
        win._procmon.game_exited.emit()  # 模拟进程监控的退出信号
        check("进入 JUDGING(非模态)", win._state.value == "judging")
        check("判决组按钮可见", win._btn_judge_present.isVisible()
              and win._btn_judge_absent.isVisible())
        check("SKIP 仅基准轮可见", win._btn_judge_skip.isVisible())
        # v0.5.2: JUDGING 冻结 → 后台复原钉启用+刷新归算基准; 解冻纯状态还原
        win._on_freeze()  # 选中仍是 m3 行(WAIT_GAME 块确立)
        _wait_status(win, "冻结复原完成", 5.0)
        check("JUDGING 冻结复原完成", win._apply_busy is False
              and win._engine.frozen == frozenset({"m3.jar"})
              and win._state.value == "judging")
        check("JUDGING 归算基准已刷新", win._last_report is not None
              and win._last_report.ok)
        win._on_unfreeze()
        app.processEvents()
        check("JUDGING 解冻还原嫌疑",
              win._engine.frozen == frozenset()
              and set(win._engine.suspects) == set(win._engine.universe))
        win._on_judge_present()  # 右列按钮: 问题还在(基准轮确认复现)
        _wait_state(win, "wait_launch", 10.0)
        check("二分轮 1 WAIT_LAUNCH", win._state.value == "wait_launch")
        check("引擎推进二分", win._engine.phase is Phase.BISECT
              and win._current_plan.index == 1)
        dis = _disabled_on_disk(mods)
        check("磁盘禁用一半(4 个)", len(dis) == 4, str(dis))

        # ---- 11. 调试直通: 免游戏直接判决 → 二分轮 2 ----
        win._on_debug_fake_game()
        check("调试直通进 JUDGING", win._state.value == "judging")
        check("SKIP 非基准轮隐藏", not win._btn_judge_skip.isVisible())
        win._on_judge_present()
        _wait_state(win, "wait_launch", 10.0)
        dis = _disabled_on_disk(mods)
        check("二分轮 2 禁 2 个", len(dis) == 2, str(dis))
        # v0.5.2: WAIT_LAUNCH 冻结被禁目标 → 重开轮, apply 顺带改回启用
        win._table.selectRow(_row_of(win, "m3.jar"))
        app.processEvents()
        win._on_freeze()
        _wait_state(win, "wait_launch", 10.0)
        dis = _disabled_on_disk(mods)
        check("冻结重开轮钉回启用", win._state.value == "wait_launch"
              and win._engine.frozen == frozenset({"m3.jar"})
              and "m3.jar.disabled" not in dis and len(dis) == 2, str(dis))
        check("冻结剔除嫌疑", win._engine.suspect_count == 3)
        win._on_unfreeze()
        _wait_state(win, "wait_launch", 10.0)
        dis = _disabled_on_disk(mods)
        check("解冻重开轮复原嫌疑", win._engine.suspect_count == 4
              and len(dis) == 2, str(dis))

        # ---- 12. 全链走完: 轮 3 → 验证轮 → 确诊终局 ----
        # 终局弹窗替身: 模拟"保持当前状态"(restore_requested=False)
        VerdictDialog.exec = lambda self: True
        # 轮 3 计划: S={m0,m1} → 禁 {m1}(1 个), 其余全启用
        win._on_debug_fake_game()
        win._on_judge_present()
        _wait_state(win, "wait_launch", 10.0)
        dis = _disabled_on_disk(mods)
        check("二分轮 3 禁 1 个", len(dis) == 1, str(dis))
        # 轮 3 判决: PRESENT → S={m0} 单单元 → 引擎转 VERIFY
        win._on_debug_fake_game()
        win._on_judge_present()
        _wait_state(win, "wait_launch", 10.0)
        check("验证轮计划", win._current_plan.phase is Phase.VERIFY)
        dis = _disabled_on_disk(mods)
        check("验证轮隔离态(仅 m0 启用)", len(dis) == 7
              and "m0.jar.disabled" not in dis, str(dis))
        # 验证轮判决: 隔离复现 → 确诊 m0 → 终局(保持当前状态 → READY)
        win._on_debug_fake_game()
        win._on_judge_present()
        app.processEvents()
        check("全链终局收尾 READY", win._state.value == "ready")
        check("终局后引擎清空", win._engine is None)
        check("终局后冻结/解冻禁用", not win._btn_freeze.isEnabled()
              and not win._btn_unfreeze.isEnabled())
        check("确诊报告入日志", "确诊" in win._log_view.toPlainText())
        del VerdictDialog.exec

        # ---- 12.5 卷帘模式(v0.4): 锁序冻结+帘带推进+锁段转二分+往返 ----
        # 锁序断言全部取自引擎冻结的 spec.order, 不读 apply 后的表格
        # 显示序(apply 会改变排序键, 事后读表必漂移 —— b1 首版死因)
        win._combo_mode.setCurrentIndex(1)  # 卷帘·顶到底 禁用
        app.processEvents()
        check("卷帘模式持久化", win._cfg.ui_scan_mode == "top_disable")
        check("步长旋钮可见", not win._spin_chunk.isHidden())
        win._on_start()
        _wait_state(win, "wait_launch", 10.0)
        check("卷帘基准轮", win._current_plan.phase is Phase.BASELINE)
        win._on_debug_fake_game()
        win._on_judge_present()  # 基准在场 → 进卷帘
        _wait_state(win, "wait_launch", 10.0)
        check("进卷帘相位", win._current_plan.phase is Phase.SCAN)
        spec = win._engine.scan_spec
        check("锁序=W冻结快照", len(spec.order)
              == len(win._engine.universe), str(spec.order))
        band = set(spec.order[:spec.chunk])
        eff = win._engine.graph.closure(band,
                                        set(win._engine.universe))
        check("帘带含闭包拖拽", win._current_plan.proposed_disabled
              == frozenset(eff),
              str(sorted(win._current_plan.proposed_disabled)))
        check("帘带外保持启用", win._current_plan.target_enabled
              == win._engine.universe - frozenset(eff),
              str(sorted(win._current_plan.target_enabled)))
        win._on_debug_fake_game()
        win._on_judge_absent()  # 消失 → 锁段 → 段内转二分
        _wait_state(win, "wait_launch", 10.0)
        check("锁段转二分", win._current_plan.phase is Phase.BISECT)
        check("嫌疑收缩进被禁侧", 0 < len(win._engine.suspects)
              and win._engine.suspects <= frozenset(eff),
              str(sorted(win._engine.suspects)))
        sp = session_mod.save_session(win._scan, win._engine)
        check("卷帘会话落盘", sp is not None)
        rr = session_mod.restore_session(sp, cfg)
        check("卷帘会话恢复", rr.ok and rr.engine.scan_spec is not None,
              rr.reason)
        if rr.ok:
            check("恢复重放状态一致",
                  rr.engine.phase is win._engine.phase
                  and rr.engine.suspects == win._engine.suspects
                  and rr.engine.round_index
                  == win._engine.round_index)
        win._combo_mode.setCurrentIndex(0)  # 复位经典二分
        app.processEvents()
        check("模式复位", win._cfg.ui_scan_mode == "bisect")

        # ---- 12.55 钉扎与过滤(v0.6): 过滤框双通道 + 钉扎往返净零 ----
        # 12.5 尾态: BISECT WAIT_LAUNCH(引擎在场, 非门禁相位)
        before_pin = _disabled_on_disk(mods)
        check("钉扎标签空态", win._lbl_pinned.text() == "")
        # 过滤框(需求 ①): 名称子串通道
        win._edit_filter.setText("mod7")
        app.processEvents()
        vis = [win._table.item(r, 0).data(_JAR_ROLE)
               for r in range(win._table.rowCount())
               if not win._table.isRowHidden(r)]
        check("过滤名称通道 mod7", vis == ["m7.jar"], str(vis))
        # 排序变更 → 行号漂移 → 过滤重放(隐藏态跟身份不跟行号)
        hh.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        app.processEvents()
        vis = [win._table.item(r, 0).data(_JAR_ROLE)
               for r in range(win._table.rowCount())
               if not win._table.isRowHidden(r)]
        check("排序变更后过滤重放", vis == ["m7.jar"], str(vis))
        hh.setSortIndicator(1, Qt.SortOrder.AscendingOrder)  # 回名称序
        app.processEvents()
        win._edit_filter.clear()
        app.processEvents()
        check("清空过滤全显", all(not win._table.isRowHidden(r)
                                  for r in range(win._table.rowCount())))
        # 钉启(菜单分发): WAIT_LAUNCH 重开轮吸收(overlay 并入启用侧)
        win._pin_menu_dispatch(frozenset({"m7.jar"}), 1)
        _wait_state(win, "wait_launch", 10.0)
        check("钉启入账", win._engine.pinned_on == frozenset({"m7.jar"}))
        check("钉扎计数标签", "钉 1 个" in win._lbl_pinned.text())
        check("钉启状态格文案", win._table.item(
            _row_of(win, "m7.jar"), 0).text() == "钉启")
        check("重开轮计划吸收钉启",
              "m7.jar" in set(win._current_plan.target_enabled))
        # 已钉行双击 = 撤销(智能钉反向)
        win._on_cell_double(_row_of(win, "m7.jar"), 0)
        app.processEvents()
        _wait_state(win, "wait_launch", 10.0)
        check("双击撤销钉扎", win._engine.pinned == frozenset()
              and win._lbl_pinned.text() == "")
        # 未钉启用行双击 = 反向钉禁(智能钉); 再双击撤销
        win._on_cell_double(_row_of(win, "m7.jar"), 0)
        app.processEvents()
        _wait_state(win, "wait_launch", 10.0)
        check("双击智能钉禁", win._engine.pinned_off == frozenset({"m7.jar"})
              and win._table.item(_row_of(win, "m7.jar"), 0).text() == "钉禁")
        win._on_cell_double(_row_of(win, "m7.jar"), 0)
        app.processEvents()
        _wait_state(win, "wait_launch", 10.0)
        check("钉扎往返净零扰动", win._engine.pinned == frozenset()
              and _disabled_on_disk(mods) == before_pin)

        # ---- 12.6 阻碍子流程(v0.5): 未测试→可测性子二分→冻结→回主 ----
        # 12.5 尾态: SCAN 锁段后 BISECT(S={m0..m3}), 本轮计划禁 {m2,m3}
        # [长期记忆: 010] 断言全部派生自 plan/engine 属性(不锚单元索引序)
        win._on_debug_fake_game()
        check("未测试按钮主相位可见", win._btn_judge_untested.isVisible())
        check("可测性组非子流程隐藏",
              not win._btn_judge_testable.isVisible())
        win._on_judge_present()  # 还在 → S={m0,m1}(下一轮禁半侧)
        _wait_state(win, "wait_launch", 10.0)
        p_half = win._current_plan  # 禁单嫌疑的二分轮计划
        win._on_debug_fake_game()
        win._on_judge_untested()  # 本轮起不来 → 候选池=本轮增量
        _wait_state(win, "wait_launch", 10.0)
        check("进可测性子流程", win._current_plan.phase is Phase.OBSTRUCT)
        check("可测性轮提示", "可测性轮" in win._current_plan.prompt)
        win._on_debug_fake_game()
        check("可测性按钮组接管", win._btn_judge_testable.isVisible()
              and win._btn_judge_untestable.isVisible())
        check("主判决组退场", not win._btn_judge_present.isVisible()
              and not win._btn_judge_untested.isVisible())
        win._on_judge_untestable()  # 确认阻碍=被禁候选 → 冻结
        _wait_state(win, "wait_launch", 10.0)
        check("冻结=本轮被禁候选", win._engine.frozen
              == frozenset(p_half.proposed_disabled))
        check("冻结后单嫌疑进验证", win._current_plan.phase is Phase.VERIFY)
        check("验证目标=嫌疑支撑∪冻结",
              set(win._current_plan.target_enabled)
              == set(win._engine.graph.support(set(win._engine.suspects)))
              | set(win._engine.frozen))
        check("冻结计数标签", "冻结 1" in win._lbl_frozen.text())
        _fj = next(iter(win._engine.frozen))
        check("冻结行橙◆", win._table.item(
            _row_of(win, _fj), 5).text() == "◆")
        check("冻结日志入册", "[冻结]" in win._log_view.toPlainText())
        sp = session_mod.save_session(win._scan, win._engine)
        rr = session_mod.restore_session(sp, cfg)
        check("阻碍会话恢复", rr.ok, rr.reason)
        if rr.ok:
            check("冻结态重放一致", rr.engine.frozen
                  == win._engine.frozen
                  and rr.engine.phase is win._engine.phase
                  and rr.engine.suspects == win._engine.suspects
                  and rr.engine.round_index == win._engine.round_index)

        # ---- 12.7 手动直通游戏退出(v0.5.3): WAIT_GAME 宣布退出 ----
        # 尾态 WAIT_LAUNCH(12.6 尾轮停于等启动): 假启动进游戏态再直通
        win._on_game_launched()
        check("直通前奏进 WAIT_GAME", win._state.value == "wait_game"
              and win._procmon is not None and win._procmon.tracking)
        win._on_debug_fake_game()
        check("手动宣布退出进 JUDGING", win._state.value == "judging")
        check("procmon 被直通吞停", not win._procmon.tracking)

        # ---- 13. 弹窗单元(直构直验, 不 exec) ----
        sd = SnapshotDialog([{"path": "a.json", "created": "t1",
                              "mods_dir": "d", "count": 3}])
        sd._on_ok()
        check("快照弹窗选中", sd.selected == "a.json")
        sd.deleteLater()
        rd = RepairDialog("Decl", "ghost", [("t.jar", "T", "old")], "prefix")
        rd._on_patch()
        check("修补弹窗选择", rd.choice == "t.jar")
        rd2 = RepairDialog("Decl", "ghost", [("t.jar", "T", "old")], "prefix")
        rd2._on_ignore()
        check("修补弹窗忽视", rd2.choice is None)
        rd.deleteLater()
        rd2.deleteLater()
        vd = VerdictDialog(Verdict(title="t", detail="d",
                                   culprit=frozenset({"m.jar"})))
        vd._on_restore()
        check("终局弹窗还原标记", vd.restore_requested is True)
        vd.deleteLater()
        app.processEvents()  # 冲刷 deleteLater

        # ---- 14. 关窗收尸 + UI 偏好持久化逻辑 ----
        print("[smoke] 关窗收尸…")
        win.close()
        app.processEvents()
        check("watcher 已拆", win._watcher is None)
        check("procmon 已停", win._procmon is not None
              and not win._procmon.tracking)
        check("几何已持久化", len(cfg.ui_window_geometry) > 10)
        check("表头已持久化", len(cfg.ui_header_state) > 10)
        check("目录已持久化", cfg.ui_last_mods_dir == mods)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n冒烟结果: {_PASS} 通过, {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())