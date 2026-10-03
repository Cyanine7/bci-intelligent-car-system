"""Offscreen QA capture using the same simulated telemetry pipeline."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from car_host.controller import HostController
from car_host.protocol import LegacyTelemetryAdapter
from car_host.ui import MainWindow
from PySide6.QtWidgets import QApplication


def main():
    bluetooth_view = "--bluetooth" in sys.argv
    positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    destination = Path(positional[0]) if positional else Path(".qa/workbench.png")
    destination.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    host = HostController(Path(__file__).resolve().parents[1] / ".qa" / "recordings")
    window = MainWindow(host)
    window.show()
    if bluetooth_view:
        window.kind_combo.setCurrentIndex(window.kind_combo.findData("bluetooth_spp"))
        app.processEvents()
        if not window.grab().save(str(destination)):
            raise RuntimeError("Could not save Bluetooth configuration screenshot")
        if host.scanner is not None or host.worker is not None:
            raise RuntimeError("Configuration capture unexpectedly operated a device")
        window.close()
        if not host.shutdown():
            raise RuntimeError("Configuration view did not close")
        print(f"Bluetooth configuration GUI OK (disconnected): {destination.resolve()}")
        return 0
    window.kind_combo.setCurrentIndex(1)
    window.connect_button.click()
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    # Seed a full minute of explicitly simulated measurements for visual QA.
    now = time.monotonic()
    adapter = LegacyTelemetryAdapter("SIMULATOR")
    host.history.clear()
    for i in range(1200):
        age = 60.0 - i * 0.05
        left = int(14 + 6 * math.sin(i * 0.02))
        right = int(14 + 5 * math.sin(i * 0.02 + 0.6))
        for sample in adapter.feed(f"{{C{left}:{right}:90}}$".encode(), now - age,
                                   datetime.now(timezone.utc).isoformat(timespec="milliseconds")):
            host.history.append(sample)
    window.send_editor.setPlainText("HELLO")
    window.send_button.click()
    host.start_recording()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    window.refresh_state()
    app.processEvents()
    if not window.grab().save(str(destination)):
        raise RuntimeError("Could not save QA screenshot")
    print(f"GUI OK: {host.frames_received} received frames; snapshot {destination.resolve()}")
    window.close()
    deadline = time.monotonic() + 4.0
    while window.isVisible() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    if window.isVisible() or not host.shutdown():
        raise RuntimeError("GUI did not finish asynchronous shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
