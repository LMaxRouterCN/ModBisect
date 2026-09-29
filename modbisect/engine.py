# SPDX-License-Identifier: MPL-2.0
"""引擎层: 二分状态机(纯逻辑域)。

不碰文件系统,不持有时间,不做 IO —— 引擎只消费"计划已执行"的事实
(执行层回报的实际生效禁用集)并推进状态。

核心不变量: 罪魁 ∈ 嫌疑集 S(绑定单元粒度),S 只缩小不放大。

工作域 W = 扫描时刻初始启用的 jar 全集:
- 初始禁用的 jar 不加载 → 不可能是当前 bug 的来源 → 出局
- 依赖图按 W 构图,基线无人提供的依赖边已在构图时剔除,
  因此初始状态天然满足闭包不动点 closure(空, W) = 空,推理自洽

每轮语义:
- 基准轮(第 0 轮,可跳过): 目标状态 = W 全启用(即用户初始状态),
  确认 "bug 存在" 前提;不复现 → 前提动摇,结案
- 二分轮: 提议禁 B = S 的后半单元,闭包扩为实际禁用集 D'(执行层回报);
  "还在" → S := S ∖ units(D');"消失了" → S := S ∩ units(D')
- 验证轮: |S| = 1 后,目标启用集 = 嫌疑单元的支撑闭包(正向依赖链全开),
  隔离复现 → 确诊单因;不复现 → 交互问题/信号噪声,结案
- 退化保护: S 归算后未缩小(闭包吞掉分割) → 转人工;S 空(信号矛盾) → 报错

调度契约(UI 层是唯一调度者):
1. plan = engine.current_plan            (只读,幂等)
2. 执行层把磁盘 diff 到 plan.target_enabled,
   回报 actual_disabled(W 域内实际禁用态 jar 集,含闭包拖拽扩大)
3. 玩家测试 → engine.report(answer, actual_disabled, crashed) → Action
4. NEXT_PLAN → 回到 1;RETEST_SAME → 同计划重走;DONE → 读 engine.verdict
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .depgraph import DependencyGraph


class Answer(str, Enum):
    """玩家对一轮测试的回答(弹窗按钮的语义映射)。"""
    PRESENT = "present"  # 还在(问题在当前启用侧)
    ABSENT = "absent"    # 消失了(问题在刚被禁用侧)
    SKIP = "skip"        # 跳过(仅基准轮合法: 信任用户前提直接进二分)


class Phase(str, Enum):
    """会话阶段。"""
    BASELINE = "baseline"  # 基准轮(前提确认)
    BISECT = "bisect"      # 二分进行中
    VERIFY = "verify"      # 验证轮(最小复现集隔离验证)
    DONE = "done"          # 终局(读 verdict)


class Action(str, Enum):
    """report() 的返回指令,UI 无脑跟随,不做自由发挥。"""
    NEXT_PLAN = "next_plan"      # 状态已推进,取下一轮计划
    RETEST_SAME = "retest_same"  # 本轮作废(崩溃/重测),同计划重走流程
    DONE = "done"                # 会话终局


@dataclass(frozen=True)
class RoundPlan:
    """一轮测试的计划书(执行层据此把磁盘 diff 到 target_enabled)。"""
    index: int                         # 轮次号(0 = 基准轮,二分轮 1 起)
    phase: Phase
    prompt: str                        # 人类可读测试指引(UI 可覆写)
    target_enabled: frozenset[str]     # 目标启用 jar 集(W 域内)
    proposed_disabled: frozenset[str]  # 提议禁用集(闭包扩大前,复盘用)
    suspects: frozenset[str]           # 本轮嫌疑 jar 集(展示用)


@dataclass
class RoundRecord:
    """历史归档(会话持久化/复盘用)。"""
    plan: RoundPlan
    answer: str                        # 玩家回答("invalid" = 崩溃作废轮)
    actual_disabled: frozenset[str]    # 执行层回报的实际生效禁用集


@dataclass
class Verdict:
    """终局报告(DONE 阶段读取)。"""
    title: str
    detail: str
    culprit: frozenset[str] = frozenset()  # 确诊罪魁 jar(仅单因结案非空)


class BisectEngine:
    """二分状态机。

    前置: graph 必须按初始启用的 jar 子集构图(工作域 W);
    UI 层负责在扫描后过滤出初始启用集再建图(空目录在 UI 层拦截)。
    """

    def __init__(self, graph: DependencyGraph):
        self.graph = graph
        self.universe: frozenset[str] = graph.universe  # 工作域 W
        self.phase: Phase = Phase.BASELINE
        # 初始嫌疑 = 全部绑定单元
        self._suspect_units: frozenset[int] = frozenset(range(len(graph.units)))
        self._round_index: int = 0
        self.history: list[RoundRecord] = []
        self.verdict: Verdict | None = None

    # -------------------------------------------------- 展示辅助(只读)

    @property
    def suspects(self) -> frozenset[str]:
        """当前嫌疑 jar 集(所有嫌疑单元成员的并集)。"""
        out: set[str] = set()
        for i in self._suspect_units:
            out |= self.graph.units[i]
        return frozenset(out)

    @property
    def suspect_count(self) -> int:
        """剩余嫌疑单元数。"""
        return len(self._suspect_units)

    @property
    def round_index(self) -> int:
        return self._round_index

    # -------------------------------------------------- 计划推导(只读)

    @property
    def current_plan(self) -> RoundPlan:
        """当前轮计划书。纯推导: report 推进状态前,重复调用结果不变。"""
        if self.phase is Phase.BASELINE:
            return RoundPlan(
                index=0,
                phase=Phase.BASELINE,
                prompt="基准轮: 以当前 mod 状态启动游戏,确认 bug 是否复现",
                target_enabled=self.universe,  # W 全启用 = 用户初始状态
                proposed_disabled=frozenset(),
                suspects=self.suspects,
            )
        if self.phase is Phase.VERIFY:
            unit_jars = self._single_suspect_unit()
            target = self.graph.support(unit_jars)  # 嫌疑 + 支撑依赖链
            return RoundPlan(
                index=self._round_index + 1,
                phase=Phase.VERIFY,
                prompt="验证轮: 仅启用嫌疑 mod 及其必需依赖,确认 bug 是否复现",
                target_enabled=target,
                proposed_disabled=self.universe - target,
                suspects=self.suspects,
            )
        # Phase.BISECT: 嫌疑单元索引排序后均分,提议禁用后半。
        # 按单元数(而非 jar 加权)均分: 轮次复杂度 = log2(单元数),
        # 单元粒度均衡即轮次最优;闭包吞并的浪费由实际生效集归算兜底
        ordered = sorted(self._suspect_units)
        mid = len(ordered) // 2
        disabled: set[str] = set()
        for i in ordered[mid:]:
            disabled |= self.graph.units[i]
        # 计划侧闭包预估(D7: 归算以执行层回报的实际集为准,不信此预估)
        eff = self.graph.closure(disabled, set(self.universe))
        n_dis = len(disabled)
        return RoundPlan(
            index=self._round_index + 1,
            phase=Phase.BISECT,
            prompt=f"二分轮: 禁用约一半嫌疑 mod({n_dis} 个),启动游戏测试",
            target_enabled=self.universe - eff,
            proposed_disabled=frozenset(disabled),
            suspects=self.suspects,
        )

    # -------------------------------------------------- 状态推进

    def report(self, answer: Answer,
               actual_disabled: frozenset[str],
               crashed: bool = False) -> Action:
        """消费一轮结果,推进状态机。

        actual_disabled 契约: W 域内本轮实际处于禁用态的 jar 集
        (执行层 diff 完成后回读磁盘的回报,含闭包拖拽扩大)。
        crashed = 本轮检测到游戏崩溃(信号无效,本轮作废重测)。
        """
        plan = self.current_plan  # 归档快照(推进前取,幂等)

        # 崩溃轮: 信号无效(游戏没起来,"消失"是假信号),作废重测
        if crashed:
            self.history.append(RoundRecord(
                plan=plan, answer="invalid",
                actual_disabled=frozenset(actual_disabled)))
            return Action.RETEST_SAME

        # 防御: 跳过仅在基准轮合法(UI 不应在其他轮提供该选项)
        if answer is Answer.SKIP and self.phase is not Phase.BASELINE:
            return Action.RETEST_SAME

        self.history.append(RoundRecord(
            plan=plan, answer=answer.value,
            actual_disabled=frozenset(actual_disabled)))

        if self.phase is Phase.BASELINE:
            return self._report_baseline(answer)
        if self.phase is Phase.VERIFY:
            return self._report_verify(answer)
        return self._report_bisect(answer, actual_disabled)

    # -------------------------------------------------- 各阶段归算(私有)

    def _report_baseline(self, answer: Answer) -> Action:
        if answer is Answer.ABSENT:
            self.verdict = Verdict(
                title="基准轮未复现",
                detail="当前 mod 状态下 bug 不存在。可能: bug 是间歇性的 / "
                       "由资源包·光影·存档·网络等非 mod 因素引起 / 观察条件变化。\n"
                       "建议: 确认稳定复现条件后再重新开始排查。")
            self.phase = Phase.DONE
            return Action.DONE
        # PRESENT → 正常进二分;SKIP → 信任用户前提直接进二分
        self.phase = Phase.BISECT
        return self._enter_next_phase_or_verify()

    def _report_bisect(self, answer: Answer,
                       actual_disabled: frozenset[str]) -> Action:
        # 受禁用影响的单元(以执行层回报的实际集为准,不信计划预估)
        hit = {self.graph.unit_of[j] for j in actual_disabled
               if j in self.graph.unit_of}
        before = self._suspect_units
        if answer is Answer.PRESENT:
            # 还在 → 罪魁在存活侧 → 排除被禁(含被闭包拖死)的嫌疑单元
            after = before - hit
        else:
            # 消失了 → 罪魁在实际被禁集内(含被闭包拖死的启用侧单元)
            after = before & hit
        self._round_index += 1

        if not after:
            # 归算为空: 各轮信号自相矛盾(理论不可达,防御兜底)
            self._suspect_units = after
            self.verdict = Verdict(
                title="信号矛盾",
                detail="嫌疑集被归算为空: 各轮回答互相矛盾,或依赖图与实际"
                       "加载行为不符。建议核实观察可靠性后重新排查。")
            self.phase = Phase.DONE
            return Action.DONE
        if len(after) == len(before):
            # 归算未缩小: 闭包吞掉整轮分割 → 二分在此图上失效,转人工
            self._suspect_units = after
            self.verdict = Verdict(
                title="二分退化",
                detail="依赖闭包吞掉了本轮分割(禁一半会拖死另一半),"
                       "无法继续有效二分。剩余嫌疑见列表。\n"
                       "建议: 人工排查,或等待后续版本的自由开关记录模式。")
            self.phase = Phase.DONE
            return Action.DONE
        self._suspect_units = after
        return self._enter_next_phase_or_verify()

    def _report_verify(self, answer: Answer) -> Action:
        unit_jars = self._single_suspect_unit()
        if answer is Answer.PRESENT:
            self.verdict = Verdict(
                title="确诊: 单一问题 mod",
                detail="嫌疑 mod 在最小启用集下独立复现问题,确诊为罪魁。\n"
                       "建议: 保持该 mod 禁用,还原其余全部 mod,"
                       "再进一次游戏确认问题彻底消失。",
                culprit=unit_jars)
        else:
            self.verdict = Verdict(
                title="疑似多 mod 交互问题",
                detail="嫌疑 mod 在最小启用集(仅它+必需依赖)下不复现 → "
                       "问题由多 mod 组合交互引起,或依赖图外因素。\n"
                       "建议: 人工组合排查,或等待后续版本的自由开关记录模式。")
        self.phase = Phase.DONE
        return Action.DONE

    # -------------------------------------------------- 内部工具(私有)

    def _enter_next_phase_or_verify(self) -> Action:
        """S 收缩后判定是否已收敛到单单元(是则直接进验证轮)。"""
        if len(self._suspect_units) == 1:
            self.phase = Phase.VERIFY
        return Action.NEXT_PLAN

    def _single_suspect_unit(self) -> frozenset[str]:
        """VERIFY 阶段的嫌疑单元成员集(此时 S 恰有一个单元)。"""
        idx = next(iter(self._suspect_units))
        return self.graph.units[idx]