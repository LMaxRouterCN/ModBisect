# SPDX-License-Identifier: MPL-2.0
"""观察层(文件事件): 游戏启动信号 + 崩溃报告信号。全事件驱动, 零轮询。

信号源(watchdog, Windows = ReadDirectoryChangesW):
- 启动: logs/latest.log 的 rotate(moved 事件, 旧文件被改名为日期归档)
  或重建(created 事件, 无旧文件时新建)。游戏每次启动必居其一;
  游戏运行中的持续写日志只产生 modified 事件, 不触发本信号
- 崩溃: crash-reports/ 树内新建 .txt(递归监听, 兼容 client/ 子目录)
动态挂载: logs/ 或 crash-reports/ 尚不存在时, 先监听实例根目录,
  待目录出现后再补挂(底层的 watchdog 无法监听不存在的目录)。
  已知竞态: 目录创建与首个文件的写入之间有毫秒级窗口,
  极端首启场景可能漏一次启动信号, 由下一轮启动补回(MVP 接受)

rotate 与重建会先后产生两条启动信号(moved + created),
重复信号由 ProcessMonitor.start_tracking 的幂等守卫吸收, 本层不去重。
回调发生在 watchdog 线程, 信号经 Qt 跨线程发射(auto queued)回到 UI 线程。
路径比较统一走 normcase+normpath(容忍大小写与正反斜杠差异)。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# 实例根下需要动态补挂的目录名
_DIR_LOGS = "logs"
_DIR_CRASH = "crash-reports"


def _norm(p: str) -> str:
    """路径归一(大小写+分隔符), 所有比较统一走这里。"""
    return os.path.normcase(os.path.normpath(p))


class _Handler(FileSystemEventHandler):
    """watchdog 事件 → Watcher 转发(只订阅 created/moved, 运行期写日志不触发)。"""

    def __init__(self, owner: "Watcher"):
        self._owner = owner

    def on_created(self, event):
        self._owner._on_created(event)

    def on_moved(self, event):
        self._owner._on_moved(event)


class Watcher(QObject):
    """实例目录的启动/崩溃事件源。

    生命周期: arm() 挂监听 → 事件持续发射 → disarm() 停止并回收线程。
    """

    game_launched = Signal()        # 游戏启动(本轮测试开始)
    crash_detected = Signal(str)    # 新崩溃报告文件名(本轮信号污染警报)

    def __init__(self, instance_root: str, parent: QObject | None = None):
        super().__init__(parent)
        self._root = _norm(instance_root)
        self._logs_dir = _norm(os.path.join(self._root, _DIR_LOGS))
        self._crash_dir = _norm(os.path.join(self._root, _DIR_CRASH))
        self._observer: Observer | None = None
        self._watches: dict[str, object] = {}

    # ------------------------------------------------ 生命周期

    def arm(self) -> None:
        """开始监听(已监听则幂等返回)。"""
        if self._observer is not None:
            return
        self._observer = Observer()
        handler = _Handler(self)
        # 根目录非递归: 捕捉 logs/ crash-reports/ 目录的迟到创建(动态挂载入口)
        self._watches[self._root] = self._observer.schedule(
            handler, self._root, recursive=False)
        if os.path.isdir(self._logs_dir):
            self._watches[self._logs_dir] = self._observer.schedule(
                handler, self._logs_dir, recursive=False)
        if os.path.isdir(self._crash_dir):
            self._watches[self._crash_dir] = self._observer.schedule(
                handler, self._crash_dir, recursive=True)
        self._observer.start()

    def disarm(self) -> None:
        """停止监听并回收 watchdog 线程(会话结束/中止时调用)。"""
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join()
        self._observer = None
        self._watches.clear()

    # ------------------------------------------------ 事件归判(watchdog 线程)

    def _on_created(self, event) -> None:
        path = _norm(event.src_path)
        name = os.path.basename(path)
        parent = os.path.dirname(path)
        if event.is_directory:
            # 迟到出现的 logs/ 或 crash-reports/ → 动态补挂监听
            if parent == self._root and name in (_DIR_LOGS, _DIR_CRASH):
                self._mount(
                    os.path.join(self._root, name),
                    recursive=(name == _DIR_CRASH))
            return
        if name == "latest.log" and parent == self._logs_dir:
            self.game_launched.emit()      # 新建 latest.log = 游戏启动
        elif self._in_crash_dir(path) and name.lower().endswith(".txt"):
            self.crash_detected.emit(name)

    def _on_moved(self, event) -> None:
        # 启动时的 rotate: logs/latest.log → logs/<日期>.log(旧文件被移走)
        # 或反向: 某文件被移入成为 latest.log(非常规但保守视为启动)
        for p in (_norm(event.src_path), _norm(event.dest_path)):
            if os.path.basename(p) == "latest.log" and os.path.dirname(p) == self._logs_dir:
                self.game_launched.emit()
                return
        # 崩溃文件移入(极罕见, 保守覆盖)
        dest = _norm(event.dest_path)
        if self._in_crash_dir(dest) and dest.lower().endswith(".txt"):
            self.crash_detected.emit(os.path.basename(dest))

    # ------------------------------------------------ 内部

    def _mount(self, path: str, recursive: bool) -> None:
        """给已存在的目录补挂监听(动态挂载)。"""
        if self._observer is None:
            return
        path = _norm(path)
        if path in self._watches or not os.path.isdir(path):
            return
        self._watches[path] = self._observer.schedule(
            _Handler(self), path, recursive=recursive)

    def _in_crash_dir(self, path: str) -> bool:
        """路径是否位于 crash-reports 树内(递归监听的子目录也算)。"""
        return path.startswith(self._crash_dir + os.sep)