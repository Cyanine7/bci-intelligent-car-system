"""Actual Qt widgets with injected PROJECT_V1 software device only."""

import csv
import time

import numpy as np

from PySide6.QtCore import QEvent, QObject
from car_host.automation import AutomationQuitGuard
from car_host.automation_panel import AutomationPanel
from car_host.project_panels import MotionPanel, ParameterPanel
from car_host.project_protocol import ARM, SET_PWM, STOP
from car_host.ui import MainWindow
from test_automation import automation, command_kinds, draft, drive, fake_host, preview_and_start, pump


def test_gui_shows_frozen_plan_locks_manual_controls_and_keeps_stop_available(automation, qapp):
    host, _, service = automation
    window = MainWindow(host)
    motion = MotionPanel(host)
    parameters = ParameterPanel(host)
    panel = AutomationPanel(service, window)
    try:
        window.show()
        panel.show()
        preview, _, _ = preview_and_start(service)
        host.changed.emit()
        qapp.processEvents()
        assert "纯软件假设备" in window.automation_status_value.text()
        assert preview["plan_id"] in panel.plan_label.text()
        assert preview["digest"] in panel.plan_label.text()
        assert panel.table.rowCount() == 2
        assert panel.table.item(0, 2).text() == "1200"
        assert panel.table.item(0, 3).text() == "-1500"
        assert not motion.arm_button.isEnabled() and not motion.run_button.isEnabled()
        assert not parameters.apply_button.isEnabled() and not parameters.read_button.isEnabled()
        assert not window.record_button.isEnabled()
        assert all(button.isEnabled() for button in (window.stop_button, motion.stop_button,
                                                      parameters.stop_button, panel.stop_button))
        drive(automation, lambda: service.status()["phase"] == "moving")
        host.changed.emit()
        assert "有限实验运行中" in window.automation_status_value.text()
        panel.stop_button.click()
        drive(automation, lambda: not service.active)
        host.changed.emit()
        assert "已中止" in window.automation_status_value.text()
        assert command_kinds(host.worker).count(STOP) == 1
    finally:
        panel.close()
        motion.close()
        parameters.close()
        window.close()
        window._retry_close()


def test_pause_display_keeps_automation_raw_recording_independent(automation, qapp):
    host, clock, service = automation
    window = MainWindow(host)
    try:
        window.show()
        preview_and_start(service, draft(repetitions=1, duration_ms=800))
        drive(automation, lambda: service.status()["phase"] == "moving")
        window.log_pause.setChecked(True)
        window.plot_pause.setChecked(True)
        frozen_logs = window.log_view.toPlainText()
        frozen_curve = window.left_curve.xData.copy()
        frames_before = host.frames_received
        drive(automation, lambda: not service.active)
        assert host.frames_received > frames_before + 20
        assert window.log_view.toPlainText() == frozen_logs
        assert np.array_equal(window.left_curve.xData, frozen_curve)
        directory = host.recorder.directory
        with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == host.recorder.sample_count and len(rows) > 30
        assert any(int(row["pwm_left"]) == 1200 for row in rows)
        assert {row["source"] for row in rows} == {"SIMULATION_PROJECT_V1"}
        assert "INTEGRITY" in (directory / "communication.log").read_text(encoding="utf-8")
        window.log_pause.setChecked(False)
        window.plot_pause.setChecked(False)
        assert window.log_view.toPlainText() != frozen_logs
        assert window.left_curve.xData.size > frozen_curve.size
    finally:
        window.close()
        window._retry_close()


def test_window_close_cancels_stage_before_fake_transport_and_file_workers_finish(automation, qapp):
    host, clock, service = automation
    worker = host.worker
    window = MainWindow(host)
    window.show()
    preview_and_start(service, draft(repetitions=3))
    drive(automation, lambda: service.status()["phase"] == "moving")
    before = sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker))
    window.close()
    assert service.status()["phase"] == "stopping"
    drive(automation, lambda: not service.active)
    deadline = time.perf_counter() + 2
    while time.perf_counter() < deadline and window.isVisible():
        window._retry_close()
        qapp.processEvents()
        time.sleep(.002)
    assert not window.isVisible() and host.worker is None
    assert not host.recorder.is_running
    assert all(not journal.is_running for journal in service._history.values())
    assert command_kinds(worker).count(STOP) == 1
    assert sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker)) == before


def test_demo_ui_real_device_choice_cannot_create_hardware_worker(automation, qapp):
    host, clock, service = automation
    host.disconnect_device()
    pump(host, clock, service)
    pump(host, clock, service)
    window = MainWindow(host)
    try:
        window.kind_combo.setCurrentIndex(window.kind_combo.findData("bluetooth_spp"))
        window.connect_button.click()
        assert host.worker is None and not host.connected
        assert "不会访问硬件" in window.log_view.toPlainText()
    finally:
        window.close()


def test_qt_quit_guard_defers_quit_until_async_shutdown_finishes(qapp):
    class PendingHost:
        complete = False
        calls = 0

        def shutdown(self):
            self.calls += 1
            return self.complete

    host = PendingHost()
    guard = AutomationQuitGuard(qapp, host)
    try:
        assert not guard.eventFilter(qapp, QEvent(QEvent.Type.User))
        assert not guard.eventFilter(QObject(), QEvent(QEvent.Type.Quit))
        assert host.calls == 0
        assert guard.eventFilter(qapp, QEvent(QEvent.Type.Quit)) is True
        assert guard.timer.isActive() and guard.timer.interval() == 100
        assert guard.eventFilter(qapp, QEvent(QEvent.Type.Quit)) is True
        assert host.calls == 2  # Saving is still incomplete; Quit remains blocked.
        host.complete = True
        assert guard.eventFilter(qapp, QEvent(QEvent.Type.Quit)) is False
        assert not guard.timer.isActive() and host.calls == 3
    finally:
        # Never emit app.quit or send an unfiltered Quit to the shared qapp.
        guard.timer.stop()
        qapp.removeEventFilter(guard)
        guard.deleteLater()


def test_quit_during_stage_waits_for_stop_raw_log_and_experiment_report(automation, qapp):
    host, _, service = automation
    worker = host.worker
    preview_and_start(service, draft(repetitions=3))
    drive(automation, lambda: service.status()["phase"] == "moving")
    motion_count = sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker))
    guard = AutomationQuitGuard(qapp, host)
    try:
        assert guard.eventFilter(qapp, QEvent(QEvent.Type.Quit)) is True
        assert service.status()["phase"] == "stopping" and guard.timer.isActive()
        assert host.recorder.is_running
        drive(automation, lambda: not service.active)
        assert guard.eventFilter(qapp, QEvent(QEvent.Type.Quit)) is False
        assert not guard.timer.isActive() and host.worker is None
        assert host.control_owner is None and not host.recorder.is_running
        result = service.dispatch("get_results", {}, "test-client")
        assert result["finished"] and result["status"] == "aborted"
        assert (host.recorder.directory / "report.md").is_file()
        assert "INTEGRITY" in (host.recorder.directory / "communication.log").read_text(encoding="utf-8")
        assert command_kinds(worker).count(STOP) == 1
        assert sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker)) == motion_count
    finally:
        guard.timer.stop()
        qapp.removeEventFilter(guard)
        guard.deleteLater()
