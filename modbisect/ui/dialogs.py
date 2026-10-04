# SPDX-License-Identifier: MPL-2.0
"""UI 弹窗层: 快照选择 / 修补确认 / 终局报告。

职责边界: 只做"展示 → 收集选择"的原子交互, 不持有业务状态, 不做调度。
v0.2 变化: 轮次判决迁移到主窗口右侧按钮区(需求 7 — 判决全程可见,
不被模态弹窗遮挡), 模态弹窗只保留必须打断用户的决策:
快照选择 / 修补确认 / 终局报告, v0.7 新增复测导出设置
(干净实例根 + config 拷贝范围, 收敛三选一 RECHECK 分支入口)。
按钮文案自带因果链(D5: 消除"是的/没有"式指代歧义)。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog,
                               QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

from ..engine import Verdict
from .style import GOLD, GREEN, ORANGE


class SnapshotDialog(QDialog):
    """快照选择弹窗(恢复哪个时刻的 mod 启停状态)。

    结果读取: selected(str|None) — 点「恢复」且有选中项时为快照文件路径;
    取消 = None。items 形如 [{"path","created","mods_dir","count"}]
    (list_snapshots() 产出, 新→旧)。
    """

    def __init__(self, items: list[dict], parent: QWidget | None = None):
        super().__init__(parent)
        self.selected: str | None = None
        self.setWindowTitle("恢复快照")
        self.setModal(True)
        root = QVBoxLayout(self)

        root.addWidget(QLabel("选择要恢复的 mod 状态快照(新 → 旧):"))

        self._list = QListWidget()
        for it in items:
            row = QListWidgetItem(
                f"{it.get('created', '?')} | {it.get('mods_dir', '?')} | "
                f"{it.get('count', 0)} 个 mod")
            row.setData(Qt.ItemDataRole.UserRole, it.get("path"))
            row.setToolTip(str(it.get("path", "")))
            self._list.addItem(row)
        self._list.setCurrentRow(0)
        root.addWidget(self._list, stretch=1)

        btns = QHBoxLayout()
        b_ok = QPushButton("恢复此快照")
        b_ok.clicked.connect(self._on_ok)
        btns.addWidget(b_ok)
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_cancel)
        root.addLayout(btns)

    def _on_ok(self) -> None:
        cur = self._list.currentItem()
        if cur is not None:
            self.selected = cur.data(Qt.ItemDataRole.UserRole)
        self.accept()  # selected 仍 None(空列表/无选中)时调用方按 None 处理


class RepairDialog(QDialog):
    """修补确认弹窗: 缺失依赖的文件名匹配结果交用户裁决(需求 8)。

    结果读取: choice(str|None) — 「修补」= 选中候选 jar 的 base_name;
    「忽视」/关闭 = None(调用方记录, 避免每次重扫重复追问)。
    matches: [(候选 base_name, 展示名, 该 jar 当前 modId)]。
    """

    def __init__(self, declaring_label: str, missing_modid: str,
                 matches: list[tuple[str, str, str]], layer: str,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.choice: str | None = None
        self.setWindowTitle("修补确认 — 依赖缺失")
        self.setModal(True)
        self.resize(560, 320)
        root = QVBoxLayout(self)

        root.addWidget(QLabel(
            f"「{declaring_label}」声明依赖 modid「{missing_modid}」,"
            "但 mods 目录内没有 mod 提供它。"))
        how = ("文件名精确匹配" if layer == "exact"
               else "文件名前缀模糊匹配" if layer == "prefix"
               else "文件名匹配")
        root.addWidget(QLabel(
            f"按{how}, 以下 jar 可能就是该依赖的提供者 —"
            " 作者可能把 mods.toml 里的 modId 写错了:"))

        self._list = QListWidget()
        for base, label, current in matches:
            row = QListWidgetItem(f"{label}({base}) — 当前 modId: {current}")
            row.setData(Qt.ItemDataRole.UserRole, base)
            self._list.addItem(row)
        self._list.setCurrentRow(0)
        root.addWidget(self._list, stretch=1)

        if len(matches) > 1:
            w = QLabel("多个候选: 请选出真正提供该依赖的那个; 拿不准就忽视。")
            w.setStyleSheet(f"color: {ORANGE};")
            root.addWidget(w)

        root.addWidget(QLabel(
            "修补 = 把该 jar 的 modId 改写为缺失的 modid;"
            "原件备份为同名 .orig, 完成后自动重新扫描。"))

        btns = QHBoxLayout()
        b_patch = QPushButton("修补它")
        b_patch.clicked.connect(self._on_patch)
        btns.addWidget(b_patch)
        b_ignore = QPushButton("忽视")
        b_ignore.clicked.connect(self._on_ignore)
        btns.addWidget(b_ignore)
        root.addLayout(btns)

    def _on_patch(self) -> None:
        cur = self._list.currentItem()
        if cur is None:
            self.reject()  # 无候选可修(防御, 正常流程不会到达)
            return
        self.choice = cur.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _on_ignore(self) -> None:
        self.choice = None
        self.reject()


class VerdictDialog(QDialog):
    """终局报告弹窗(判决结果 + 可选一键还原)。

    结果读取: restore_requested(bool) — 用户是否要求还原全部 mod。
    """

    def __init__(self, verdict: Verdict, parent: QWidget | None = None):
        super().__init__(parent)
        self.restore_requested: bool = False
        self.setWindowTitle("排查结果")
        self.setModal(True)
        self.resize(520, 360)
        root = QVBoxLayout(self)

        title = QLabel(f"【{verdict.title}】")
        title.setStyleSheet(f"color: {GOLD}; font-weight: bold;")
        root.addWidget(title)

        # 详情(多行文本, 只读, 自动换行)
        detail = QPlainTextEdit(verdict.detail)
        detail.setReadOnly(True)
        root.addWidget(detail, stretch=1)

        # 确诊罪魁清单(仅单因结案非空)
        if verdict.culprit:
            box = QFrame()
            box.setStyleSheet(f"QFrame {{ background: {GREEN}; }} "
                              "QLabel { color: white; font-weight: bold; }")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(8, 8, 8, 8)
            bl.addWidget(QLabel("罪魁 mod(确认禁用即可解决问题):"))
            for name in sorted(verdict.culprit):
                bl.addWidget(QLabel(f"  • {name}"))
            root.addWidget(box)

        # ---- 动作按钮 ----
        row = QHBoxLayout()
        b_restore = QPushButton("还原所有 mod 到初始状态")
        b_restore.setMinimumHeight(40)
        b_restore.clicked.connect(self._on_restore)
        row.addWidget(b_restore)
        b_keep = QPushButton("保持当前状态")
        b_keep.setMinimumHeight(40)
        b_keep.clicked.connect(self.reject)
        row.addWidget(b_keep)
        root.addLayout(row)

    def _on_restore(self) -> None:
        self.restore_requested = True
        self.accept()


class RecheckDialog(QDialog):
    """复测导出设置弹窗(v0.7): 干净实例根 + config 拷贝范围。

    结果读取: instance_root(str) — 「导出」后为所选目录;
    include_config(bool) — 勾选即附带 config(modid 前缀匹配)。
    export_names: 导出集合预览(recheck_export_names 产出, 含依赖)。
    契约: exec() 为 True ⟹ instance_root 非空(空根禁用导出按钮,
    按文本事件联动保证, 无轮询)。
    """

    def __init__(self, export_names: list[str], include_config: bool,
                 last_dir: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.instance_root: str = ""  # 取消/未导出保持空
        self.setWindowTitle("复测导出 — 干净实例")
        self.setModal(True)
        self.resize(520, 420)
        root = QVBoxLayout(self)

        root.addWidget(QLabel(
            "将以下 mod(嫌疑 + 共谋候选 + 依赖链)导出到干净实例, "
            "在其 mods 目录里隔离复测:"))
        names = QPlainTextEdit("\n".join(export_names))
        names.setReadOnly(True)
        root.addWidget(names, stretch=1)

        # ---- 实例根选择行(预填上次目录, 浏览/手输皆可) ----
        row = QHBoxLayout()
        row.addWidget(QLabel("干净实例根目录:"))
        self._dir_edit = QLineEdit(last_dir)
        self._dir_edit.textChanged.connect(self._sync_ok)
        row.addWidget(self._dir_edit, stretch=1)
        b_browse = QPushButton("浏览…")
        b_browse.clicked.connect(self._on_browse)
        row.addWidget(b_browse)
        root.addLayout(row)
        root.addWidget(QLabel(
            "实例根 = 带加载器的可玩实例目录(mods 的上一级); "
            "程序只往里放文件, 不代建实例不代删内容。"))

        # ---- config 拷贝范围(默认关, 橙字警示理由) ----
        self._cfg_check = QCheckBox("附带各 mod 的 config(不建议)")
        self._cfg_check.setChecked(include_config)
        self._cfg_check.setToolTip(
            "按 modid 前缀匹配源实例 config/ 下的文件与目录一并拷贝")
        root.addWidget(self._cfg_check)
        warn = QLabel(
            "警示: config 里的世界状态/机器缓存可能本身就是问题载体, "
            "拷过去会把「mod 有 bug」污染成「mod + 旧状态有 bug」。")
        warn.setStyleSheet(f"color: {ORANGE};")
        warn.setWordWrap(True)
        root.addWidget(warn)

        # ---- 动作按钮 ----
        btns = QHBoxLayout()
        self._b_ok = QPushButton("导出到该实例")
        self._b_ok.setMinimumHeight(40)
        self._b_ok.clicked.connect(self._on_ok)
        btns.addWidget(self._b_ok)
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_cancel)
        root.addLayout(btns)
        self._sync_ok()  # 初始态(last_dir 空 = 禁用导出)

    def _sync_ok(self) -> None:
        """导出按钮联动: 根文本非空才可点(契约保证)。"""
        self._b_ok.setEnabled(bool(self._dir_edit.text().strip()))

    @property
    def include_config(self) -> bool:
        """导出附带 config 的复选框状态(app 层导出前读取)。"""
        return self._cfg_check.isChecked()

    def _on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择干净实例根", self._dir_edit.text().strip())
        if d:  # 取消浏览不改写当前文本
            self._dir_edit.setText(d)

    def _on_ok(self) -> None:
        self.instance_root = self._dir_edit.text().strip()
        self.accept()