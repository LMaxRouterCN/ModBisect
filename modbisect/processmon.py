# SPDX-License-Identifier: MPL-2.0
"""观察层(进程): 游戏进程绑定与退出检测。

主路径(零轮询):
  启动信号 → 枚举 java 进程(cmdline 含配置主类) → 绑定 PID
  → psutil Process.wait()(底层 WaitForSingleObject, 内核级阻塞, 零 CPU)
  → 退出 → 防抖窗口后重扫: 有新进程 = 用户重启, 重绑续等; 无 → 发射 game_exited
降级路径(绑定失败时启用, 全程序唯一轮询):
  进程快照轮询: 出现→消失 = 退出; 用户重启天然被吸收, 无需单独防抖分支。
  降级原因示例: 非常规启动参数 / 自定义主类 / 权限不足读不到 cmdline。
  修订记录: 原设计为 latest.log 文件锁探测, 但 log4j2 以共享写模式持锁,
  Windows 上独占打开测试无法区分运行/退出(假信号源), 已弃用 —— 见 GOAL-PLAN D3

所有等待都发生在后台 daemon 线程, UI 线程零阻塞。
跨线程 Qt 信号发射(auto queued), UI 线程安全接收。
watcher 的 rotate+created 双启动信号由 start_tracking 的幂等守卫吸收。
"""

from __future__ import annotations

import threading
import time

import psutil
from PySide6.QtCore import QObject, Signal


class ProcessMonitor(QObject):
    """游戏进程生命周期观察器(一轮一个实例, 会话内复用)。"""

    game_exited = Signal()   # 游戏退出(防抖确认后)
    status = Signal(str)     # 状态行文案(已绑定 PID / 降级轮询 / 等待中…)

    def __init__(self, game_main_class: str,
                 bind_grace_ms: int, debounce_ms: int, poll_ms: int,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._needle = game_main_class
        self._grace = bind_grace_ms / 1000.0
        self._debounce = debounce_ms / 1000.0
        self._poll = max(0.05, poll_ms / 1000.0)  # 下限 50ms 防御性钳位
        self._tracking = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------ 生命周期

    def start_tracking(self) -> None:
        """收到启动信号后调用; 幂等(重复启动信号在此吸收)。"""
        if self._tracking:
            return
        self._tracking = True
        self.status.emit("等待游戏进程…")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求停止(会话中止)。wait 中的线程在游戏退出后自行了结。"""
        self._tracking = False

    @property
    def tracking(self) -> bool:
        return self._tracking

    # ------------------------------------------------ 主流程(daemon 线程)

    def _run(self) -> None:
        pid = self._find_game()
        if pid is None:
            # 绑定宽限: 启动信号到进程可枚举之间有毫秒级延迟, 一次性等待后重试
            time.sleep(self._grace)
            pid = self._find_game()
        if pid is None:
            self.status.emit("未能绑定游戏进程, 降级为进程轮询")
            self._poll_loop()
            return
        self.status.emit(f"已绑定游戏进程 PID {pid}")
        try:
            while self._tracking:
                try:
                    psutil.Process(pid).wait()  # 内核级等待, 零 CPU
                except psutil.NoSuchProcess:
                    pass  # 绑定与死亡之间的竞态, 视同退出
                except psutil.Error:
                    pass  # psutil 杂类错误: 走防抖重扫兜底判定
                if not self._tracking:
                    break
                # 防抖(D4): 窗口内出现新进程 = 用户重启, 重绑续等
                time.sleep(self._debounce)
                new = self._find_game()
                if new:
                    pid = new
                    self.status.emit(f"检测到游戏重启, 重新绑定 PID {pid}")
                    continue
                break
        finally:
            if self._tracking:
                self._tracking = False
                self.game_exited.emit()

    def _poll_loop(self) -> None:
        """降级路径: 进程快照轮询(出现→消失=退出), 重启天然吸收。"""
        seen = False
        while self._tracking:
            found = self._find_game() is not None
            if seen and not found:
                # 消失 → 防抖窗口后复核, 复核仍在才算真退出
                time.sleep(self._debounce)
                if self._find_game() is None:
                    break
                continue
            seen = found
            time.sleep(self._poll)
        if self._tracking:
            self._tracking = False
            self.game_exited.emit()

    # ------------------------------------------------ 进程发现

    def _find_game(self) -> int | None:
        """cmdline 含配置主类的进程 PID(启动器等 java 进程主类不同, 天然过滤)。"""
        try:
            for p in psutil.process_iter(["pid", "cmdline"]):
                cl = p.info.get("cmdline")
                if cl and any(self._needle in str(a) for a in cl):
                    return p.info["pid"]
        except psutil.Error:
            pass  # 枚举瞬态失败: 交由调用侧重试/轮询兜底
        return None