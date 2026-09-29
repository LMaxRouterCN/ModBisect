# SPDX-License-Identifier: MPL-2.0
"""UI 弹窗层: 轮次判决弹窗 + 终局报告弹窗。

职责边界: 只做"展示 → 收集答案"的原子交互, 不持有业务状态, 不做调度。
按钮文案自带因果链(D5: 消除"是的/没有"式指代歧义)。
风格: 直角(main.py 全局 QSS 已归零圆角), 无业务特化配色。
"""

from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

from ..engine import Answer, Phase, RoundPlan, Verdict

# "重新测试本轮"返回哨兵(独立于 Answer, 语义上不是玩家观察结果)
RETEST = "retest"


class JudgeDialog(QDialog):
    """一轮游戏结束后的结果收集弹窗。

    结果读取: answer(Answer|None) 与 retest(bool) 二选一为真;
    基准轮额外提供"跳过"选项(SKIP 仅在该轮合法, 引擎侧有二次防御)。
    """

    def __init__(self, plan: RoundPlan, crashed: bool,
                 disabled_names: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.answer: Answer | None = None
        self.retest: bool = False
        self.setWindowTitle("本轮测试结果")
        self.setModal(True)
        root = QVBoxLayout(self)

        # ---- 崩溃警告横幅(红色; 仅本轮检测到崩溃时显示, D5 信号防污染) ----
        if crashed:
            banner = QFrame()
            banner.setStyleSheet(
                "QFrame { background: #b71c1c; }"
                "QLabel { color: white; font-weight: bold; }")
            bl = QVBoxLayout(banner)
            bl.setContentsMargins(8, 8, 8, 8)
            bl.addWidget(QLabel(
                "警告: 本轮游戏发生崩溃。\n"
                "崩溃意味着游戏可能没有完成正常测试, 本次观察结果可能无效。\n"
                "建议选择「重新测试本轮」。"))
            root.addWidget(banner)

        # ---- 轮次说明 ----
        phase_title = {
            Phase.BASELINE: "基准轮 — 前提确认",
            Phase.BISECT: "二分轮",
            Phase.VERIFY: "验证轮 — 最小启用集",
        }[plan.phase]
        root.addWidget(QLabel(f"【{phase_title}】"))
        root.addWidget(QLabel(plan.prompt))

        # 本轮被禁用清单(展示前 8 个, 附件数防刷屏)
        if disabled_names:
            head = ", ".join(disabled_names[:8])
            more = (f" 等共 {len(disabled_names)} 个"
                    if len(disabled_names) > 8 else "")
            root.addWidget(QLabel(f"本轮已禁用: {head}{more}"))
        else:
            root.addWidget(QLabel("本轮未禁用任何 mod(维持当前状态)"))

        root.addSpacing(8)

        # ---- 答案按钮组: 文案即因果链 ----
        def _btn(text: str, handler) -> QPushButton:
            b = QPushButton(text)
            b.setMinimumHeight(52)  # 两行文案的呼吸空间
            b.clicked.connect(handler)
            return b

        root.addWidget(_btn(
            "问题还在\n(问题出在当前启用的 mod 里)",
            lambda: self._choose(Answer.PRESENT)))
        root.addWidget(_btn(
            "问题消失了\n(问题出在刚才被禁用的 mod 里)",
            lambda: self._choose(Answer.ABSENT)))
        if plan.phase is Phase.BASELINE:
            root.addWidget(_btn(
                "跳过基准确认\n(我确定 bug 存在, 直接开始二分)",
                lambda: self._choose(Answer.SKIP)))
        root.addWidget(_btn(
            "重新测试本轮\n(还原本轮改动, 再测一次)",
            self._choose_retest))

    # ---- 结果记录 ----
    def _choose(self, answer: Answer) -> None:
        self.answer = answer
        self.accept()

    def _choose_retest(self) -> None:
        self.retest = True
        self.reject()  # reject 区分"非答案性关闭"


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

        root.addWidget(QLabel(f"【{verdict.title}】"))

        # 详情(多行文本, 只读, 自动换行)
        detail = QPlainTextEdit(verdict.detail)
        detail.setReadOnly(True)
        root.addWidget(detail, stretch=1)

        # 确诊罪魁清单(仅单因结案非空)
        if verdict.culprit:
            box = QFrame()
            box.setStyleSheet("QFrame { background: #2e7d32; }"
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