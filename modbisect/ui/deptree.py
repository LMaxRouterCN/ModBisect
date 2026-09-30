# SPDX-License-Identifier: MPL-2.0
"""左侧依赖树面板(用户 v0.3 需求: 窗口左侧, 选中模组的依赖关系树形图)。

双树渲染(单击表格行联动, 与双击操作互不干扰):
- 上半树「依赖它的」(传递): 禁用选中 jar 会拖死的完整链条 = 级联预览
- 下半树「它依赖的」(传递): 选中 jar 的 mandatory 需求链,
  缺失依赖(目录内无人提供)为橙色叶子 — 与修补系统语义打通

环防 = 路径栈剪枝: 当前递归路径上再次出现的节点 → 追加「（环路）」
灰显且不再展开; 菱形结构(不同路径到达同一节点)保留 — 信息完整优先于去重。
深度防御: 传递链超过 MAX_DEPTH 层截断(病态长链防爆栈)。

数据全部为 depgraph 只读查询; 面板自建反向邻接(不依赖 app 层缓存)。
禁用 mod 不在图内(扫描时刻出局, 无关系数据) → hint 文案说明。
样式沿用 DepPanel 模式: objectName + 局部 setStyleSheet(不进 MAIN_QSS)。
"""
from __future__ import annotations

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QLabel, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..depgraph import DependencyGraph
from ..model import JarInfo
from ..sortkey import name_key
from .style import BG_PANEL, GOLD, GOLD_DIM_BG, GREEN, ORANGE, RED, TEXT_DIM

_CYCLE = "（环路）"        # 环路标记(路径栈剪枝的产物)
_TRUNC = "（更深…已截断）"  # 深度防御标记
MAX_DEPTH = 32             # 传递链渲染深度上限(正常整合包 < 10, 防爆栈)


class DepTreePanel(QWidget):
    """左侧常驻面板: 单击表格行 → show_for(base) 联动渲染双树。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._graph: DependencyGraph | None = None
        self._jars: dict[str, JarInfo] = {}
        self._rev: dict[str, set[str]] = {}  # base → 直接依赖它的 base 集
        self._base = ""                      # 当前渲染的根 jar(空 = 空态)

        self.setMinimumWidth(200)
        self.setMaximumWidth(320)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        self._title = QLabel("依赖树")
        self._title.setStyleSheet(f"color: {GOLD}; font-weight: bold;")
        root.addWidget(self._title)

        self._hint = QLabel("扫描后单击表格行, 此处展示它的依赖树")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color: {TEXT_DIM};")
        root.addWidget(self._hint)

        # 上树: 依赖它的(传递) — 禁用即拖死
        self._up_tree = QTreeWidget()
        self._up_tree.setHeaderLabel("依赖它的（传递）— 禁用即拖死")
        self._up_tree.setObjectName("depTreeUp")
        root.addWidget(self._up_tree, stretch=1)

        # 下树: 它依赖的(传递) — 缺失示橙
        self._down_tree = QTreeWidget()
        self._down_tree.setHeaderLabel("它依赖的（传递）— 缺失示橙")
        self._down_tree.setObjectName("depTreeDown")
        root.addWidget(self._down_tree, stretch=1)

        self._apply_qss()

    def _apply_qss(self) -> None:
        """局部样式(与 DepPanel 同模式: 不进 MAIN_QSS, 零全局污染)。"""
        tree_qss = (
            f"QTreeWidget {{ background: {BG_PANEL}; "
            f"border: 1px solid {GOLD}; }}"
            "QTreeWidget::item { height: 22px; padding: 1px 2px; }"
            "QTreeWidget::item:hover, QTreeWidget::item:selected "
            f"{{ background: {GOLD_DIM_BG}; }}")
        self._up_tree.setStyleSheet(tree_qss)
        self._down_tree.setStyleSheet(tree_qss)

    # ---------------------------------------------------------------- 对外

    def set_graph(self, graph: DependencyGraph | None,
                  jars: list[JarInfo]) -> None:
        """扫描完成/会话恢复后换血: 重建反向邻接, 回空态等选中。"""
        self._graph = graph
        self._jars = {j.base_name: j for j in jars}
        self._rev = {}
        if graph is not None:
            # base ← 依赖它的: 正向 deps 经 providers 折回 jar 粒度
            for b, deps in graph.jar_deps.items():
                for mid in deps:
                    for p in graph.providers.get(mid, ()):
                        if p != b:  # 跳过 jar 内部自引用
                            self._rev.setdefault(p, set()).add(b)
        self._base = ""
        self._clear()

    def show_for(self, base: str) -> None:
        """表格单击选中行 → 渲染该 jar 的双树(只读, 不碰状态机)。"""
        if self._graph is None or base not in self._jars:
            self._base = ""
            self._clear()
            return
        self._base = base
        self._render()

    def refresh(self) -> None:
        """启停状态变化后重绘(与 DepPanel.refresh 同语义, UI 刷表时顺带调)。"""
        if self._base and self._graph is not None:
            self._render()

    # ---------------------------------------------------------------- 渲染

    def _clear(self) -> None:
        self._title.setText("依赖树")
        self._hint.setText("扫描后单击表格行, 此处展示它的依赖树")
        self._up_tree.clear()
        self._down_tree.clear()

    def _render(self) -> None:
        base = self._base
        jar = self._jars.get(base)
        if jar is None:
            self._clear()
            return
        self._up_tree.clear()
        self._down_tree.clear()
        self._title.setText(f"依赖树 — {jar.label}")

        if base not in self._graph.jar_deps:
            # 禁用 jar: 扫描时刻出局, 不在图内(与 DepPanel 同文案语义)
            self._hint.setText(
                "该 mod 在扫描时刻处于禁用状态, 未参与依赖图构建(无关系数据)。")
            return
        self._hint.setText("")

        # ---- 上树: 依赖它的(传递) ----
        direct = self._rev.get(base, set())
        if direct:
            for d in sorted(direct, key=name_key):
                item = self._node(d)
                self._up_tree.addTopLevelItem(item)
                item.setExpanded(True)  # 默认展开一级(深层手动展开)
                self._expand_up(item, d, {base, d})
        else:
            self._up_tree.addTopLevelItem(
                QTreeWidgetItem(["无（没人依赖它）"]))

        # ---- 下树: 它依赖的(经 provider, 传递) ----
        deps = self._graph.jar_deps.get(base, set())
        if deps or self._missing_of(base):
            for mid in sorted(deps):
                self._add_dep(None, mid, {base})
            for mid in self._missing_of(base):
                leaf = self._missing_node(mid)
                self._down_tree.addTopLevelItem(leaf)
        else:
            self._down_tree.addTopLevelItem(
                QTreeWidgetItem(["无（不依赖任何 mod）"]))

    # ---------------------------------------------------------------- 递归

    def _expand_up(self, item: QTreeWidgetItem, base: str,
                   path: set[str]) -> None:
        """上树节点展开: 依赖它的每个 jar 再递归(传递闭包方向)。"""
        if len(path) > MAX_DEPTH:
            item.addChild(QTreeWidgetItem([_TRUNC]))
            return
        for d in sorted(self._rev.get(base, ()), key=name_key):
            child = self._node(d)
            item.addChild(child)
            if d in path:
                self._mark_cycle(child)  # 路径栈剪枝: 环路不展开
            else:
                self._expand_up(child, d, path | {d})

    def _add_dep(self, parent_item: QTreeWidgetItem | None, mid: str,
                 path: set[str]) -> None:
        """下树递归单元: 一个依赖 modid → 提供者节点(多个提供者全展开)。"""
        providers = sorted(self._graph.providers.get(mid, ()))
        if not providers:
            self._attach(parent_item, self._missing_node(mid))
            return
        for p in providers:
            item = self._node(p, provide=mid)
            self._attach(parent_item, item)
            if parent_item is None:
                item.setExpanded(True)  # 顶级默认展开一级
            if p in path:
                self._mark_cycle(item)
            else:
                self._expand_down(item, p, path | {p})

    def _expand_down(self, item: QTreeWidgetItem, base: str,
                     path: set[str]) -> None:
        """下树节点展开: 它依赖的每个 modid 再递归 + 本层缺失叶子。

        缺失边在 depgraph 构图时已从 jar_deps 剔除(基线缺失边剔除),
        所以必须从 graph.missing 补挂 — 每层都查, 深层缺失不丢。
        """
        if len(path) > MAX_DEPTH:
            item.addChild(QTreeWidgetItem([_TRUNC]))
            return
        for mid in sorted(self._graph.jar_deps.get(base, ())):
            self._add_dep(item, mid, path)
        for mid in self._missing_of(base):
            item.addChild(self._missing_node(mid))

    # ---------------------------------------------------------------- 节点

    def _missing_of(self, base: str) -> list[str]:
        """该 jar 的缺失依赖 modid(排序稳定)。"""
        return sorted(m for b, m in self._graph.missing if b == base)

    def _missing_node(self, mid: str) -> QTreeWidgetItem:
        """缺失依赖叶子: 橙色(修补系统的输入)。"""
        leaf = QTreeWidgetItem([f"{mid}（缺失）"])
        leaf.setForeground(0, QBrush(QColor(ORANGE)))
        leaf.setToolTip(0, "目录内无人提供此 modid(可用修补系统处理)")
        return leaf

    def _node(self, base: str, provide: str | None = None) -> QTreeWidgetItem:
        """jar 节点: label + 启停状态着色; tooltip = 身份 + 提供 modid。"""
        j = self._jars.get(base)
        if j is None:
            return QTreeWidgetItem([base])
        state = "启用" if j.enabled else "禁用"
        item = QTreeWidgetItem([f"{j.label} [{state}]"])
        item.setForeground(0, QBrush(QColor(GREEN if j.enabled else RED)))
        tip = f"{base}\n提供: {', '.join(sorted(j.modids))}"
        if provide:
            tip = f"满足依赖: {provide}\n" + tip
        item.setToolTip(0, tip)
        return item

    def _mark_cycle(self, item: QTreeWidgetItem) -> None:
        """环防标记: 追加「（环路）」灰显(SCC 回边, 不再展开)。"""
        item.setText(0, item.text(0) + _CYCLE)
        item.setForeground(0, QBrush(QColor(TEXT_DIM)))

    def _attach(self, parent_item: QTreeWidgetItem | None,
                item: QTreeWidgetItem) -> None:
        """挂载: 无父 = 下树顶级, 有父 = 子级。"""
        if parent_item is None:
            self._down_tree.addTopLevelItem(item)
        else:
            parent_item.addChild(item)