# SPDX-License-Identifier: MPL-2.0
"""执行层: 把引擎计划书的目标状态 diff 到磁盘(重命名启停 mod)。

设计契约:
- 幂等: 同一目标状态重复 apply, 第二次零改名(二分"重新测试本轮"依赖此性质)
- 真实为准: 每次 apply 后回读磁盘, 引擎归算用的 actual_disabled 取自回读,
  绝不信计划书自封(D7)
- 篡改检测: apply 前全量校验磁盘状态 vs 执行层记录(D9),
  不一致立即中止(绝不在未知状态上叠加操作), 由 UI 决定重扫或放弃
- 恢复: restore_initial() 回到扫描时刻的初始状态(结案后一键还原)
- 纯同步, 无 Qt 依赖; 由 UI 包在工作线程中调用
- PermissionError 重试: 游戏占用/杀软扫描锁的物理等待,
  次数与退避走配置; 等待仅发生在此(工作)线程, UI 零阻塞
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .config import AppConfig
from .model import JarInfo


@dataclass
class ApplyReport:
    """一次 apply 的结果(回执给 UI/引擎)。"""
    ok: bool
    renamed: list[str] = field(default_factory=list)      # 本轮实际改名 jar
    actual_disabled: frozenset[str] = frozenset()         # W 域内实际禁用集(回读)
    tampered: list[str] = field(default_factory=list)     # 状态不符 jar(需重扫)
    errors: list[str] = field(default_factory=list)       # 错误描述


class Executor:
    """mods 目录的启停执行器。

    expected 状态以"执行层最后一次确认的磁盘状态"为准:
    初始 = 扫描时刻; 之后每次 apply 以回读刷新。
    """

    def __init__(self, jars: list[JarInfo], cfg: AppConfig):
        self._cfg = cfg
        self._jars = list(jars)
        self._expected: dict[str, bool] = {j.base_name: j.enabled for j in jars}
        # 扫描时刻状态快照(restore_initial 的目标)
        self._initial: dict[str, bool] = dict(self._expected)

    # ------------------------------------------------ 状态探测

    def _probe(self, jar: JarInfo) -> bool | None:
        """探测磁盘真实状态; 双存在/双缺失返回 None(异常态)。"""
        sfx = self._cfg.disabled_suffix
        en = os.path.exists(jar.path(True, sfx))
        dis = os.path.exists(jar.path(False, sfx))
        if en and not dis:
            return True
        if dis and not en:
            return False
        return None

    def verify(self) -> list[str]:
        """全量篡改检测(D9); 返回不符 jar 列表, 空列表 = 磁盘与记录一致。"""
        bad: list[str] = []
        for jar in self._jars:
            actual = self._probe(jar)
            if actual is None or actual != self._expected[jar.base_name]:
                bad.append(jar.base_name)
        return bad

    # ------------------------------------------------ 应用

    def apply(self, target_enabled: frozenset[str]) -> ApplyReport:
        """把磁盘状态 diff 到 target_enabled(计划书目标), 回读回报。"""
        # 0) 目标集合法性: 计划书里的名字必须是已知 jar(引擎/执行数据对齐的哨兵)
        unknown = [t for t in target_enabled if t not in self._expected]
        if unknown:
            return ApplyReport(ok=False, errors=[
                f"目标集包含未知 jar: {unknown[:3]}"
                "(引擎与执行层数据不一致, 属程序缺陷)"])

        # 1) 篡改检测(D9): 未知状态上绝不叠加操作
        tampered = self.verify()
        if tampered:
            return ApplyReport(
                ok=False, tampered=tampered,
                errors=[f"{len(tampered)} 个 jar 的磁盘状态与记录不符"
                        "(可能被外部改动), 请重新扫描目录后再继续"])

        # 2) diff: 只动 expected ≠ want 的 jar(幂等的根源)
        ops: list[tuple[JarInfo, bool]] = []
        for jar in self._jars:
            want = jar.base_name in target_enabled
            if want != self._expected[jar.base_name]:
                ops.append((jar, want))

        # 3) 重命名(带重试): 失败不中断其余 jar, 错误如实上报
        renamed: list[str] = []
        errors: list[str] = []
        for jar, want in ops:
            cur = self._expected[jar.base_name]
            src = jar.path(cur, self._cfg.disabled_suffix)
            dst = jar.path(want, self._cfg.disabled_suffix)
            if self._rename_with_retry(src, dst, errors, jar.base_name):
                renamed.append(jar.base_name)
                self._expected[jar.base_name] = want
                jar.enabled = want  # 同步展示模型(UI 表格读这里)

        # 4) 回读(D7): 引擎归算用的实际禁用集以磁盘为准
        actual_disabled: set[str] = set()
        for jar in self._jars:
            actual = self._probe(jar)
            if actual is None:
                errors.append(f"{jar.base_name}: 重命名后状态异常(双存在或缺失)")
                continue
            self._expected[jar.base_name] = actual
            jar.enabled = actual
            # W 域 = 初始启用的 jar; 初始禁用者永远不进引擎归算
            if self._initial[jar.base_name] and not actual:
                actual_disabled.add(jar.base_name)

        return ApplyReport(
            ok=not errors, renamed=renamed,
            actual_disabled=frozenset(actual_disabled),
            errors=errors)

    def restore_initial(self) -> ApplyReport:
        """结案一键还原: 回到扫描时刻的初始状态。"""
        target = frozenset(b for b, v in self._initial.items() if v)
        return self.apply(target)

    # ------------------------------------------------ 内部

    def _rename_with_retry(self, src: str, dst: str,
                           errors: list[str], label: str) -> bool:
        """重命名 + PermissionError 退避重试(杀软/游戏占用是瞬态锁)。"""
        retries = max(0, self._cfg.rename_retries)
        backoff = self._cfg.rename_backoff_ms / 1000.0
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                os.rename(src, dst)
                return True
            except PermissionError as e:
                # 游戏占用/杀软扫描锁: 退避后重试(本函数只在 UI 提供的工作线程里被调用)
                last_err = e
                if attempt < retries:
                    time.sleep(backoff)
            except OSError as e:
                # 目标已存在等非瞬态错误: 立即上报, 重试无意义
                errors.append(f"{label}: 重命名失败({e})")
                return False
        errors.append(f"{label}: 重命名失败, 游戏可能正在运行"
                      f"(重试 {retries} 次后仍被占用: {last_err})")
        return False