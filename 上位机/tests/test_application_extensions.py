"""Transport-independent device services, identities and asynchronous shutdown."""

from collections import deque
from dataclasses import replace
import csv
import time
from uuid import uuid4

import pytest

from car_host.controller import HostController
from car_host.device import DeviceCapabilities, register_protocol_adapter
from car_host.protocol import LegacyTelemetryAdapter
from car_host.transport import ConnectionConfig, IoEvent


def spin(qapp, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


class Link:
    def __init__(self, config=None):
        self.config = config
        self.connection_id = uuid4().hex
        self.events = deque()
        self.sent = []
        self.finished = False
        self.stopped = False
        self.stop_immediately = True

    def emit(self, kind, payload=b"", message="", request_id="", connection_id=None):
        self.events.append(IoEvent(kind, payload, message, time.monotonic(),
                                   "2026-10-03T08:00:00.000Z",
                                   self.connection_id if connection_id is None else connection_id,
                                   request_id))

    def start(self):
        if self.config:
            self.emit("opened")

    def request_send(self, payload, request_id=""):
        if self.stopped:
            return False
        self.sent.append((payload, request_id))
        self.emit("tx", payload, request_id=request_id)
        return True

    def request_stop(self):
        self.stopped = True
        if self.stop_immediately and not self.finished:
            self.finished = True
            self.emit("closed", message="扫描结束" if self.config is None else "连接已关闭")

    def drain_events(self, max_count=256):
        return [self.events.popleft() for _ in range(min(max_count, len(self.events)))]

    def take_overflow_count(self):
        return 0

    def isFinished(self):
        return self.finished


@pytest.fixture
def host(qapp, tmp_path):
    controller = HostController(tmp_path, worker_factory=Link, scanner_factory=Link)
    controller.timer.stop()
    yield controller
    if controller.worker:
        controller.worker.stop_immediately = True
        controller.worker.request_stop()
    if controller.scanner:
        controller.scanner.stop_immediately = True
        controller.scanner.request_stop()
    spin(qapp, controller.shutdown)


def test_wireless_uses_legacy_parser_and_parameter_services_emit_no_guessed_bytes(host):
    assert host.connect_device(ConnectionConfig(kind="bluetooth_spp", bluetooth_address="aa-bb-cc-dd-ee-ff"))
    link = host.worker
    host.poll()
    assert host.connected and host.source == "BLUETOOTH_SPP"
    assert host.connection_status == "蓝牙SPP已连接"
    assert host.telemetry_status == "等待有效遥测"
    for chunk in (b"noise{C12:", b"34:88}${C56:78:90}$"):
        link.emit("rx", chunk)
        host.poll()
    assert host.frames_received == 2
    assert host.latest.left_speed_abs_mps == .56
    assert host.latest.connection_id == host.connection_id
    assert host.parameters.read_parameters().status == "unsupported"
    assert host.parameters.apply_parameters({"Kp": 1}).status == "unsupported"
    assert host.send_command("apply_parameters", {"Kp": 1}).status == "unsupported"
    assert link.sent == []
    assert host.send_manual("HELLO", False)
    host.poll()
    assert any("已写入蓝牙连接" in log.message for log in host.logs)
    assert link.sent[0][1] and any(log.request_id == link.sent[0][1] for log in host.logs)


def test_scan_deduplicates_caps_list_and_waits_for_cancel_before_connect(host):
    assert host.scan_bluetooth()
    scanner = host.scanner
    scanner.stop_immediately = False
    for index in range(150):
        scanner.emit("device", f"AA:BB:CC:DD:EE:{index:02X}".encode(), "WHEELTEC")
    scanner.emit("device", b"AA:BB:CC:DD:EE:00", "renamed")
    host.poll()
    assert len(host.bluetooth_devices) == 128
    assert ("renamed", "AA:BB:CC:DD:EE:00") in host.bluetooth_devices
    assert host.connect_device(ConnectionConfig(kind="bluetooth_spp", bluetooth_address="AA:BB:CC:DD:EE:00"))
    assert scanner.stopped and host.worker is None
    scanner.finished = True
    scanner.emit("closed", message="扫描已取消")
    host.poll()
    assert host.scanner is None and host.worker is not None and host.connected


def test_late_opened_does_not_revive_cancelled_connection_or_accept_other_session(host):
    assert host.connect_device(ConnectionConfig(kind="simulation"))
    link = host.worker
    link.stop_immediately = False
    host.disconnect_device()
    host.poll()
    assert not host.connected
    link.emit("opened")
    link.emit("rx", b"{C1:2:90}$", connection_id="retired-session")
    host.poll()
    assert not host.connected and host.latest is None
    assert not host.capabilities.raw_send
    link.finished = True
    link.emit("closed")
    host.poll()
    assert host.worker is None


def test_recording_connect_identity_survives_transport_switch(host, qapp):
    assert host.connect_device(ConnectionConfig(port="COM_TEST"))
    host.poll()
    assert host.start_recording()
    directory = host.recorder.directory
    identities = [host.connection_id]
    host.worker.emit("rx", b"{C10:20:90}$")
    host.poll()
    host.disconnect_device()
    host.poll()
    assert host.connect_device(ConnectionConfig(kind="bluetooth_spp", bluetooth_address="AA:BB:CC:DD:EE:FF", device_name="car"))
    host.poll()
    identities.append(host.connection_id)
    host.worker.emit("rx", b"{C30:40:89}$")
    host.poll()
    host.stop_recording()
    spin(qapp, lambda: not host.recorder.is_running)
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [row["connection_id"] for row in rows] == identities
    assert [row["source"] for row in rows] == ["SERIAL", "BLUETOOTH_SPP"]
    log = (directory / "communication.log").read_text(encoding="utf-8")
    assert "COM_TEST" in log and "AA:BB:CC:DD:EE:FF" in log and "rfcomm_channel" in log
    assert all(identity in log for identity in identities)


def test_protocol_extension_reuses_parameter_service_without_changing_link(host):
    name = "TEST_PARAMETERS_" + uuid4().hex

    class Adapter(LegacyTelemetryAdapter):
        protocol = name

        def encode_command(self, command, values):
            # An artificial test vector, deliberately not a MCU wire contract.
            return f"test:{command}".encode()

    register_protocol_adapter(name, Adapter, DeviceCapabilities(telemetry=True, raw_send=True,
                                                               parameter_read=True, parameter_write=True))
    assert host.connect_device(ConnectionConfig(kind="simulation", protocol=name))
    host.poll()
    result = host.parameters.apply_parameters({"test.gain": .5})
    assert result.success and result.status == "queued" and result.request_id
    assert host.worker.sent == [(b"test:apply_parameters", result.request_id)]
    host.disconnect_device()
    host.poll()
    assert not host.capabilities.parameter_write
    assert host.parameters.read_parameters().status == "unsupported"


def test_shutdown_returns_immediately_while_link_still_running(host):
    assert host.connect_device(ConnectionConfig(kind="simulation"))
    host.poll()
    link = host.worker
    link.stop_immediately = False
    started = time.monotonic()
    assert not host.shutdown()
    assert time.monotonic() - started < .1
    assert not host.connect_device(ConnectionConfig(kind="simulation"))
    link.finished = True
    link.emit("closed")
    assert host.shutdown()


def test_registered_protocol_can_disable_raw_sending_and_parameter_commands(host):
    name = "TEST_READONLY_" + uuid4().hex

    class Adapter(LegacyTelemetryAdapter):
        protocol = name

        def encode_command(self, command, values):
            raise AssertionError("Capability-denied commands must not reach the encoder")

    register_protocol_adapter(name, Adapter, DeviceCapabilities(telemetry=True, reason="只读测试设备"))
    assert host.connect_device(ConnectionConfig(kind="simulation", protocol=name))
    host.poll()
    assert host.connected
    assert not host.send_manual("HELLO", False)
    assert host.send_command("apply_parameters", {"gain": 1}).status == "unsupported"
    assert host.worker.sent == []
    assert any("只读测试设备" in log.message for log in host.logs)


def test_scan_error_remains_visible_after_closed(host):
    assert host.scan_bluetooth()
    host.scanner.emit("error", message="适配器已关闭")
    host.scanner.emit("closed", message="蓝牙扫描已结束")
    host.scanner.finished = True
    host.poll()
    assert host.bluetooth_scan_status == "适配器已关闭"


def test_virtual_thirty_minutes_record_all_frames_with_bounded_history(host, qapp, monkeypatch):
    """Advance software time; this is not a 30-minute radio soak test."""
    from types import SimpleNamespace
    import car_host.controller as controller_module

    now = [time.monotonic()]
    monkeypatch.setattr(controller_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    assert host.connect_device(ConnectionConfig(kind="bluetooth_spp", bluetooth_address="AA:BB:CC:DD:EE:FF"))
    host.poll()
    assert host.start_recording()
    directory = host.recorder.directory
    for batch in range(150):
        for _ in range(240):
            now[0] += .05
            host.worker.emit("rx", b"{C7:13:92}$")
            host.worker.events[-1] = replace(host.worker.events[-1], monotonic=now[0])
        host.poll()
        assert len(host.history.samples) <= 1201
        assert len(host.logs) <= 1000
        # Drain the finite recorder between accelerated batches instead of
        # pretending a burst faster than disk throughput is a 20 Hz workload.
        spin(qapp, lambda: host.recorder.sample_count == (batch + 1) * 240)
    assert host.frames_received == 36000
    assert host.receive_frequency == pytest.approx(20, rel=.01)
    assert not host.recording_incomplete
    host.stop_recording()
    spin(qapp, lambda: not host.recorder.is_running)
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as file:
        assert sum(1 for _ in csv.DictReader(file)) == 36000
    assert host.recorder.dropped_total == 0


@pytest.mark.parametrize("address,channel", [("", 1), ("bogus", 1), ("00:00:00:00:00:00", 1),
                                            ("AA:BB:CC:DD:EE:FF", 0), ("AA:BB:CC:DD:EE:FF", 31)])
def test_bad_bluetooth_config_is_rejected_without_creating_link(host, address, channel):
    assert not host.connect_device(ConnectionConfig(kind="bluetooth_spp", bluetooth_address=address,
                                                   rfcomm_channel=channel))
    assert host.worker is None
