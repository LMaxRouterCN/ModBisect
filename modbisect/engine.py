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
- 阻碍轮(v0.5): 玩家无法测试(游戏起不来/无法观察) -> 回退本轮,
  子二分定位"阻碍 mod"(禁用即破坏可测性者) -> 冻结(恒启用+移出
  调度与嫌疑池) -> 回主流程重切; 罪魁被冻出的风险已由用户确认自担

调度契约(UI 层是唯一调度者):
1. plan = engine.current_plan            (只读,幂等)
2. 执行层把磁盘 diff 到 plan.target_enabled,
   回报 actual_disabled(W 域内实际禁用态 jar 集,含闭包拖拽扩大)
3. 玩家测试 → engine.report(answer, actual_disabled, crashed) → Action
4. NEXT_PLAN → 回到 1;RETEST_SAME → 同计划重走;DONE → 读 engine.verdict

钉扎层(v0.6): 会话中手动启停经 pin_toggle 记账, 计划提案出口统一过
_pin_overlay((target ∪ 钉启) − 钉禁闭包) 强制改写; 归算侧零改动
(执行层回读盘面 = 一切真相, 天然吸收钉扎)。
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
    UNTESTED = "untested"        # 此次未测试(-> 阻碍子流程 v0.5)
    TESTABLE = "testable"        # 可测性轮: 可以正常测试
    UNTESTABLE = "untestable"    # 可测性轮: 无法测试(阻碍在此侧)


class Phase(str, Enum):
    """会话阶段。"""
    BASELINE = "baseline"  # 基准轮(前提确认)
    SCAN = "scan"          # 卷帘进行中(v0.4: 锁序逐段卷, 锁定段转二分)
    OBSTRUCT = "obstruct"  # 可测性子二分(v0.5: 定位阻碍 mod, 冻结后回主流程)
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
    """历史归档(会话持久化/复盘用)。

    v0.5.2: 记录统一入账(kind 字段区分):
    - "round": 测试轮(plan/answer/actual_disabled 有效);
    - "freeze"/"unfreeze": 手动冻结/解冻的显式事件(自由意志不可由
      状态推导, 会话重放据此直接调用 freeze/unfreeze 重建)。
      此时 plan=None, jars = 本次操作的 jar 集(整单元成员)。
    - "pin_on"/"pin_off"/"pin_clear"(v0.6): 手动钉扎显式事件, 重放
      据此直接调用 pin_toggle/pin_clear; jars 同为整单元成员。
    """
    plan: RoundPlan | None
    answer: str                        # 玩家回答("invalid" = 崩溃作废轮)
    actual_disabled: frozenset[str]    # 执行层回报的实际生效禁用集
    kind: str = "round"                # 记录类型: round / freeze / unfreeze
    jars: frozenset[str] = frozenset() # 手动事件的 jar 集(仅 freeze/unfreeze)


@dataclass
class Verdict:
    """终局报告(DONE 阶段读取)。"""
    title: str
    detail: str
    culprit: frozenset[str] = frozenset()  # 确诊罪魁 jar(仅单因结案非空)


@dataclass(frozen=True)
class ScanSpec:
    """卷帘模式规格(v0.4): 锁序/步长/方向, 会话持久化同源。

    order = 开排查时表格显示序滤 W 成员的冻结快照(之后改排序不影响);
    enable = True 启用方向(判据=问题出现, 首轮静默底场=W 全禁)。
    from_top 仅存档语义(方向已折入 order, 底到顶时 UI 预先反转)。
    """
    order: tuple[str, ...]  # 卷动顺序(base 名序列, W 域内)
    chunk: int              # 每轮卷动个数(>=1)
    enable: bool            # True=启用方向 False=禁用方向
    from_top: bool          # True=由顶向下(展示语义)


@dataclass
class _ObstructState:
    """可测性子二分进行态(v0.5; 会话重放确定式重建, 不单独落盘).

    pool = 仍含阻碍的绑定单元集(单调收缩, 至少含一个阻碍单元);
    base_actual = 触发时的最近可测配置(实际禁用集, 闭包封闭);
    return_phase = 冻结完成后回归的主流程相位.
    """

    pool: frozenset[int]
    base_actual: frozenset[str]
    return_phase: Phase


class BisectEngine:
    """二分状态机。

    前置: graph 必须按初始启用的 jar 子集构图(工作域 W);
    UI 层负责在扫描后过滤出初始启用集再建图(空目录在 UI 层拦截)。
    """

    def __init__(self, graph: DependencyGraph,
                 scan_spec: ScanSpec | None = None):
        self.graph = graph
        self.universe: frozenset[str] = graph.universe  # 工作域 W
        self.scan_spec = scan_spec  # None=经典二分; 有值=卷帘接力(v0.4)
        self.phase: Phase = Phase.BASELINE
        self._scan_pos: int = 0  # 卷帘指针: order 前缀中已卷 jar 数
        # 初始嫌疑 = 全部绑定单元
        self._suspect_units: frozenset[int] = frozenset(range(len(graph.units)))
        self._round_index: int = 0
        self.history: list[RoundRecord] = []
        self.verdict: Verdict | None = None
        self._frozen: frozenset[str] = frozenset()  # 阻碍集(v0.5): 恒钉启用
        # v0.5.2 手动冻结记账: 冻结时仍在嫌疑池的单元索引(解冻按此恢复资格)
        self._frozen_was_suspect: set[int] = set()
        # v0.5.2 冻结排空结案标志: True 时 DONE 可被 unfreeze 撤销回主流程
        self._frozen_drained = False
        # v0.5.2 冻结排空时的相位存档(解冻撤回结案时恢复到冻结前相位)
        self._frozen_drain_phase = Phase.BASELINE
        self._obstruct: _ObstructState | None = None  # 可测性子二分进行态
        # v0.6 手动钉扎(用户意志层): 计划提案出口的强制改写两集
        # 钉禁 = 恒禁用(留嫌疑池参与归因; 与冻结的"移出调度"相对)
        # 钉启 = 恒启用(不动调度, 依赖缺失由阻碍子流程自然收敛)
        self._pin_off: frozenset[str] = frozenset()
        self._pin_on: frozenset[str] = frozenset()

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

    @property
    def frozen(self) -> frozenset[str]:
        """被冻结的阻碍 mod 集(base 名, 恒钉启用, 已移出嫌疑与调度)。"""
        return self._frozen

    @property
    def pinned(self) -> frozenset[str]:
        """钉扎全集(钉禁 ∪ 钉启, base 名; UI 标记/计数用)。"""
        return self._pin_off | self._pin_on

    @property
    def pinned_off(self) -> frozenset[str]:
        """钉禁集(强制禁用侧, base 名)。"""
        return self._pin_off

    @property
    def pinned_on(self) -> frozenset[str]:
        """钉启集(强制启用侧, base 名)。"""
        return self._pin_on

    # -------------------------------------------------- 计划推导(只读)

    @property
    def current_plan(self) -> RoundPlan:
        """当前轮计划书。纯推导: report 推进状态前,重复调用结果不变。"""
        if self.phase is Phase.BASELINE:
            return RoundPlan(
                index=0,
                phase=Phase.BASELINE,
                prompt="基准轮: 以当前 mod 状态启动游戏,确认 bug 是否复现",
                target_enabled=self._pin_overlay(
                    self.universe),  # W 全启用 = 用户初始状态(v0.6 钉扎改写)
                proposed_disabled=frozenset(),
                suspects=self.suspects,
            )
        if self.phase is Phase.VERIFY:
            unit_jars = self._single_suspect_unit()
            target = self.graph.support(unit_jars) | self._frozen  # 嫌疑+支撑依赖链+冻结恒钉启用
            # v0.6: 钉扎 overlay(嫌疑自身被钉禁的场景已被入口守卫截断,
            # 此处只处理非嫌疑钉扎的常规改写)
            target = self._pin_overlay(target)
            return RoundPlan(
                index=self._round_index + 1,
                phase=Phase.VERIFY,
                prompt="验证轮: 仅启用嫌疑 mod 及其必需依赖,确认 bug 是否复现",
                target_enabled=target,
                proposed_disabled=self.universe - target,
                suspects=self.suspects,
            )
        if self.phase is Phase.SCAN:
            # 卷帘轮(v0.4): 帘带 = 锁序中下一 chunk 个(前缀已卷过)
            spec = self.scan_spec
            # v0.5: 帘带跳过冻结成员(钉启用不可卷), 指针消耗含跨过冻结位
            band, _ = self._scan_band()
            band_set = frozenset(band)
            if spec.enable:
                # 启用方向: 静默底场(仅已卷段+帘带支撑闭包启用),
                # 判据=问题出现; support 拖入的依赖"提前上场",
                # 记账按执行层实际启用集归算(与闭包拖拽同构)
                # v0.6: 钉扎 overlay(未卷段的钉启者提前上场; 钉禁闭包剔除)
                target = self._pin_overlay(
                    frozenset(spec.order[:self._scan_pos])
                    | self.graph.support(band_set)
                    | self._frozen)
                eff = self.universe - target
                prompt = (f"卷帘轮: 启用下一段 {len(band)} 个"
                          f"(其余 {len(eff)} 个全禁), 测试问题是否出现")
            else:
                # 禁用方向: 全启用前提(同基准轮), 逐段禁用(含闭包拖拽)
                rolled = ((frozenset(spec.order[:self._scan_pos]) - self._frozen) | band_set)
                eff = self.graph.closure(rolled, set(self.universe))
                # v0.6: 钉扎 overlay(钉禁闭包恒在场, 钉启恒启用)
                target = self._pin_overlay((self.universe - eff) | self._frozen)
                prompt = (f"卷帘轮: 禁用下一段 {len(band)} 个, "
                          "测试问题是否消失")
            return RoundPlan(
                index=self._round_index + 1,
                phase=Phase.SCAN,
                prompt=prompt,
                target_enabled=target,
                proposed_disabled=frozenset(eff),
                suspects=self.suspects,
            )
        if self.phase is Phase.OBSTRUCT:
            # 可测性轮(v0.5): 最近可测态 + 试探禁用候选(闭包扩张)。
            # v0.5.1 全拖活锁修: 探针必须使预报命中为池的非空真子集
            # (两答案均严格缩池, 终止性构造保证)。否则 UNTESTABLE 归算
            # pool∩hit=pool 零收缩, 而下轮 sorted(pool) 确定性重选同一
            # 探针 → 永久循环(线上: 池恒3/禁2 空转)。
            # 预报与执行后归算同构: 实际禁用 = closure(base|probe) − frozen,
            # 而 frozen∩pool=∅ → 池内命中不受 frozen 影响。
            # 单调性: 探针变大 ⟹ 预报命中变大; 禁任一池单元 u 必命中 u 自身
            # (H(u)⊇{u}), 故 |池|≥2 时单点扫描必得合法探针 —— 若每个单点
            # 都全拖, 则池内单元两两互达 → 同一 SCC, 与单元划分矛盾。
            # 补集半侧梯级经证明冗余(其合法 ⟹ 内部单点合法)裁撤,
            # 梯子两层: 标准半探 → 单点扫描。
            ob = self._obstruct
            assert ob is not None
            ordered = sorted(ob.pool)
            cand: set[str] = set()
            for i in ordered:
                cand |= self.graph.units[i]

            def _forecast(pu: frozenset[int]) -> frozenset[int]:
                """试探单元集 → 预报命中池的单元集(与 UNTESTABLE 归算同构)。

                v0.6: 命中 = 探针闭包 ∪ 钉禁闭包(overlay 恒在场)再减
                冻结/钉启(恒启用侧永不入命中) — 与计划出口 overlay 同构;
                预报偏小会使探针合法性判断失真(命中必须含钉禁拖拽)。
                """
                jars: set[str] = set()
                for i in pu:
                    jars |= self.graph.units[i]
                eff2 = self.graph.closure(ob.base_actual | jars,
                                          set(self.universe))
                hit = ((frozenset(eff2) | self._pin_dead())
                       - self._frozen - self._pin_on)
                return frozenset(
                    self.graph.unit_of[j] for j in hit
                    if j in self.graph.unit_of) & ob.pool

            probe: frozenset[int] | None = None
            degraded = False
            if len(ordered) == 1:
                probe = frozenset(ordered)  # 终局确认轮: 两答案走冻结/矛盾出口
            else:
                half = frozenset(ordered[len(ordered) // 2:])
                if _forecast(half) < ob.pool:  # 真子集 → 标准半探可用
                    probe = half
                else:
                    # 半侧全拖(依赖链拖住补集): 逐单点找合法探针(可证必达)
                    for u in ordered:
                        single = frozenset({u})
                        if _forecast(single) < ob.pool:
                            probe = single
                            degraded = True
                            break
                assert probe is not None  # |池|≥2 存在性已证(见分支头注释)
            probe_jars: set[str] = set()
            for i in probe:
                probe_jars |= self.graph.units[i]
            # 子轮目标 = 上一可测配置 + 试探侧(闭包扩张); 冻结恒钉启用
            eff = self.graph.closure(ob.base_actual | probe_jars,
                                     set(self.universe))
            # v0.6: 钉扎 overlay(与 _forecast 同构, 否则预报与实际脱钩)
            target = self._pin_overlay((self.universe - eff) | self._frozen)
            prompt = (f"可测性轮: 额外禁用 {len(probe_jars)} 个阻碍候选"
                      f"(池剩 {len(ob.pool)} 组), 游戏能否正常启动并观察?")
            if degraded:
                prompt += "(依赖链限制切分, 已改用单点试探)"
            return RoundPlan(
                index=self._round_index + 1,
                phase=Phase.OBSTRUCT,
                prompt=prompt,
                target_enabled=target,
                proposed_disabled=frozenset(probe_jars),
                suspects=frozenset(cand),
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
            # v0.5: 冻结恒钉启用(闭包拖拽也拖不死)
            # v0.6: 钉扎 overlay(钉禁闭包禁/钉启恒启)统一改写计划出口
            target_enabled=self._pin_overlay(
                (self.universe - eff) | self._frozen),
            proposed_disabled=frozenset(disabled),
            suspects=self.suspects,
        )

    # -------------------------------------------------- 状态推进

    # -------------------------------------------------- 手动冻结/解冻(v0.5.2)

    def freeze(self, bases: frozenset[str]) -> frozenset[str]:
        """手动冻结(单元粒度): 恒钉启用, 移出嫌疑与调度, 显式事件入史。

        返回实际新冻结的 jar 集(整单元成员); 空 = 无事发生
        (输入不在 W / 已全部冻结 / 非法相位)。
        非法相位: OBSTRUCT(可测性子二分维护 frozen∩pool=∅ 不变量,
        手动插入会假排除候选)与 DONE(唯一例外: 撤销冻结排空的 unfreeze)。
        冻结时仍在嫌疑池的单元记入 _frozen_was_suspect(解冻对称恢复);
        嫌疑池被冻空 = 结案(verdict「嫌疑被冻结排空」, 与自动路径同款)。
        """
        return self._manual_freeze(bases, "freeze")

    def unfreeze(self, bases: frozenset[str]) -> frozenset[str]:
        """手动解冻(冻结的对称撤销): 纯状态开关, 不动盘面。

        仅恢复解冻时原属嫌疑池的单元(_frozen_was_suspect 记账);
        会话若因「冻结排空」结案(_frozen_drained), 解冻使嫌疑池复活时
        撤销结案, 恢复到冻结前相位继续推理(单单元经
        _enter_next_phase_or_verify 自动转 VERIFY)。
        """
        return self._manual_freeze(bases, "unfreeze")

    def _manual_freeze(self, bases: frozenset[str],
                       kind: str) -> frozenset[str]:
        """freeze/unfreeze 公共体: 门禁 → 单元提升 → 记账 → 显式事件。

        单元粒度: 依赖互锁的 jar 绑成单元(SCC), 半冻单元 = 物理禁态,
        故输入按 unit_of 提升后整单元操作, 返回/入史同为整单元成员。
        """
        # 门禁: OBSTRUCT 全拒; DONE 仅允许撤销「冻结排空」的解冻
        if self.phase is Phase.OBSTRUCT:
            return frozenset()
        if self.phase is Phase.DONE and not (
                kind == "unfreeze" and self._frozen_drained):
            return frozenset()
        # 输入过滤: 只认 W 域内成员, 提升到绑定单元
        want: set[int] = set()
        for b in bases:
            if b in self.universe and b in self.graph.unit_of:
                want.add(self.graph.unit_of[b])
        if kind == "freeze":
            # 幂等: 已冻单元剔除(重复冻结 = 空操作)
            want -= {self.graph.unit_of[b] for b in self._frozen}
            if not want:
                return frozenset()
            new_jars = frozenset(
                j for i in want for j in self.graph.units[i])  # 整单元成员
            # 嫌疑池记账: 冻结时仍在池内的单元, 解冻时按此恢复资格
            for i in want:
                if i in self._suspect_units:
                    self._frozen_was_suspect.add(i)
            self._frozen |= new_jars
            # v0.6: 冻结吞钉(与自动冻结同规): 钉启冗余摘除, 钉禁
            # 连根拔(_pin_yield_to_frozen; 恒启用语义优先)
            self._pin_on -= new_jars
            self._pin_yield_to_frozen(new_jars)
            self._suspect_units -= want
            # 冻空嫌疑池: 罪魁可能就在冻结集(用户冻结 = 自担排除判断)
            if not self._suspect_units:
                self._frozen_drain_phase = self.phase  # 撤回时恢复相位
                self._frozen_drained = True
                self.verdict = Verdict(
                    title="嫌疑被冻结排空",
                    detail="全部嫌疑 mod 均被手动冻结排除。罪魁可能就在"
                           "冻结集: " + ", ".join(sorted(self._frozen))
                           + "\n(冻结 = 恒启用钉死, 不参与开关二分)\n"
                           "建议: 人工核查上述 mod, 或解冻部分后继续排查。")
                self.phase = Phase.DONE
            touched = set(new_jars)
        else:
            # 幂等: 未冻单元剔除(解冻未冻结项 = 空操作)
            want &= {self.graph.unit_of[b] for b in self._frozen}
            if not want:
                return frozenset()
            gone = frozenset(j for i in want for j in self.graph.units[i])
            # 嫌疑资格恢复: 仅原属嫌疑池的单元回池(对称还原冻结动作)
            back = want & self._frozen_was_suspect
            self._frozen -= gone
            self._frozen_was_suspect -= want
            self._suspect_units |= back
            if back:
                # 冻结排空结案被撤销: 会话复活, 恢复到冻结前相位继续推理
                self.verdict = None
                if self._frozen_drained:
                    # 仅排空结案场景恢复存档相位; 非排空解冻不动当前相位
                    self.phase = self._frozen_drain_phase
                    self._frozen_drained = False
                # 单元数重判: 单单元转 VERIFY(v0.6: 钉禁守卫可截断结案);
                # 多单元滞留 VERIFY 退回二分
                if len(self._suspect_units) == 1:
                    self.phase = Phase.VERIFY
                    self._verify_entry_guard()  # True 时已改判 DONE
                elif (len(self._suspect_units) > 1
                      and self.phase is Phase.VERIFY):
                    self.phase = Phase.BISECT
            touched = set(gone)
        # 轮次号不推进: 手动冻结/解冻不是测试轮, 不吃 plan.index 计数
        # 显式事件: 自由意志不可由状态推导, kind/jars 全量入史供重放
        self.history.append(RoundRecord(
            plan=None, answer="", actual_disabled=frozenset(),
            kind=kind, jars=frozenset(touched)))
        return frozenset(touched)

    # -------------------------------------------------- 手动钉扎(v0.6)

    def pin_toggle(self, bases: frozenset[str],
                   enable: bool) -> frozenset[str]:
        """手动钉扎(单元粒度): 计划提案出口的强制改写层, 显式事件入史。

        语义: enable=True 钉启(恒启用), False 钉禁(恒禁用);
        同单元方向翻转 = 替换(先摘旧方向, 同时只钉一个方向)。
        与冻结的分工: freeze = 钉启 + 移出嫌疑与调度(用户担保无罪);
        钉禁 = 留在嫌疑池参与归因(用户怀疑它, 问题消失轮自然命中)。
        钉启不抬升支撑依赖: 依赖被引擎禁 → 缺依赖 → UNTESTED → 阻碍
        子流程自然收敛(与 frozen 同构的载重行为); 若抬升依赖会中和
        OBSTRUCT 探针预报 → 池零收缩死循环(v0.5.1 活锁复辟)。
        门禁: OBSTRUCT(池不变量)/VERIFY(隔离纯度)/DONE(终局)拒绝。
        冲突守卫: 钉禁闭包不得触碰钉启或冻结(拖死恒启用成员 = 矛盾);
        钉启者的支撑闭包不得含其余钉禁(缺依赖跑不起来)。违规整批拒绝。
        返回实际钉住的 jar 集(整单元成员); 空 = 无事发生
        (不在 W / 门禁 / 冲突 / 幂等重钉)。
        """
        # 门禁: 三相位拒绝(引擎侧强制, UI 侧另有提示双保险)
        if self.phase in (Phase.OBSTRUCT, Phase.VERIFY, Phase.DONE):
            return frozenset()
        # 输入过滤: 只认 W 域内成员, 提升到绑定单元(整单元钉扎)
        want: set[str] = set()
        for b in bases:
            if b in self.universe and b in self.graph.unit_of:
                want |= self.graph.unit_members(b)
        if not want:
            return frozenset()
        if enable:
            # 冲突守卫: 钉启者的支撑闭包含其余钉禁 → 字面矛盾
            if self.graph.support(want) & (self._pin_off - want):
                return frozenset()
            new_on = self._pin_on | frozenset(want)
            new_off = self._pin_off - frozenset(want)  # 替换: 摘旧钉禁
        else:
            # 冲突守卫: 钉禁闭包(含既有)触碰钉启(幸存者)或冻结 → 矛盾
            dead = self.graph.closure(self._pin_off | want,
                                      set(self.universe))
            if dead & ((self._pin_on - want) | self._frozen):
                return frozenset()
            new_off = self._pin_off | frozenset(want)
            new_on = self._pin_on - frozenset(want)  # 替换: 摘旧钉启
        if (new_on, new_off) == (self._pin_on, self._pin_off):
            return frozenset()  # 幂等重钉 = 空操作(不入史)
        self._pin_on, self._pin_off = new_on, new_off
        # 显式事件: 自由意志不可由状态推导, kind/方向/jars 入史供重放
        self.history.append(RoundRecord(
            plan=None, answer="", actual_disabled=frozenset(),
            kind="pin_on" if enable else "pin_off",
            jars=frozenset(want)))
        return frozenset(want)

    def pin_clear(self, bases: frozenset[str]) -> frozenset[str]:
        """撤销钉扎(双向摘除, 单元粒度): 回归引擎自由调度。

        门禁比新钉宽: VERIFY 放行(撤销可恢复隔离纯度 — 收敛与钉禁
        一致的结案可由此回到验证正路); OBSTRUCT(池不变量)/DONE 仍拒。
        返回实际摘除钉扎的 jar 集; 空 = 无事发生。
        """
        if self.phase in (Phase.OBSTRUCT, Phase.DONE):
            return frozenset()
        want: set[str] = set()
        for b in bases:
            if b in self.universe and b in self.graph.unit_of:
                want |= self.graph.unit_members(b)
        hit = (self._pin_on | self._pin_off) & frozenset(want)
        if not hit:
            return frozenset()
        self._pin_on -= hit
        self._pin_off -= hit
        # 显式事件: 同 freeze/unfreeze, 重放据此直接调本方法
        self.history.append(RoundRecord(
            plan=None, answer="", actual_disabled=frozenset(),
            kind="pin_clear", jars=frozenset(hit)))
        return frozenset(hit)

    # -------------------------------------------------- 钉扎内部工具(私有)

    def _pin_dead(self) -> frozenset[str]:
        """钉禁实际生效禁用集(钉禁 + 被闭包拖死的依赖者, W 域)。"""
        if not self._pin_off:
            return frozenset()
        return self.graph.closure(self._pin_off, set(self.universe))

    def _pin_overlay(self, target: frozenset[str]) -> frozenset[str]:
        """钉扎层: 计划提案出口的统一改写(v0.6, 五相位计划共用)。

        final = (target ∪ 钉启) − 钉禁闭包
        - 钉禁闭包扩张(被拖死的依赖者一并禁, D7 回读如实归算);
        - 钉启字面并入, 不抬升依赖(理由见 pin_toggle docstring);
        - frozen 已含于各相位 target, 此处不重复处理;
        - 冲突守卫保证闭包与钉启/冻结无交, 减法不误伤。
        无钉扎时原样返回(零开销短路)。
        """
        if not (self._pin_on or self._pin_off):
            return target
        return (target | self._pin_on) - self._pin_dead()

    def _pin_yield_to_frozen(self, new_jars: frozenset[str]) -> None:
        """冻结吞钉(v0.6): 摘除与冻结集冲突的全部钉禁根源。

        只摘冻结成员自身的钉会漏掉「拖死它的外部钉禁根源」(受害者被
        冻结: 根源仍钉禁 → 闭包覆盖冻结成员 → overlay 把它从启用侧
        减掉 → 冻结永不生效 → 不可测死循环)。
        闭包对并集分配(closure(S) = 各单点闭包之并): 逐根源检查
        closure({r}) 与冻结集之交, 摘净后 frozen 与 closure(pin_off)
        无交, overlay 不再误伤冻结成员。
        """
        if not self._pin_off:
            return
        kill = {r for r in self._pin_off
                if self.graph.closure({r}, set(self.universe)) & new_jars}
        if kill:
            self._pin_off = self._pin_off - frozenset(kill)

    def _verify_entry_guard(self) -> bool:
        """VERIFY 入口钉禁守卫: 唯一嫌疑处于钉禁(闭包)侧时结案。

        隔离验证的前提 = 嫌疑在启用侧单独复现; 嫌疑被钉禁时验证轮
        反而"禁着嫌疑测", 复现/不复现都与嫌疑无关(反向误归因)。
        直接以「收敛与钉禁一致」结案: 收敛证据完整, 缺隔离确认。
        返回 True = 已结案(DONE), 调用方停止推进。
        """
        if len(self._suspect_units) != 1:
            return False
        unit_jars = self._single_suspect_unit()
        if not (unit_jars & self._pin_dead()):
            return False
        self.verdict = Verdict(
            title="收敛与钉禁一致(未隔离验证)",
            detail="嫌疑已收敛到唯一单元, 但该单元处于钉禁(闭包)侧, "
                   "隔离验证无意义(验证轮会禁着嫌疑测)。\n"
                   "嫌疑 mod: " + ", ".join(sorted(unit_jars))
                   + "\n建议: 撤销相应钉扎后重新排查以获得隔离验证, "
                     "或直接人工确认。",
            culprit=unit_jars)
        self.phase = Phase.DONE
        return True

    def report(self, answer: Answer,
               actual_disabled: frozenset[str],
               crashed: bool = False) -> Action:
        """消费一轮结果,推进状态机。

        actual_disabled 契约: W 域内本轮实际处于禁用态的 jar 集
        (执行层 diff 完成后回读磁盘的回报,含闭包拖拽扩大)。
        crashed = 本轮检测到游戏崩溃(信号无效,本轮作废重测)。
        UNTESTED(此次未测试) = 本轮无法观察 -> 回退并进可测性子二分
        (v0.5 阻碍子流程, 归算见 _report_untested/_report_obstruct)。
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

        # v0.5 阻碍子流程分发(归档后: 阻碍轮入 history, 重放据此重建冻结与子流程态)
        if self.phase is Phase.OBSTRUCT:
            if answer not in (Answer.TESTABLE, Answer.UNTESTABLE):
                return Action.RETEST_SAME  # 防御: 子流程只收可测性答案
            return self._report_obstruct(answer, actual_disabled)
        if answer in (Answer.TESTABLE, Answer.UNTESTABLE):
            return Action.RETEST_SAME  # 防御: 可测性答案只在子流程合法
        if answer is Answer.UNTESTED:
            # 此次未测试 -> 回退本轮, 子二分定位阻碍后回主流程
            return self._report_untested(actual_disabled)
        if self.phase is Phase.BASELINE:
            return self._report_baseline(answer)
        if self.phase is Phase.SCAN:
            return self._report_scan(answer, actual_disabled)
        if self.phase is Phase.VERIFY:
            return self._report_verify(answer)
        return self._report_bisect(answer, actual_disabled)

    # -------------------------------------------------- 各阶段归算(私有)

    def _report_baseline(self, answer: Answer) -> Action:
        if answer is Answer.ABSENT:
            if self._pin_off:
                # v0.6: 钉禁在场时"全启用未复现"另有解释 — 罪魁可能就在
                # 被钉禁的闭包里(用户钉禁 = 提前下了怀疑判断)
                self.verdict = Verdict(
                    title="基准未复现(存在钉禁)",
                    detail="除钉禁集外全部启用仍未复现问题。罪魁可能就在"
                           "被钉禁的 mod 中: "
                           + ", ".join(sorted(self._pin_dead()))
                           + "\n建议: 撤销相应钉扎后重新排查, "
                             "或人工核查钉禁集。",
                    culprit=self._pin_dead())
                self.phase = Phase.DONE
                return Action.DONE
            self.verdict = Verdict(
                title="基准轮未复现",
                detail="当前 mod 状态下 bug 不存在。可能: bug 是间歇性的 / "
                       "由资源包·光影·存档·网络等非 mod 因素引起 / 观察条件变化。\n"
                       "建议: 确认稳定复现条件后再重新开始排查。")
            self.phase = Phase.DONE
            return Action.DONE
        # PRESENT → 进扫描/二分;SKIP → 信任用户前提直接进
        self.phase = (Phase.SCAN if self.scan_spec is not None
                      else Phase.BISECT)
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
            if self._frozen:
                # 嫌疑排空且存在冻结: 罪魁大概率在被冻结的阻碍集中
                self.verdict = Verdict(
                    title="嫌疑排空(存在冻结)",
                    detail="嫌疑集归算为空, 且此前有 mod 因破坏可测性被"
                           "冻结: 罪魁可能就在冻结集 "
                           + ", ".join(sorted(self._frozen))
                           + " 中(恒启用钉死, 未参与二分)。\n"
                           "建议: 人工核查上述 mod, 或核实观察可靠性后"
                           "重新排查。")
            else:
                self.verdict = Verdict(
                    title="信号矛盾",
                    detail="嫌疑集被归算为空: 各轮回答互相矛盾,或依赖图与"
                           "实际加载行为不符。建议核实观察可靠性后重新排查。")
            self.phase = Phase.DONE
            return Action.DONE
        if len(after) == len(before):
            # 归算未缩小: 闭包吞掉整轮分割 → 二分在此图上失效,转人工
            self._suspect_units = after
            # v0.6: 嫌疑全部钉在同侧时"不缩小"其实是收敛证据(非退化):
            # 全钉禁 = 每轮禁用侧恒含全部嫌疑(ABSENT 无法分离);
            # 全钉启 = 每轮启用侧恒含全部嫌疑(PRESENT 无法排除)
            sus = self.suspects
            if sus and sus <= self._pin_dead():
                self.verdict = Verdict(
                    title="收敛至钉禁闭包",
                    detail="剩余嫌疑全部位于钉禁(闭包)侧, 二分无法再分离: "
                           + ", ".join(sorted(sus))
                           + "\n建议: 人工确认, 或调整钉扎后继续排查。",
                    culprit=sus)
                self.phase = Phase.DONE
                return Action.DONE
            if sus and sus <= self._pin_on:
                self.verdict = Verdict(
                    title="收敛至钉启集",
                    detail="剩余嫌疑全部被钉启(恒启用侧), 无法通过禁用"
                           "二分排除: " + ", ".join(sorted(sus))
                           + "\n建议: 撤销部分钉启后继续, 或人工确认。",
                    culprit=sus)
                self.phase = Phase.DONE
                return Action.DONE
            self.verdict = Verdict(
                title="二分退化",
                detail="依赖闭包吞掉了本轮分割(禁一半会拖死另一半),"
                       "无法继续有效二分。剩余嫌疑见列表。\n"
                       "建议: 人工排查,或等待后续版本的自由开关记录模式。")
            self.phase = Phase.DONE
            return Action.DONE
        self._suspect_units = after
        return self._enter_next_phase_or_verify()

    def _report_scan(self, answer: Answer,
                     actual_disabled: frozenset[str]) -> Action:
        """卷帘轮归算(v0.4): 四组合统一公式, 命中即锁段转二分。

        记账以执行层回报的实际集为准(D7 教义):
        - 禁用方向: 实际禁用集 = 帘带+闭包拖拽。还在 → 被禁嫌疑出局
          (继续卷); 消失 → 嫌疑收缩进实际被禁集(锁段)。
        - 启用方向: 实际启用集 = W − 实际禁用。还在 → 嫌疑收缩进
          实际启用集(含支撑闭包提前上场者, 锁段); 消失 → 帘带出局
          (继续卷)。
        锁定 = 嫌疑收缩 + phase 切 BISECT(引擎后半段无感知, 单单元
        自动转 VERIFY); 卷尽未命中 → 信号矛盾同型结案。
        """
        spec = self.scan_spec
        hit_dis = {self.graph.unit_of[j] for j in actual_disabled
                   if j in self.graph.unit_of}
        hit_en = {self.graph.unit_of[j]
                  for j in self.universe - actual_disabled
                  if j in self.graph.unit_of}
        before = self._suspect_units
        if spec.enable:
            locked = answer is Answer.PRESENT
            after = (before & hit_en if locked else before - hit_en)
        else:
            locked = answer is not Answer.PRESENT
            after = (before & hit_dis if locked else before - hit_dis)
        self._round_index += 1
        # v0.5: 指针推进到本帘带消耗终点(含跨过冻结位, 与计划同源)
        _, next_pos = self._scan_band()
        self._scan_pos = next_pos

        if not after:
            # 卷尽未命中: 罪魁不在 W(前提/观察失真), 与信号矛盾同型
            self._suspect_units = after
            pin_note = ""
            if self._pin_off:
                # v0.6: 钉禁侧未参与启用方向卷帘, 给出显式提示
                pin_note = ("\n注意: 存在钉禁("
                            + ", ".join(sorted(self._pin_off))
                            + "), 罪魁也可能在被钉禁的 mod 中。")
            self.verdict = Verdict(
                title="卷帘未命中",
                detail="锁序全部卷完仍未能定位罪魁: 回答链与前提矛盾,"
                       "或罪魁在初始禁用集/依赖图外/被冻结的阻碍集中。\n"
                       "建议: 核实问题复现条件后重新排查。" + pin_note)
            self.phase = Phase.DONE
            return Action.DONE
        self._suspect_units = after
        if locked:
            self.phase = Phase.BISECT  # 锁段: 后续沿用二分收敛
            return self._enter_next_phase_or_verify()
        # v0.6: 帘带耗尽而嫌疑未空 → 幸存者必为钉扎成员(_scan_band 永久
        # 跳过它们, 永不进 hit); 空帘带计划会原地重测永不推进 → 收敛结案
        band_next, _ = self._scan_band()
        if not band_next:
            sus = self.suspects
            if sus <= self._pin_dead():
                self.verdict = Verdict(
                    title="收敛至钉禁闭包",
                    detail="卷帘锁序已尽, 剩余嫌疑全部位于钉禁(闭包)侧: "
                           + ", ".join(sorted(sus))
                           + "\n建议: 人工确认, 或撤销钉扎后继续排查。",
                    culprit=sus)
            elif sus <= self._pin_on:
                self.verdict = Verdict(
                    title="收敛至钉启集",
                    detail="卷帘锁序已尽, 剩余嫌疑全部被钉启(恒启用侧): "
                           + ", ".join(sorted(sus))
                           + "\n建议: 撤销部分钉启后继续, 或人工确认。",
                    culprit=sus)
            else:
                # 防御(理论不可达: 幸存者必属钉扎集): 按未命中处理防空转
                self.verdict = Verdict(
                    title="卷帘未命中",
                    detail="锁序卷完仍有嫌疑无法归置: 回答链与前提矛盾。\n"
                           "建议: 核实观察可靠性后重新排查。")
            self.phase = Phase.DONE
            return Action.DONE
        return Action.NEXT_PLAN

    def _report_untested(self, actual_disabled: frozenset[str]) -> Action:
        """此次未测试 -> 回退本轮 + 可测性子二分入口(v0.5).

        数学: 单调阻碍假设(禁任一阻碍 mod 即破坏可测性)下,
        本轮实际禁用集 ⊆ 某可测配置 ⟹ 本轮应可测; 故结构性不可测
        必有新增禁用增量(候选池非空):
        - 池空 -> 偶发/外部因素 -> 同计划重测(不冻结不回退);
        - 池非空 -> 阻碍 ∈ 池 -> 进 OBSTRUCT 子二分(基准=最近可测配置)
        """
        # A0 = 最近一次真实作答(present/absent)轮的实际禁用集; 无则全启
        base_actual: frozenset[str] = frozenset()
        for rec in reversed(self.history[:-1]):  # 不含刚记录的本轮
            if rec.answer in (Answer.PRESENT.value, Answer.ABSENT.value):
                base_actual = rec.actual_disabled
                break
        pool_jars = ((frozenset(actual_disabled) - base_actual)
                     - self._frozen)
        # v0.6: 钉禁在场时不提前结案 — 重测会带钉重开基准轮,
        # 给「钉掉问题 mod 后恢复可测」留通路(钉启不改变全启
        # 盘面, 无此待遇)
        if (not pool_jars and self.phase is Phase.BASELINE
                and not self._pin_off):
            # 全启用(无可开关增量)即不可测: 排查前提不成立
            self.verdict = Verdict(
                title="基准轮无法测试",
                detail="全部 mod 启用状态下即无法启动游戏或观察问题 — "
                       "排查前提不成立(可能是安装损坏或非 mod 因素)。\n"
                       "建议: 先修复游戏启动问题, 再重新开始排查。")
            self.phase = Phase.DONE
            return Action.DONE
        if not pool_jars:
            # 单调结构下本轮 ⊆ 最近可测配置: 不可测属偶发/外部 -> 重测
            return Action.RETEST_SAME
        # v0.6 钉扎-基准一致性守卫: 可测性子二分的探针配置恒过
        # overlay, 基准(最近可测配置)必须仍可达, 否则归因被钉扎翻转
        # 污染: 钉禁闭包有成员不在基准禁用集 = 每个探针都额外禁它;
        # 钉启成员在基准中是被禁态 = 每个探针都额外启它(单调前提破坏)。
        # 任一方向的翻转都无法与探针增量隔离 -> 子二分不可信 -> 结案
        # 指路(防: 全钉池探针非法断言崩溃/归因零收缩活锁/冻无辜单元)。
        bad_pins = ((self._pin_dead() - base_actual)
                    | (self._pin_on & base_actual))
        if bad_pins:
            self.verdict = Verdict(
                title="不可测与钉扎冲突",
                detail="本轮无法测试, 且当前钉扎与最近可测配置冲突"
                       "(钉禁/钉启改写了基准可达态), 可测性子二分"
                       "无法隔离归因。\n冲突钉扎: "
                       + ", ".join(sorted(bad_pins))
                       + "\n建议: 撤销相应钉扎后重新排查, 或人工核查。",
                culprit=bad_pins)
            self.phase = Phase.DONE
            return Action.DONE
        pool = frozenset(self.graph.unit_of[j] for j in pool_jars
                         if j in self.graph.unit_of)
        self._obstruct = _ObstructState(
            pool=pool, base_actual=base_actual, return_phase=self.phase)
        self._round_index += 1
        self.phase = Phase.OBSTRUCT
        return Action.NEXT_PLAN

    def _report_obstruct(self, answer: Answer,
                         actual_disabled: frozenset[str]) -> Action:
        """可测性子二分归算(v0.5): 收缩候选池, 剩一冻结回主流程.

        hit = 本子轮实际被禁单元(含闭包扩张, 不含钉启的冻结集):
        TESTABLE -> 池剔除 hit(阻碍不在被禁侧);
        UNTESTABLE -> 池收缩至 hit 交集(阻碍在被禁侧);
        |池| = 1 -> 该单元即阻碍: 冻结(恒钉启用 + 双剔除)后回原相位.
        """
        ob = self._obstruct
        assert ob is not None
        hit = frozenset(self.graph.unit_of[j] for j in actual_disabled
                        if j in self.graph.unit_of)
        if answer is Answer.TESTABLE:
            after = ob.pool - hit
        else:
            after = ob.pool & hit
        self._round_index += 1

        if not after:
            # 全部候选曾被禁却可测: 与阻碍前提矛盾(观察不稳定/外部)
            self._obstruct = None
            self.verdict = Verdict(
                title="可测性信号矛盾",
                detail="阻碍候选全部被禁用后反而可测, 与「存在阻碍」"
                       "前提矛盾 — 观察可能不稳定, 或受非 mod 因素"
                       "干扰。\n建议: 核实测试条件后重新开始排查。")
            self.phase = Phase.DONE
            return Action.DONE
        if len(after) == 1:
            # 唯一候选 = 阻碍单元: 冻结(钉启用) + 双剔除, 回主流程
            idx = next(iter(after))
            # v0.5.2 嫌疑池记账: 自动冻结同样入账, 手动解冻可对称恢复
            if idx in self._suspect_units:
                self._frozen_was_suspect.add(idx)
            self._frozen = self._frozen | self.graph.units[idx]
            # v0.6: 冻结吞钉(恒启用语义优先): 钉启冗余摘除; 钉禁侧
            # 连根拔(见 _pin_yield_to_frozen — 只摘冻结成员自身的钉,
            # 会漏掉拖死它的外部根源 → overlay 仍禁着冻结成员 → 死循环)
            self._pin_on -= self.graph.units[idx]
            self._pin_yield_to_frozen(self.graph.units[idx])
            self._obstruct = None
            after_s = self._suspect_units - {idx}
            self._suspect_units = after_s
            self.phase = ob.return_phase
            if not after_s:
                # 嫌疑全部被冻结排除: 罪魁可能就在冻结集(用户自担)
                self.verdict = Verdict(
                    title="嫌疑被冻结排空",
                    detail="全部嫌疑 mod 均作为启动阻碍被冻结排除。"
                           "罪魁可能就在冻结集: "
                           + ", ".join(sorted(self._frozen))
                           + "\n(冻结 = 恒启用钉死, 不参与开关二分)\n"
                           "建议: 人工核查上述 mod, 或核实复现条件后"
                           "重新排查。")
                self.phase = Phase.DONE
                return Action.DONE
            return self._enter_next_phase_or_verify()
        ob.pool = after
        return Action.NEXT_PLAN

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
        """S 收缩后判定是否已收敛到单单元(是则直接进验证轮)。

        v0.6: 唯一嫌疑处于钉禁(闭包)侧 → 隔离验证无意义(验证轮会禁着
        嫌疑测, 复现与否都与嫌疑无关) → 守卫结案(收敛证据保留)。
        """
        if len(self._suspect_units) == 1:
            if self._verify_entry_guard():
                return Action.DONE
            self.phase = Phase.VERIFY
        return Action.NEXT_PLAN

    def _single_suspect_unit(self) -> frozenset[str]:
        """VERIFY 阶段的嫌疑单元成员集(此时 S 恰有一个单元)。"""
        idx = next(iter(self._suspect_units))
        return self.graph.units[idx]

    def _scan_band(self) -> tuple[list[str], int]:
        """从当前指针取下一帘带(v0.5: 跳过冻结; v0.6: 跳过钉扎), 返回(帘带, 消耗后指针).

        冻结/钉扎 mod 恒定启停不可卷, 指针越过即视为已消耗(不再出现在
        任何帘带); 指针推进到帘带最后成员之后(含跨过的恒定位), 计划推导
        与 report 推进共用本函数保证同源.
        """
        spec = self.scan_spec
        assert spec is not None  # 仅 SCAN 相位调用
        pos = self._scan_pos
        band: list[str] = []
        last = pos
        while pos < len(spec.order):
            name = spec.order[pos]
            pos += 1
            # v0.6: 钉扎成员(两向)与冻结同样不消耗帘带(恒启停, 卷=空转轮)
            if name not in self._frozen and name not in self.pinned:
                band.append(name)
                last = pos
                if len(band) >= spec.chunk:
                    break
        return band, (last if band else pos)
