# SPDX-License-Identifier: MPL-2.0
"""依赖图层: 提供者映射 / mandatory 闭包 / 绑定单元(SCC)。

三个核心产物:
1. providers: modid(小写归一) → 提供它的 jar base_name 集合
   (内嵌 jarjar mod 的 modid 计入其外层 jar)
2. closure(T): 在给定全集内, 从禁用集 T 出发沿 mandatory 依赖反向拖拽到不动点。
   基线即无人提供的依赖边(整合包既存的损坏状态)在构图时直接剔除 ——
   工具不修复也不放大既存问题, 否则每轮都会误拖正常运行的 mod
3. 绑定单元: "唯一提供者拖拽图"的 SCC。互为唯一强制依赖的 jar 物理不可分离
   (禁任何一个必然拖死全组), 二分永远不在单元内部下刀 ——
   切了闭包必吞掉分割, 白白烧掉一整轮游戏启动

图构建是纯静态过程(一次性); closure 是纯函数(每轮调用), 线程安全。
"""

from __future__ import annotations

from collections.abc import Iterator

from .model import JarInfo


class DependencyGraph:
    """mods 目录全量 jar 的依赖图。

    jar 身份 = JarInfo.base_name(扫描层已保证唯一)。
    """

    def __init__(self, jars: list[JarInfo], ignore_modids):
        ignore = {str(m).lower() for m in ignore_modids}
        self.universe: frozenset[str] = frozenset(j.base_name for j in jars)

        # --- 提供者映射: modid → jar 集合 ---
        providers: dict[str, set[str]] = {}
        for j in jars:
            for mod in j.mods:
                providers.setdefault(mod.modid, set()).add(j.base_name)
        self.providers: dict[str, set[str]] = providers

        # --- jar 级 mandatory 依赖集(含内嵌 mod 的依赖) ---
        self.warnings: list[str] = []
        # 结构化缺失依赖: [(jar base_name, 缺失 modid)] — 修补系统的输入(UI 按需消费)
        self.missing: list[tuple[str, str]] = []
        self.jar_deps: dict[str, frozenset[str]] = {}
        for j in jars:
            needed: set[str] = set()
            for mod in j.mods:
                for dep in mod.dependencies:
                    if dep.mandatory:
                        needed.add(dep.modid)
            kept: set[str] = set()
            for d in needed:
                if d in ignore:
                    continue  # minecraft/forge/javafml 等由加载器或游戏本体提供
                provs = providers.get(d, frozenset())
                if j.base_name in provs:
                    continue  # 本 jar 自给自足(含内嵌 mod 提供)
                if not provs:
                    # 基线即无人提供: 整合包既存状态, 该边不参与传播
                    self.missing.append((j.base_name, d))  # 交给修补系统
                    self.warnings.append(
                        f"{j.base_name}: 依赖 {d} 目录内无人提供(既存状态, 已忽略该边)")
                    continue
                kept.add(d)
            self.jar_deps[j.base_name] = frozenset(kept)

        # --- 拖拽图(唯一提供者)与 SCC 绑定单元 ---
        # 边 k → j: k 是 j 某依赖的唯一提供者 → k 死则 j 必陪葬
        drag: dict[str, set[str]] = {j.base_name: set() for j in jars}
        for j in jars:
            for d in self.jar_deps[j.base_name]:
                provs = providers.get(d, frozenset())
                if len(provs) == 1:
                    k = next(iter(provs))
                    if k != j.base_name:
                        drag[k].add(j.base_name)
        # 按单元内最小 base_name 排序, 保证输出确定性
        sccs = _tarjan_scc(sorted(drag), drag)
        self.units: list[frozenset[str]] = sorted(sccs, key=lambda u: min(u))
        self.unit_of: dict[str, int] = {}
        for i, u in enumerate(self.units):
            for name in u:
                self.unit_of[name] = i

    # ------------------------------------------------------------------

    @property
    def multi_units(self) -> list[frozenset[str]]:
        """多 jar 绑定单元(单 jar 单元无展示价值)。"""
        return [u for u in self.units if len(u) > 1]

    def unit_members(self, jar_name: str) -> frozenset[str]:
        """jar 所在绑定单元的全体成员(单 jar 单元返回自身)。"""
        return self.units[self.unit_of[jar_name]]

    def providers_of(self, modid: str) -> frozenset[str]:
        """某 modid 的提供者 jar 集合(UI 检索用)。"""
        return frozenset(self.providers.get(modid.lower(), ()))

    # ------------------------------------------------------------------

    def closure(self, disabled: set[str],
                universe: set[str] | None = None) -> frozenset[str]:
        """禁用集 T 在启用品全集内的实际生效禁用集(含依赖拖拽, 不动点)。

        universe 缺省 = 构图全集; 传入子集可支持子域推理。
        返回值 ⊇ T ∩ universe; 不修改入参。
        """
        u = self.universe if universe is None else frozenset(universe)
        dead = {d for d in disabled if d in u}
        alive = set(u) - dead
        # 反复扫描存活 jar: 某依赖在存活集中无人提供 → 该 jar 陪葬, 直至不动点
        changed = True
        while changed:
            changed = False
            for j in list(alive):
                for d in self.jar_deps.get(j, ()):
                    if not (self.providers.get(d, frozenset()) & alive):
                        dead.add(j)
                        alive.discard(j)
                        changed = True
                        break
        return frozenset(dead)


    def support(self, seeds: set[str]) -> frozenset[str]:
        """正向依赖支撑闭包: seeds 及其全部依赖链上的 jar。

        用于验证轮的最小启用集: 某依赖有多个提供者时全部启用
        (保证依赖必然满足 —— 多开只是复现条件略宽,
        缺开会让嫌疑 mod 加载失败,污染验证信号)。
        """
        alive: set[str] = set(seeds)
        frontier: list[str] = list(seeds)
        while frontier:
            j = frontier.pop()
            for d in self.jar_deps.get(j, ()):
                for p in self.providers.get(d, ()):
                    if p not in alive:
                        alive.add(p)
                        frontier.append(p)
        return frozenset(alive)


def _tarjan_scc(nodes: list[str],
                adj: dict[str, set[str]]) -> list[frozenset[str]]:
    """迭代版 Tarjan SCC(避免深递归炸栈, 千级节点安全)。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    out: list[frozenset[str]] = []

    for root in nodes:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        # 工作栈元素: (节点, 未耗尽的邻居迭代器)
        work: list[tuple[str, Iterator[str]]] = [(root, iter(adj.get(root, ())))]

        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index:
                    # 深入未访问邻居
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(adj.get(w, ()))))
                    pushed = True
                    break
                if w in on_stack:
                    # 回边/横边(栈内): 更新 low
                    if index[w] < low[v]:
                        low[v] = index[w]
            if pushed:
                continue
            # 邻居耗尽 → 回溯
            work.pop()
            if work:
                pv = work[-1][0]
                if low[v] < low[pv]:
                    low[pv] = low[v]
            if low[v] == index[v]:
                # v 是其 SCC 的根, 弹出整块
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(frozenset(comp))
    return out