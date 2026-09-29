# SPDX-License-Identifier: MPL-2.0
"""程序入口: 构建 QApplication, 注入黑金直角主题, 装配主窗口。

启动形态:
    python main.py            (交互运行)
    QT_QPA_PLATFORM=offscreen python main.py --smoke   (冒烟自检)
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from modbisect.config import load_config
from modbisect.ui.app import MainWindow
from modbisect.ui.style import MAIN_QSS


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(MAIN_QSS)  # v0.2 黑金直角主题(style.py 唯一色源)
    cfg = load_config()
    win = MainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())