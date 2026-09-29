# SPDX-License-Identifier: MPL-2.0
"""依赖面板: 双击表格条目后展开的"关系画框"(用户 v0.2 需求 4)。

独立 QFrame 金边画框(与表格视觉分离), 内容两区:
- 上半区: 依赖它的(禁用父条目会拖死的)
- 下半区: 它依赖的(按提供者 jar 聚合)
每个条目右侧提醒"超出父关系"的额外关系:
- 该模组还依赖于: X, Y(它还需要别的)
- 该模组还被其他模组依赖: A, B(别的还靠着它)
→ 提醒用户: 动这个条目不止影响父条目一条边。

计算全部是 depgraph 只读查询(不改图); 面板只做展示,
关闭信号交 UI 层收起。禁用 mod 不在图内(扫描时出局) → 显示说明。
"""
from __future__ import annotations

from html import escape as _esc

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..depgraph import DependencyGraph
from ..model import JarInfo
from ..sortkey import name_key
from .style import BG_PANEL, GOLD, GREEN, ORANGE, RED, TEXT_DIM


class DepPanel(QFrame):
    """单条 jar 的依赖关系画框(UI 层持有, 双击表格条目时唤出)。"""

    closed = Signal()  # 用户点收起 → UI 层隐藏本面板

    def __init__(self, graph: DependencyGraph, jars: list[JarInfo],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._graph = graph
        self._jars = {j.base_name: j for j in jars}
        self._base = ""
        self.setVisible(False)  # 默认隐藏

        self.setObjectName("DepPanel")
        self.setStyleSheet(
            f"QFrame#DepPanel {{ background: {BG_PANEL}; "
            f"border: 1px solid {GOLD}; }}")
        self.setMaximumHeight(240)  # 画框限高, 内容超出走滚动

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        head = QHBoxLayout()
        self._title = QLabel("")
        self._title.setStyleSheet(f"color: {GOLD}; font-weight: bold;")
        head.addWidget(self._title, stretch=1)
        btn = QPushButton("收起")
        btn.clicked.connect(self._on_close)
        head.addWidget(btn)
        root.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self._scroll, stretch=1)

    # ---------------------------------------------------------------- 对外

    def show_for(self, base: str) -> None:
        """渲染并展示指定 jar 的关系画框。"""
        self._base = base
        self._render()
        self.show()
        self.raise_()

    def refresh(self) -> None:
        """启停状态变化后重绘(仅可见时有意义, UI 刷新表格时顺带调)。"""
        if self._base and self.isVisible():
            self._render()

    # ---------------------------------------------------------------- 事件

    def _on_close(self) -> None:
        self.setVisible(False)
        self.closed.emit()

    # ---------------------------------------------------------------- 只读查询

    def _provided(self, base: str) -> set[str]:
        jar = self._jars.get(base)
        return {m.modid for m in jar.mods} if jar else set()

    def _dependents_of(self, base: str, exclude: set[str]) -> list[str]:
        """依赖 base 所提供 modid 的 jar 集(排除自身与 exclude), 拼音序。"""
        prov = self._provided(base)
        out = [j for j, deps in self._graph.jar_deps.items()
               if j != base and j not in exclude and deps & prov]
        return sorted(out, key=name_key)

    # ---------------------------------------------------------------- 渲染

    def _render(self) -> None:
        base = self._base
        jar = self._jars.get(base)
        if jar is None:
            return
        self._title.setText(f"依赖关系 — {_esc(jar.label)}")

        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(2, 2, 2, 2)
        if base not in self._graph.jar_deps:
            lay.addWidget(self._row(
                "该 mod 在扫描时刻处于禁用状态, 未参与依赖图构建(无关系数据)。"))
        else:
            self._render_dependents(lay, base)
            self._render_dependencies(lay, base)
        lay.addStretch(1)

        old = self._scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self._scroll.setWidget(content)

    def _render_dependents(self, lay: QVBoxLayout, base: str) -> None:
        """上半区: 依赖它的。"""
        deps_on_parent = self._dependents_of(base, exclude=set())
        title = QLabel(f"依赖它的({len(deps_on_parent)}) — "
                       f"禁用它会把下面这些一起拖死:")
        title.setStyleSheet(f"color: {GOLD};")
        lay.addWidget(title)
        if not deps_on_parent:
            lay.addWidget(self._row("  无"))
            return
        for d in deps_on_parent:
            lay.addWidget(self._row(self._entry_html(d)
                                    + self._reminders_html(d, base)))

    def _render_dependencies(self, lay: QVBoxLayout, base: str) -> None:
        """下半区: 它依赖的(提供者 jar 聚合, 一个 jar 可供多个 id)。"""
        my_deps = sorted(self._graph.jar_deps.get(base, ()))
        supplied: dict[str, list[str]] = {}
        for mid in my_deps:
            for p in sorted(self._graph.providers.get(mid, ())):
                supplied.setdefault(p, []).append(mid)
        title = QLabel(f"它依赖的(需要 {len(my_deps)} 个 modid, "
                       f"由 {len(supplied)} 个 mod 提供):")
        title.setStyleSheet(f"color: {GOLD};")
        lay.addWidget(title)
        if not supplied:
            lay.addWidget(self._row("  无"))
        for p in sorted(supplied, key=name_key):
            html = (self._entry_html(p)
                    + f' <span style="color:{TEXT_DIM}">提供: '
                    + _esc(", ".join(sorted(supplied[p]))) + "</span>"
                    + self._reminders_html(p, base))
            lay.addWidget(self._row(html))
        # 无法解析的依赖(missing → 修补系统的输入, 与需求 8 打通)
        miss = sorted(m for b, m in self._graph.missing if b == base)
        if miss:
            lay.addWidget(self._row(
                f'<span style="color:{ORANGE}">无法解析的依赖'
                f"(目录内无人提供, 可用修补系统处理): "
                + _esc(", ".join(miss)) + "</span>"))

    def _entry_html(self, base: str) -> str:
        """条目行: 名称 + 状态(状态着色)。"""
        jar = self._jars.get(base)
        if jar is None:
            return _esc(base)
        state = "启用" if jar.enabled else "禁用"
        color = GREEN if jar.enabled else RED
        return (f"<b>{_esc(jar.label)}</b> "
                f'<span style="color:{color}">[{state}]</span>')

    def _reminders_html(self, entry_base: str, parent_base: str) -> str:
        """条目右侧的"超出父关系"提醒(非空才显示, 需求 4 的扩展)。"""
        prov_parent = self._provided(parent_base)
        other_deps = sorted(
            self._graph.jar_deps.get(entry_base, ()) - prov_parent)
        others = [j for j in self._dependents_of(entry_base, exclude={parent_base})
                  if j != entry_base]
        parts = []
        if other_deps:
            parts.append(f'<span style="color:{ORANGE}">该模组还依赖于: '
                         + _esc(", ".join(other_deps)) + "</span>")
        if others:
            labels = [_esc(self._jars[j].label) if j in self._jars else _esc(j)
                      for j in others]
            parts.append(f'<span style="color:{ORANGE}">该模组还被其他模组依赖: '
                         + ", ".join(labels) + "</span>")
        return ("<br>" + "<br>".join(parts)) if parts else ""

    def _row(self, html: str) -> QLabel:
        """富文本行(自动换行)。"""
        lab = QLabel(html)
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.TextFormat.RichText)
        return lab