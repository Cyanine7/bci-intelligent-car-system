import csv
import time
from dataclasses import replace

import pytest

from car_host.controller import HostController
from car_host.preferences import AppPreferences, BluetoothProfile
from car_host.ui import MainWindow
from test_application_extensions import Link


def spin_until(qapp, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for UI state")


def test_workbench_paused_display_keeps_recording_and_can_resume(qapp, tmp_path):
    host = HostController(tmp_path)
    window = MainWindow(host)
    window.show()
    try:
        assert not host.connected
        assert not window.send_button.isEnabled()
        assert window.connection_value.text() == "未连接"
        window.kind_combo.setCurrentIndex(1)
        window.connect_button.click()
        spin_until(qapp, lambda: host.frames_received >= 5)
        assert window.source_badge.text() == "模拟数据"
        assert window.left_value.text() != "—"
        assert window.telemetry_value.text() == "接收正常"
        assert window.left_curve.xData is not None
        window.record_button.click()
        assert host.recorder.is_active
        window.log_pause.setChecked(True)
        window.plot_pause.setChecked(True)
        frozen_log = window.log_view.toPlainText()
        frozen_curve = window.left_curve.xData.copy()
        window.send_format.setCurrentIndex(1)
        window.send_editor.setPlainText("48 49")
        window.send_button.click()
        first_count = host.frames_received
        spin_until(qapp, lambda: host.frames_received >= first_count + 6 and host.bytes_sent == 2)
        assert window.log_view.toPlainText() == frozen_log
        assert (window.left_curve.xData == frozen_curve).all()
        assert host.recorder.sample_count >= 5
        window.log_format.setCurrentIndex(1)
        window.log_pause.setChecked(False)
        window.plot_pause.setChecked(False)
        qapp.processEvents()
        assert "48 49" in window.log_view.toPlainText()
        assert window.left_curve.xData.size > frozen_curve.size
        window.record_button.click()
        spin_until(qapp, lambda: not host.recorder.is_running)
        directory = host.recorder.directory
        with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) >= 6
        assert {row["source"] for row in rows} == {"SIMULATOR"}
        assert "48 49" in (directory / "communication.log").read_text(encoding="utf-8")
        window.clear_log_button.click()
        assert window.log_view.toPlainText() == ""
        window.clear_plot_button.click()
        assert len(host.history.samples) == 0
        window.disconnect_button.click()
        spin_until(qapp, lambda: host.worker is None)
        assert not window.send_button.isEnabled()
    finally:
        window.close()
        spin_until(qapp, lambda: not window.isVisible() and host.shutdown())


def test_port_editor_prefers_user_text_and_invalid_baud_is_visible(qapp, tmp_path):
    host = HostController(tmp_path)
    window = MainWindow(host)
    try:
        window.kind_combo.setCurrentIndex(window.kind_combo.findData("serial"))
        window.baud_combo.setEditText("bad baud")
        window.connect_button.click()
        assert host.worker is None
        assert "波特率" in window.log_view.toPlainText()
        window.kind_combo.setCurrentIndex(1)
        assert not window.port_combo.isEnabled()
        assert not window.baud_combo.isEnabled()
        window.connect_button.click()
        spin_until(qapp, lambda: host.frames_received > 0)
    finally:
        window.close()
        spin_until(qapp, host.shutdown)


def test_editable_port_uses_typed_com_instead_of_old_item_data(qapp, tmp_path):
    host = HostController(tmp_path)
    window = MainWindow(host)
    requested = []
    host.connect_device = lambda config: requested.append(config)
    try:
        window.kind_combo.setCurrentIndex(window.kind_combo.findData("serial"))
        window.port_combo.clear()
        window.port_combo.addItem("COM3 · Example", "COM3")
        window.port_combo.setCurrentIndex(0)
        window.port_combo.setEditText("COM99")
        window.connect_button.click()
        assert requested[0].port == "COM99"
        window.refresh_ports()
        assert window.port_combo.currentText().split(" · ")[0] == "COM99"
    finally:
        window.close()
        spin_until(qapp, host.shutdown)


@pytest.fixture
def fake_workbench(qapp, tmp_path):
    host = HostController(tmp_path, worker_factory=Link, scanner_factory=Link)
    host.timer.stop()
    window = MainWindow(host)
    window.show()
    yield host, window
    if host.worker is not None:
        host.worker.stop_immediately = True
        host.worker.request_stop()
    if host.scanner is not None:
        host.scanner.stop_immediately = True
        host.scanner.request_stop()
    window.close()
    spin_until(qapp, lambda: host.shutdown() and not window.isVisible())


def test_default_profile_does_not_start_scan_or_connection_in_window(fake_workbench):
    host, window = fake_workbench
    window.kind_combo.setCurrentIndex(2)
    assert host.worker is None and host.scanner is None
    assert window.bluetooth_panel.isVisible()
    assert not window.serial_fields.isVisible()
    assert not window.baud_combo.isEnabled()
    assert window.scan_button.isEnabled()
    assert not window.cancel_scan_button.isEnabled()
    assert window.kind_combo.currentData() == "bluetooth_spp"
    assert window.protocol_combo.currentData() == "PROJECT_V1"
    assert window.bluetooth_address.text() == "2A:A2:19:07:1B:8B"
    assert window.bluetooth_alias.text() == "我的小车"
    assert window.auto_connect_checkbox.isChecked()
    assert window.current_preferences() == AppPreferences()
    assert "我的小车" in window.profile_label.text()
    assert "2A:A2:19:07:1B:8B" in window.profile_label.text()
    assert window.left_value.text() == window.right_value.text() == "—"
    assert window.left_speed_caption.text() == "左轮有符号轮速 · m/s"
    assert window.right_speed_caption.text() == "右轮有符号轮速 · m/s"
    assert window.left_channel.text() == "左轮有符号轮速"
    assert window.right_channel.text() == "右轮有符号轮速"
    assert window.plot.getAxis("left").labelText == "有符号轮速"
    assert "等待 CAPS 握手和有效 STATE" in window.telemetry_note.text()
    assert "速度幅值" not in window.telemetry_note.text()
    assert window.rfcomm_channel.minimum() == 1
    assert window.rfcomm_channel.maximum() == 30
    assert window.rfcomm_channel.value() == 1
    assert "Windows" in window.bluetooth_note.text()


def test_injected_profile_restores_editable_fields_without_link(qapp, tmp_path):
    preferences = AppPreferences(BluetoothProfile(alias="台架二号", address="AA:BB:CC:DD:EE:02",
                                                   channel=3, protocol="LEGACY_APP", device_name="WHEELTEC"), False)
    host = HostController(tmp_path, worker_factory=Link, scanner_factory=Link)
    window = MainWindow(host, preferences=preferences)
    try:
        assert window.current_preferences() == preferences
        assert window.bluetooth_alias.text() == "台架二号"
        assert window.rfcomm_channel.value() == 3
        assert window.protocol_combo.currentData() == "LEGACY_APP"
        assert not window.auto_connect_checkbox.isChecked()
        assert host.worker is None and host.scanner is None
    finally:
        window.close()
        spin_until(qapp, host.shutdown)


class ProfileStore:
    def __init__(self, error=None):
        self.saved = []
        self.error = error

    def save(self, preferences):
        if self.error:
            raise self.error
        self.saved.append(preferences)


def test_profile_save_is_explicit_validated_and_sends_no_commands(fake_workbench):
    host, window = fake_workbench
    store = ProfileStore()
    window._preferences_store = store
    window.bluetooth_alias.setText("  台架二号  ")
    window.bluetooth_address.setText("aa-bb-cc-dd-ee-02")
    window.rfcomm_channel.setValue(3)
    window.auto_connect_checkbox.setChecked(False)
    assert store.saved == []
    assert "我的小车" in window.profile_label.text()
    window.save_profile_button.click()
    assert len(store.saved) == 1
    saved = store.saved[0]
    assert saved.profile.alias == "台架二号"
    assert saved.profile.address == "AA:BB:CC:DD:EE:02"
    assert saved.profile.channel == 3
    assert saved.profile.protocol == "PROJECT_V1"
    assert saved.profile.device_name == ""
    assert not saved.auto_connect
    assert window.bluetooth_alias.text() == "台架二号"
    assert window.bluetooth_address.text() == "AA:BB:CC:DD:EE:02"
    assert "台架二号" in window.profile_label.text()
    assert "AA:BB:CC:DD:EE:02" in window.profile_label.text()
    assert host.worker is None and host.scanner is None


def test_invalid_profile_save_preserves_previous_profile_and_is_visible(fake_workbench):
    host, window = fake_workbench
    store = ProfileStore()
    window._preferences_store = store
    window.bluetooth_address.setText("bad MAC")
    window.save_profile_button.click()
    assert store.saved == []
    assert "保存失败" in window.log_view.toPlainText()
    assert "2A:A2:19:07:1B:8B" in window.profile_label.text()
    window.bluetooth_address.setText("2A:A2:19:07:1B:8B")
    window.bluetooth_alias.setText(" ")
    with pytest.raises(ValueError):
        window.current_preferences()
    assert host.worker is None


def test_profile_storage_error_is_visible_and_does_not_change_profile(fake_workbench):
    host, window = fake_workbench
    window._preferences_store = ProfileStore(OSError("只读目录"))
    window.bluetooth_alias.setText("新别名")
    window.save_profile_button.click()
    assert "只读目录" in window.log_view.toPlainText()
    assert "我的小车" in window.profile_label.text()
    assert host.worker is None and host.scanner is None


def test_simulation_selects_legacy_and_restores_previous_real_protocol(fake_workbench):
    host, window = fake_workbench
    window.kind_combo.setCurrentIndex(window.kind_combo.findData("simulation"))
    assert window.protocol_combo.currentData() == "LEGACY_APP"
    assert not window.protocol_combo.isEnabled()
    assert window.left_speed_caption.text() == "左轮速度幅值 · m/s"
    assert window.right_channel.text() == "右轮速度幅值"
    assert window.plot.getAxis("left").labelText == "速度幅值"
    assert "LEGACY_APP 只反馈速度幅值" in window.telemetry_note.text()
    assert window.current_preferences().profile.protocol == "PROJECT_V1"
    host.changed.emit()
    assert not window.protocol_combo.isEnabled()
    window.kind_combo.setCurrentIndex(window.kind_combo.findData("serial"))
    assert window.protocol_combo.currentData() == "PROJECT_V1"
    assert window.plot.getAxis("left").labelText == "有符号轮速"
    assert "等待 CAPS 握手和有效 STATE" in window.telemetry_note.text()
    window.protocol_combo.setCurrentIndex(window.protocol_combo.findData("LEGACY_APP"))
    assert window.plot.getAxis("left").labelText == "速度幅值"
    assert "LEGACY_APP 只反馈速度幅值" in window.telemetry_note.text()
    window.kind_combo.setCurrentIndex(window.kind_combo.findData("simulation"))
    window.kind_combo.setCurrentIndex(window.kind_combo.findData("bluetooth_spp"))
    assert window.protocol_combo.currentData() == "LEGACY_APP"


def test_connection_interactions_emit_but_refresh_does_not(fake_workbench):
    host, window = fake_workbench
    interactions = []
    window.connection_interacted.connect(lambda: interactions.append(1))
    host.changed.emit()
    window.refresh_state()
    assert interactions == []
    for edit in (
        lambda: window.bluetooth_alias.setText("另一台小车"),
        lambda: window.auto_connect_checkbox.setChecked(False),
        lambda: window.bluetooth_address.setText("AA:BB:CC:DD:EE:02"),
        lambda: window.rfcomm_channel.setValue(2),
        lambda: window.protocol_combo.setCurrentIndex(window.protocol_combo.findData("LEGACY_APP")),
        lambda: window.kind_combo.setCurrentIndex(window.kind_combo.findData("serial")),
        lambda: window.kind_combo.setCurrentIndex(window.kind_combo.findData("bluetooth_spp")),
        lambda: window.scan_button.click(),
        lambda: window.cancel_scan_button.click(),
    ):
        count = len(interactions)
        edit()
        assert len(interactions) > count
    host.poll()
    count = len(interactions)
    window.connect_button.click()
    assert len(interactions) > count
    host.poll()
    count = len(interactions)
    window.disconnect_button.click()
    assert len(interactions) > count
    host.poll()
    count = len(interactions)
    window.close()
    assert len(interactions) > count


def test_manual_mac_connect_ignores_serial_baud_and_identifies_wireless(fake_workbench):
    host, window = fake_workbench
    window.kind_combo.setCurrentIndex(2)
    window.protocol_combo.setCurrentIndex(window.protocol_combo.findData("LEGACY_APP"))
    window.baud_combo.setEditText("not a baud")
    window.bluetooth_address.setText("aa-bb-cc-dd-ee-ff")
    window.rfcomm_channel.setValue(3)
    window.connect_button.click()
    host.poll()
    assert host.config.bluetooth_address == "AA:BB:CC:DD:EE:FF"
    assert host.config.rfcomm_channel == 3
    assert host.config.kind == "bluetooth_spp"
    assert window.source_badge.text() == "蓝牙 SPP"
    assert window.telemetry_value.text() == "等待有效遥测"
    assert not window.scan_button.isEnabled()
    assert not window.bluetooth_address.isEnabled()
    assert window.send_button.isEnabled()
    host.worker.emit("rx", b"{C10:20:90}$")
    host.poll()
    assert window.left_value.text() == "0.10"
    assert window.right_value.text() == "0.20"


def test_compact_mac_manual_connect_uses_validated_profile_without_scan(fake_workbench):
    host, window = fake_workbench
    window.bluetooth_address.setText("2aa219071b8b")
    window.connect_button.click()
    host.poll()
    assert host.config.bluetooth_address == "2A:A2:19:07:1B:8B"
    assert host.config.device_name == "WHEELTEC"
    assert host.config.protocol == "PROJECT_V1"
    assert host.scanner is None
    assert len(host.worker.sent) == 1
    assert host.worker.sent[0][0][3] == 1
    assert window.left_speed_caption.text() == "左轮有符号轮速 · m/s"
    assert window.left_value.text() == "—"


def test_scan_selection_distinguishes_same_names_and_filters_locally(fake_workbench):
    host, window = fake_workbench
    window.kind_combo.setCurrentIndex(2)
    window.scan_button.click()
    assert host.bluetooth_scanning
    assert not window.connect_button.isEnabled()
    assert window.cancel_scan_button.isEnabled()
    scanner = host.scanner
    for name, address in (("WHEELTEC", "AA:BB:CC:DD:EE:01"), ("WHEELTEC", "AA:BB:CC:DD:EE:02"),
                          ("Other", "AA:BB:CC:DD:EE:03")):
        scanner.emit("device", address.encode(), name)
    host.poll()
    assert window.bluetooth_device_combo.count() == 3
    window.bluetooth_filter.setText("wheel")
    assert window.bluetooth_device_combo.count() == 2
    index = window.bluetooth_device_combo.findData("AA:BB:CC:DD:EE:02")
    window.bluetooth_device_combo.setCurrentIndex(index)
    assert window.bluetooth_address.text() == "AA:BB:CC:DD:EE:02"
    assert window.current_preferences().profile.device_name == "WHEELTEC"
    assert "2A:A2:19:07:1B:8B" in window.profile_label.text()
    window.bluetooth_filter.setText("aa-bb-cc-dd-ee-02")
    assert window.bluetooth_device_combo.count() == 1
    assert window.bluetooth_device_combo.itemData(0) == "AA:BB:CC:DD:EE:02"
    window.cancel_scan_button.click()
    host.poll()
    assert not host.bluetooth_scanning
    assert window.connect_button.isEnabled()


def test_invalid_mac_is_visible_and_no_link_is_created(fake_workbench):
    host, window = fake_workbench
    window.kind_combo.setCurrentIndex(2)
    window.bluetooth_address.setText("bad MAC")
    window.connect_button.click()
    assert host.worker is None
    assert "MAC" in window.log_view.toPlainText()


def test_pid_controls_remain_unavailable_before_wireless_caps(fake_workbench):
    host, window = fake_workbench
    window.kind_combo.setCurrentIndex(2)
    window.bluetooth_address.setText("AA:BB:CC:DD:EE:FF")
    window.connect_button.click()
    host.poll()
    assert not window.pid_button.isEnabled()
    assert "CAPS" in window.pid_button.toolTip()
    assert "PID" in window.tool_reason_label.text()
    assert len(host.worker.sent) == 1
    assert host.worker.sent[0][0][3] == 1  # HELLO only; no parameter or motion command.


def test_raw_send_controls_follow_device_capabilities(fake_workbench):
    host, window = fake_workbench
    window.kind_combo.setCurrentIndex(1)
    window.connect_button.click()
    host.poll()
    host.capabilities = replace(host.capabilities, raw_send=False, reason="只读设备")
    host.changed.emit()
    assert not window.send_button.isEnabled()
    assert window.send_button.toolTip() == "只读设备"


def test_close_waits_asynchronously_and_automatically_retries(fake_workbench, qapp):
    from PySide6.QtCore import QTimer

    host, window = fake_workbench
    window.kind_combo.setCurrentIndex(1)
    window.connect_button.click()
    host.poll()
    link = host.worker
    link.stop_immediately = False
    beats = []
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: beats.append(1))
    heartbeat.start()
    started = time.monotonic()
    assert not window.close()
    assert time.monotonic() - started < .1
    assert window.isVisible() and window._close_retry_timer.isActive()
    spin_until(qapp, lambda: len(beats) >= 5)
    assert not window.send_button.isEnabled()
    link.finished = True
    link.emit("closed")
    spin_until(qapp, lambda: not window.isVisible())
    heartbeat.stop()
    assert host.worker is None and not host.timer.isActive()
