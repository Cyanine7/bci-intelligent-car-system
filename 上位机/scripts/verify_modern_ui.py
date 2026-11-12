"""Capture every work page at both supported sizes without connecting a device."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from car_host.ui import MainWindow
from car_host.controller import HostController
from PySide6.QtWidgets import QApplication


def main():
    app = QApplication([])
    destination = ROOT / ".qa" / "modern_ui"
    destination.mkdir(parents=True, exist_ok=True)
    host = HostController(destination / "recordings")
    window = MainWindow(host)
    window.show()
    try:
        for width, height in ((1320, 900), (1000, 720)):
            window.resize(width, height)
            app.processEvents()
            for index, name in enumerate(("monitor", "speed", "yaw", "continuous", "device")):
                window.workspace_tabs.setCurrentIndex(index)
                app.processEvents()
                assert host.worker is None and host.scanner is None
                assert window.width() == width and window.height() == height
                assert window.grab().save(str(destination / f"{name}_{width}.png"))
    finally:
        window.close()
        assert host.shutdown()
    print("All five pages captured at 1320x900 and 1000x720; no device access")


if __name__ == "__main__":
    main()
