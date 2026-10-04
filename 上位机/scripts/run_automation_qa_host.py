"""Launch the real offscreen entry point with its hardware-free demo factory.

Internal helper for verify_mcp_end_to_end.py. The exit marker must be inside
this project's .qa directory. An existing endpoint or instance lock is refused.
"""

import os
import json
from pathlib import Path
import sys


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    if len(sys.argv) != 2:
        raise SystemExit("需要由 verify_mcp_end_to_end.py 提供 .qa 内的退出标记路径")
    stop_file = Path(sys.argv[1]).resolve()
    if stop_file.parent != (project / ".qa").resolve() or not stop_file.name.startswith("mcp_qa_stop_"):
        raise SystemExit("拒绝 .qa 目录之外或不属于本验证的退出标记")
    if any((project / ".runtime" / name).exists() for name in ("automation.json", "automation.lock")):
        raise SystemExit("已有工作台端点或实例锁；拒绝启动验证，不附加现有进程。")
    # Windows venv's python.exe may be a launcher whose PID differs from this
    # interpreter's. The parent verifies this unique invocation's owner record.
    stop_file.with_suffix(".owner.json").write_text(
        json.dumps({"pid": os.getpid(), "run_marker": stop_file.name}), encoding="utf-8")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(project))
    # car_host prepares Qt's runtime before this helper imports Qt.
    from car_host import __main__ as entry
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    class QAApplication(QApplication):
        def exec(self):
            timer = QTimer(self)
            timer.setInterval(100)
            timer.timeout.connect(lambda: self.closeAllWindows() if stop_file.exists() else None)
            timer.start()
            return super().exec()

    entry.QApplication = QAApplication
    sys.argv = ["car_host", "--automation-demo", "--no-auto-connect"]
    return entry.main()


if __name__ == "__main__":
    raise SystemExit(main())
