"""Capture PROJECT_V1 UI using read-only fake link replies, without hardware."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections import deque
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from car_host.controller import HostController
from car_host.project_protocol import (ACK, CAPS, CAPS_LAYOUT, GET_PARAMS, HELLO,
    PARAMS, PARAMS_LAYOUT, PROJECT_V1, STATE, STATE_LAYOUT, encode_frame)
from car_host.project_panels import MotionPanel, ParameterPanel
from car_host.transport import ConnectionConfig, IoEvent
from car_host.ui import MainWindow, STYLE
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget


class ReadOnlyFixture:
    def __init__(self, config):
        self.connection_id = uuid4().hex
        self.events = deque()
        self.finished = False
        self.session = self.seq = 0

    def emit(self, kind, payload=b""):
        self.events.append(IoEvent(kind, payload, "软件协议夹具；未操作硬件", time.monotonic(),
                                   "2026-10-03T08:00:00.000Z", self.connection_id))

    def start(self):
        self.emit("opened")

    def request_send(self, payload, request_id=""):
        import struct
        _, _, kind, _, self.session, self.seq = struct.unpack_from("<2sBBHII", payload)
        if kind == HELLO:
            self.emit("rx", encode_frame(CAPS, self.session, self.seq, CAPS_LAYOUT.pack(15, 6000, 300, 1000, 100, 20, 1)))
        elif kind == GET_PARAMS:
            self.emit("rx", encode_frame(PARAMS, self.session, self.seq, PARAMS_LAYOUT.pack(7, 120000, 1000, 120000, 1000)))
        else:
            raise RuntimeError("界面截图夹具只允许 HELLO 与 GET_PARAMS")
        self.emit("rx", encode_frame(ACK, self.session, self.seq, bytes((kind, 0))))
        return True

    def request_stop(self):
        self.finished = True
        self.emit("closed")

    def drain_events(self, max_count=256):
        return [self.events.popleft() for _ in range(min(max_count, len(self.events)))]

    def take_overflow_count(self):
        return 0

    def isFinished(self):
        return self.finished


def main():
    app = QApplication([])
    root = Path(__file__).resolve().parents[1]
    destination = root / "docs" / "assets"
    host = HostController(root / ".qa" / "recordings", worker_factory=ReadOnlyFixture)
    host.timer.stop()
    window = MainWindow(host)
    window.protocol_combo.setCurrentIndex(window.protocol_combo.findData(PROJECT_V1))
    window.port_combo.setEditText("COM_QA_FIXTURE")
    window.show()
    assert host.connect_device(ConnectionConfig(port="COM_QA_FIXTURE", protocol=PROJECT_V1))
    host.poll()
    host.poll()
    link = host.worker
    now = time.monotonic()
    for index in range(120):
        payload = STATE_LAYOUT.pack(1000 + 50 * index, link.seq, -456 + index, 123 + index,
            -120, 250, 0, 0, 12345, 91, 0, 0, 1, 1, 0, 0, 7, 0)
        link.events.append(IoEvent("rx", encode_frame(STATE, link.session, 0, payload), "",
                                  now - (119 - index) * .05, "2026-10-03T08:00:00.000Z", link.connection_id))
    host.poll()
    host.parameters.read_parameters()
    host.poll()
    host.poll()
    window._last_plot_update = 0
    window.refresh_state()
    window.source_badge.setText("软件协议夹具")
    app.processEvents()
    assert window.grab().save(str(destination / "project_v1_workbench.png"))
    panels = QWidget()
    panels.setStyleSheet(STYLE)
    row = QHBoxLayout(panels)
    motion, parameters = MotionPanel(host, 1), ParameterPanel(host)
    for panel in (motion, parameters):
        panel.setParent(panels, Qt.WindowType.Widget)
        row.addWidget(panel)
    parameters._copy()
    panels.show()
    panels.resize(1250, 520)
    app.processEvents()
    assert panels.grab().save(str(destination / "project_v1_panels.png"))
    panels.close()
    window.close()
    assert host.shutdown()
    print("PROJECT_V1 GUI screenshots saved using read-only software fixture; no hardware")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
