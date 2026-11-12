"""Shared workbench appearance, including lazily created experiment plots."""

STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif; font-size: 12px; color: #253449; }
QMainWindow, QWidget#workbench { background: #f3f5f9; }
QWidget#formPage { background: white; border-radius: 8px; }
QLabel#title { font-size: 24px; font-weight: 700; color: #16253b; }
QLabel#subtitle, QLabel#muted { color: #738196; font-weight: 400; }
QLabel#badge { background: #e8edf4; color: #617087; padding: 7px 14px; border-radius: 14px; font-weight: 600; }
QLabel#badge[state="connected"] { background: #e2f4ed; color: #18745b; }
QLabel#badge[state="simulation"] { background: #e9edff; color: #4b5fc1; }
QFrame#actionBar, QFrame#navigation { background: white; border: 1px solid #e2e7ef; border-radius: 10px; }
QFrame#contextBar { background: #eaf0fc; border: 1px solid #dae4f9; border-radius: 8px; }
QLabel#contextText { color: #415b85; }
QLabel#sectionTitle { font-size: 19px; font-weight: 700; color: #203653; }
QGroupBox { background: white; border: 1px solid #e0e6ef; border-radius: 10px; margin-top: 18px; padding: 14px 12px 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0px 6px; color: #344860; }
QPushButton { background: white; border: 1px solid #dbe2ec; border-radius: 6px; padding: 7px 12px; color: #425570; }
QPushButton:hover { background: #f0f4fc; border-color: #a8bce8; }
QPushButton:pressed { background: #e4ebfa; }
QPushButton:focus { border: 1px solid #527be6; }
QPushButton:disabled { color: #9ba6b7; background: #f5f7fa; border-color: #e7ebf1; }
QPushButton#primary { background: #426ce0; color: white; border-color: #426ce0; font-weight: 600; }
QPushButton#primary:hover { background: #335ac7; }
QPushButton#primary:disabled { background: #b7c6eb; border-color: #b7c6eb; color: #f5f7fc; }
QPushButton#danger { background: #fff0f0; border-color: #f2cccc; color: #b43d45; font-weight: 700; }
QPushButton#danger:hover { background: #fbe0e2; border-color: #dc8f95; }
QPushButton#danger:disabled { background: #faf4f4; border-color: #eee1e1; color: #b9a2a4; }
QPushButton#navItem { text-align: left; border: none; padding: 12px 10px; background: transparent; color: #6b7890; }
QPushButton#navItem:checked { background: #eaf0ff; color: #365fc7; font-weight: 700; }
QPushButton#navItem:hover { background: #f0f4fc; }
QPushButton#logToggle:checked { background: #eaf0ff; border-color: #cbd8f7; color: #365fc7; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { border: 1px solid #dbe2ec; border-radius: 5px; padding: 5px 7px; background: #fcfdff; min-height: 20px; selection-background-color: #426ce0; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #7293e8; background: white; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled { color: #98a4b4; background: #f3f5f8; }
QComboBox QAbstractItemView { background: white; selection-background-color: #eaf0ff; selection-color: #365fc7; }
QPlainTextEdit { border: 1px solid #e1e7f0; border-radius: 6px; background: #f9fbfe; padding: 7px; font-family: Consolas, 'Microsoft YaHei UI'; font-size: 12px; font-weight: 400; selection-background-color: #426ce0; }
QLabel#metric { font-family: 'Segoe UI', 'Microsoft YaHei UI'; font-size: 30px; font-weight: 700; color: #365fc7; }
QFrame#metricCard { background: #f0f4ff; border: 1px solid #e4eafa; border-radius: 8px; }
QScrollArea { background: transparent; border: none; }
QSplitter::handle { background: #e5eaf2; }
QSplitter::handle:vertical { height: 5px; }
QSplitter::handle:horizontal { width: 5px; }
QTabWidget::pane { border: none; }
QTabBar::tab { padding: 9px 16px; margin-right: 3px; color: #718098; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #365fc7; background: white; font-weight: 700; border-bottom: 2px solid #426ce0; }
QTabBar::tab:hover { color: #365fc7; background: #eaf0ff; }
QCheckBox { spacing: 6px; }
QStatusBar { background: #edf1f7; color: #6c7d93; }
QToolTip { background: #253449; color: white; border: none; padding: 6px; }
QScrollBar:vertical { background: #f1f4f9; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #c7d1e0; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


def style_plot(plot):
    plot.setBackground("white")
    plot.showGrid(x=True, y=True, alpha=0.12)
    for name in ("left", "bottom"):
        axis = plot.getAxis(name)
        axis.setPen("#d7dfeb")
        axis.setTextPen("#718098")
