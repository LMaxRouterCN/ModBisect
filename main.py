# SPDX-License-Identifier: MPL-2.0
"""程序入口: 构建 QApplication, 全局直角化, 装配主窗口。

启动形态:
    python main.py            (交互运行)
    QT_QPA_PLATFORM=offscreen python main.py --smoke   (冒烟自检)
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from modbisect.config import load_config
from modbisect.ui.app import MainWindow

# 全局直角 QSS(Fusion 底盘): 所有控件圆角归零, 系 Windows 经典观感。
# 注意: 通用选择器已覆盖原生控件, 自绘控件各自 setStyleSheet 时须自带归零
_SQUARE_QSS = """
* { border-radius: 0; }
QPushButton { padding: 6px 16px; }
QPlainTextEdit { padding: 4px; }
QLineEdit, QSpinBox { padding: 2px 4px; }
QTableView { padding: 0; }
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(_SQUARE_QSS)
    cfg = load_config()
    win = MainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())