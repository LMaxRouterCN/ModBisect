# SPDX-License-Identifier: MPL-2.0
"""全局视觉: 黑金直角主题(用户 v0.2 需求: 主黑金配色, 全控件零圆角)。

- MAIN_QSS: 应用级样式表, main.py 里 QApplication.setStyleSheet 注入
- 语义色常量: 供代码侧 setForeground / 局部样式引用; QSS 内的字面值
  与常量同源(test_core 有同步性断言, 改色必两边同步)
- 直角: QSS 不写任何 border-radius(Qt 默认即直角, 显式零圆角)
"""
from __future__ import annotations

# ---- 语义色(代码侧引用; QSS 字面值同源) ----
GOLD = "#d4af37"          # 金: 主强调(表头文字/按钮边框/标题)
GOLD_BRIGHT = "#f0d060"   # 亮金: hover 与选中态
BG = "#141414"            # 窗口底色(近黑)
BG_PANEL = "#101010"      # 面板/表格底色
TEXT = "#e8e8e8"          # 主文字(白灰)
TEXT_DIM = "#909090"      # 次要文字(灰/禁用行)
BORDER = "#3a3a3a"        # 常规边框(深灰)
GOLD_DIM_BG = "#3a2f10"   # 金系暗底(选中行/hover)
GREEN = "#4caf50"         # 状态: 启用
RED = "#e53935"           # 状态: 禁用/危险
ORANGE = "#ff9800"        # 警告(崩溃横幅/作废提示)
YELLOW = "#fdd835"        # 嫌疑标记

MAIN_QSS = """
/* v0.2 黑金直角全局主题(色值与 style.py 常量同源) */
QMainWindow, QDialog { background: #141414; }
QWidget { color: #e8e8e8; font-family: "Microsoft YaHei UI", "Segoe UI"; }

QPushButton { background: #1e1e1e; border: 1px solid #d4af37; color: #d4af37; padding: 5px 12px; }
QPushButton:hover { background: #3a2f10; border-color: #f0d060; color: #f0d060; }
QPushButton:pressed { background: #d4af37; color: #141414; }
QPushButton:disabled { background: #161616; border-color: #3a3a3a; color: #6a6a6a; }

QLineEdit, QPlainTextEdit { background: #1a1a1a; border: 1px solid #3a3a3a; color: #e8e8e8; selection-background-color: #3a2f10; selection-color: #f0d060; }
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #d4af37; }

QTableWidget { background: #101010; alternate-background-color: #141414; gridline-color: #262626; border: 1px solid #3a3a3a; color: #e8e8e8; selection-background-color: #3a2f10; selection-color: #f0d060; }
QHeaderView::section { background: #101010; color: #d4af37; border: 0; border-right: 1px solid #262626; border-bottom: 2px solid #d4af37; padding: 4px 8px; }
QHeaderView::section:hover { background: #1a1a1a; color: #f0d060; }
QTableCornerButton::section { background: #101010; border: 0; }
QTableWidget::item { padding: 2px 6px; }

QListWidget { background: #1a1a1a; border: 1px solid #3a3a3a; color: #e8e8e8; }
QListWidget::item { padding: 4px 6px; }
QListWidget::item:selected { background: #3a2f10; color: #f0d060; }

QLabel { background: transparent; color: #e8e8e8; }
QGroupBox { border: 1px solid #3a3a3a; margin-top: 10px; color: #d4af37; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #d4af37; }

QScrollBar:vertical { background: #0d0d0d; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a3a3a; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #d4af37; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: #0d0d0d; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #3a3a3a; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: #d4af37; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

QMenu { background: #161616; border: 1px solid #3a3a3a; color: #e8e8e8; }
QMenu::item { padding: 4px 24px; }
QMenu::item:selected { background: #3a2f10; color: #f0d060; }
QToolTip { background: #161616; color: #e8e8e8; border: 1px solid #d4af37; padding: 2px; }
QMessageBox { background: #141414; }
"""