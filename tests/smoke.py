# SPDX-License-Identifier: MPL-2.0
"""UI 冒烟测试(offscreen): 无头环境驱动主窗口跑真实事件链。

运行: python tests/smoke.py   (退出码 0 = 通过)

覆盖(全部走真实代码路径, 仅两处测试替身, 见模块末注释):
  构建主窗口 → 真扫描(8 个假 jar) → 开始排查(基准轮 apply, 零改名)
  → 仿真启动信号 → 仿真退出信号 → 判决弹窗(替身自动选"问题还在")
  → 引擎推进二分轮 1 → apply worker 真改名 4 个文件
  → 断言磁盘/表格/引擎状态 → 关窗收尸。

测试替身(冒烟无真人按按钮, 必须替代):
  1. JudgeDialog.exec 桩: 模态 exec 在无头环境无人可答会永久阻塞,
     桩 = 直接调 _choose(PRESENT) 模拟点击; 另设 QTimer 兜底:
     若桩未生效(弹窗真的进入 exec), 1 秒后定时器替它作答, 保证永不挂死。
  2. _wait_state 泵内 0.02s 休眠: 等待后台线程信号回投的物理必然,
     仅测试脚本允许(产品代码零硬编码休眠)。

进程/文件系统/watchdog/procmon 全部真实运行 —— 冒烟的意义就在真实。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# offscreen 必须先于任何 Qt 导入
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from modbisect import session as session_mod
from modbisect.config import AppConfig
from modbisect.engine import Answer, Phase, Verdict
from modbisect.ui.app import MainWindow
from modbisect.ui.dialogs import JudgeDialog, VerdictDialog

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


def make_jar(path: str, modid: str) -> None:
    """测试 jar(与 test_core 同构的最小 mods.toml)。"""
    toml = f'modLoader="javafml"\n[[mods]]\nmodId="{modid}"\nversion="1.0"\n'
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


def _bailout_answer() -> None:
    """弹窗兜底作答: 桩失效(弹窗真进入模态循环)时由 QTimer 触发。"""
    w = QApplication.activeModalWidget()
    if isinstance(w, JudgeDialog):
        w._choose(Answer.PRESENT)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="modbisect-smoke-")
    # 会话落盘重定向到临时目录(防污染真实 sessions/)
    session_mod.SESSIONS_DIR = os.path.join(tmp, "sessions")
    cfg = AppConfig()
    app = QApplication([])
    try:
        # ---- 1. 构建窗口(IDLE) ----
        print("[smoke] 构建主窗口…")
        win = MainWindow(cfg)
        win.show()  # offscreen show 零成本; show 后 close 必发 closeEvent
        app.processEvents()
        check("初始 IDLE", win._state.value == "idle")
        check("IDLE 可扫描", win._btn_scan.isEnabled())
        check("IDLE 不可开始", not win._btn_start.isEnabled())

        # ---- 2. 扫描(真实临时 mods 目录) ----
        mods = os.path.join(tmp, "mods")
        os.makedirs(mods)
        for i in range(8):
            make_jar(os.path.join(mods, f"m{i}.jar"), f"mod{i}")
        win._dir_edit.setText(mods)
        print("[smoke] 扫描…")
        win._on_scan()
        _wait_state(win, "ready", 5.0)
        check("扫描后 READY", win._state.value == "ready")
        check("8 jar 入表", win._table.rowCount() == 8)
        check("目录框回填", win._dir_edit.text() == mods)
        check("引擎未装配(全新扫描)", win._engine is None)
        check("READY 可开始", win._btn_start.isEnabled())

        # ---- 3. 开始排查 → 基准轮 apply(目标=全启用 → 零改名) ----
        print("[smoke] 开始排查(基准轮)…")
        win._on_start()
        _wait_state(win, "wait_launch", 10.0)
        check("基准轮 WAIT_LAUNCH", win._state.value == "wait_launch")
        check("基准轮计划", win._current_plan is not None
              and win._current_plan.phase is Phase.BASELINE
              and win._current_plan.index == 0)
        check("基准轮零改名",
              all(not n.endswith(".disabled") for n in os.listdir(mods)))
        check("会话已落盘", os.path.isdir(session_mod.SESSIONS_DIR) and
              any(n.startswith("session-")
                  for n in os.listdir(session_mod.SESSIONS_DIR)))
        check("watcher 已武装", win._watcher is not None)

        # ---- 4. 仿真游戏生命周期 ----
        print("[smoke] 仿真启动→退出→判决…")
        win._on_game_launched()  # 模拟 watcher 的 game_launched 信号
        check("进入 WAIT_GAME", win._state.value == "wait_game")
        check("procmon 跟踪中", win._procmon is not None
              and win._procmon.tracking)

        # 判决弹窗替身(见模块 docstring): exec 桩 + 1s 兜底定时器
        def _stub_exec(self) -> bool:
            self._choose(Answer.PRESENT)  # 模拟用户点"问题还在"
            return True
        JudgeDialog.exec = _stub_exec
        QTimer.singleShot(1000, _bailout_answer)
        win._procmon.game_exited.emit()  # 模拟进程监控的退出信号
        check("判决后推进二分轮",
              win._state.value in ("applying", "wait_launch"),
              win._state.value)
        _wait_state(win, "wait_launch", 10.0)
        check("二分轮 1 WAIT_LAUNCH", win._state.value == "wait_launch")
        check("引擎轮次推进", win._engine.round_index == 0
              and win._engine.phase is Phase.BISECT
              and win._current_plan.index == 1)  # 已答轮数!=已apply轮数: 此刻=0
        disabled = [n for n in os.listdir(mods) if n.endswith(".disabled")]
        check("磁盘禁用一半(4 个)", len(disabled) == 4, str(disabled))
        rows = [win._table.item(r, 0).text()
                for r in range(win._table.rowCount())]
        check("表格禁用行 4", rows.count("禁用") == 4, str(rows))
        del JudgeDialog.exec  # 移除桩, 回落 QDialog.exec 原实现

        # ---- 5. 弹窗单元行为(不 exec, 直构直验) ----
        jd = JudgeDialog(win._current_plan, crashed=True,  # True 走崩溃横幅分支
                         disabled_names=["m1.jar", "m2.jar"], parent=win)
        jd._choose(Answer.ABSENT)
        check("判决弹窗答案记录", jd.answer is Answer.ABSENT and not jd.retest)
        jd.deleteLater()
        vd = VerdictDialog(Verdict(title="冒烟判决", detail="详情",
                                   culprit=frozenset({"m3.jar"})), parent=win)
        vd._on_restore()
        check("终局弹窗还原标记", vd.restore_requested is True)
        vd.deleteLater()
        app.processEvents()  # 冲刷 deleteLater

        # ---- 6. 关窗收尸 ----
        print("[smoke] 关窗收尸…")
        win.close()
        app.processEvents()
        check("watcher 已拆", win._watcher is None)
        check("procmon 已停", win._procmon is not None
              and not win._procmon.tracking)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n冒烟结果: {_PASS} 通过, {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())