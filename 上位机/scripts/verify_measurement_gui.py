"""Render the actual measurement widgets with an injected software link only."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from car_host.controller import HostController
from car_host.project_fake import FAKE_CONFIG, create_project_fake_worker
from car_host.automation import AutomationService
from car_host.automation_panel import AutomationPanel
from car_host.health_panel import CommunicationHealthPanel
from car_host.ui import MainWindow, STYLE
from car_host.runtime import prepare_qt_fonts
from PySide6.QtWidgets import QApplication


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / "docs" / "assets"
    app = QApplication([])
    prepare_qt_fonts()
    app.setStyleSheet(STYLE)
    host = HostController(root / ".qa" / "measurement_gui", worker_factory=create_project_fake_worker)
    service = AutomationService(host, root / ".qa" / "measurement_gui", demo=True)
    window = MainWindow(host)
    panel = AutomationPanel(service, window)
    health = CommunicationHealthPanel(host, window)
    window.show()
    panel.show()
    host.connect_device(FAKE_CONFIG)
    words = "纯软件GUI夹具：批准示例整表；无实物连接或运动"
    test_plan = dict(schema_version=2, name="纯软件 · 400+600ms 分段与零输出对照",
        suite=dict(suite_id="gui_" + str(time.time_ns()), total_entered_budget=3), stages=[dict(
        stage_id="B1", name="时间块1", block_id="B1", rest_before_ms=0,
        steps=[dict(segments=[dict(left_pwm=1400, right_pwm=0, duration_ms=400),
                             dict(left_pwm=pwm, right_pwm=0, duration_ms=600)],
                    control_type=kind, label=kind, repetitions=1)
               for pwm, kind in ((900, "candidate"), (1400, "high_hold"), (0, "zero_output"))])])
    deadline = time.monotonic() + 3
    while not host.project_state_fresh and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert host.project_state_fresh
    panel.set_draft(test_plan)
    panel.preview_draft()
    deadline = time.monotonic() + 3
    while not host.capabilities.pwm_profile and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert host.capabilities.pwm_profile
    panel.approval_edit.setText(words)
    panel.ready_check.setChecked(True)
    panel.tabs.setCurrentWidget(panel.draft_table)
    app.processEvents()
    assert panel.grab().save(str(destination / "measurement_editor.png"))
    panel.run_plan()
    deadline = time.monotonic() + 18
    captured = False
    while service.active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
        if service.status()["phase"] == "moving" and not captured:
            panel.tabs.setCurrentWidget(panel.table)
            panel.refresh()
            window._flush_display()
            assert panel.grab().save(str(destination / "measurement_running.png"))
            assert window.grab().save(str(destination / "measurement_workbench.png"))
            captured = True
    assert captured and service.status()["phase"] == "completed", service.status()
    host.send_command("stop")
    deadline = time.monotonic() + 1.3
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    health.show()
    health.refresh()
    panel.show_results()
    app.processEvents()
    assert health.grab().save(str(destination / "communication_health.png"))
    assert panel.grab().save(str(destination / "measurement_results.png"))
    health.close()
    panel.close()
    window.close()
    deadline = time.monotonic() + 3
    while not host.shutdown() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert host.shutdown()
    print("GUI captures complete; three software profile trials only: " + str(host.recorder.directory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
