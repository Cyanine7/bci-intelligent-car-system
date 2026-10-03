from dataclasses import replace

import pytest

from car_host.preferences import AppPreferences
from car_host.startup import StartupAutoConnector
from car_host.controller import HostController
from car_host.project_protocol import HELLO
from car_host.ui import MainWindow
from test_application_extensions import Link, spin


class Host:
    worker = scanner = None
    connected = _shutting_down = False

    def __init__(self):
        self.requests = []
        self.logs = []

    def add_log(self, message, level="INFO"):
        self.logs.append((message, level))

    def connect_device(self, config):
        self.requests.append(config)
        return False  # A failure must not arrange another attempt.


def test_startup_uses_saved_mac_exactly_once_after_failure(qapp):
    host = Host()
    connector = StartupAutoConnector(host, AppPreferences, delay_ms=0)
    connector.start()
    connector.start()
    qapp.processEvents()
    connector.start()
    qapp.processEvents()
    assert connector.attempted and not connector.pending
    assert len(host.requests) == 1
    config = host.requests[0]
    assert config.kind == "bluetooth_spp" and config.protocol == "PROJECT_V1"
    assert config.bluetooth_address == "2A:A2:19:07:1B:8B"


def test_disabled_startup_never_creates_link(qapp):
    host = Host()
    connector = StartupAutoConnector(host, lambda: AppPreferences(auto_connect=False), delay_ms=0)
    connector.start()
    qapp.processEvents()
    assert not host.requests and not connector.pending and not connector.attempted


def test_cancel_consumes_pending_attempt_for_this_launch(qapp):
    host = Host()
    connector = StartupAutoConnector(host, AppPreferences, delay_ms=0)
    connector.start()
    assert connector.pending
    connector.cancel()
    connector.start()
    qapp.processEvents()
    assert not host.requests and not connector.attempted


@pytest.mark.parametrize("field,value", [("worker", object()), ("scanner", object()),
                                        ("connected", True), ("_shutting_down", True)])
def test_existing_activity_blocks_startup_without_scan_handoff(qapp, field, value):
    host = Host()
    connector = StartupAutoConnector(host, AppPreferences, delay_ms=0)
    connector.start()
    setattr(host, field, value)
    qapp.processEvents()
    setattr(host, field, None if field in ("worker", "scanner") else False)
    connector.start()
    qapp.processEvents()
    assert not host.requests and not connector.attempted


def test_changed_profile_is_not_cached_before_attempt(qapp):
    host = Host()
    current = [AppPreferences()]
    connector = StartupAutoConnector(host, lambda: current[0], delay_ms=0)
    connector.start()
    current[0] = replace(current[0], profile=replace(current[0].profile, address="AA:BB:CC:DD:EE:FF"))
    qapp.processEvents()
    assert host.requests[0].bluetooth_address == "AA:BB:CC:DD:EE:FF"


def test_switching_off_before_callback_prevents_attempt(qapp):
    host = Host()
    current = [AppPreferences()]
    connector = StartupAutoConnector(host, lambda: current[0], delay_ms=0)
    connector.start()
    current[0] = replace(current[0], auto_connect=False)
    qapp.processEvents()
    assert not host.requests


def test_invalid_profile_at_callback_is_reported_without_connection(qapp):
    host = Host()
    calls = []

    def invalid_after_start():
        if calls:
            raise ValueError("invalid saved profile")
        calls.append(True)
        return AppPreferences()

    connector = StartupAutoConnector(host, invalid_after_start, delay_ms=0)
    connector.start()
    qapp.processEvents()
    assert not host.requests
    assert any("配置无效" in message for message, _ in host.logs)


@pytest.fixture
def real_host(qapp, tmp_path):
    links = []

    def create_link(config):
        link = Link(config)
        links.append(link)
        return link

    host = HostController(tmp_path, worker_factory=create_link, scanner_factory=Link)
    host.timer.stop()
    yield host, links
    spin(qapp, host.shutdown)


def test_auto_connection_only_handshakes_and_drop_requires_manual_retry(qapp, real_host):
    host, links = real_host
    # Same names must never change the target address or require discovery.
    host.bluetooth_devices = [("WHEELTEC", "AA:BB:CC:DD:EE:FF"),
                              ("WHEELTEC", "2A:A2:19:07:1B:8B")]
    preferences = AppPreferences()
    connector = StartupAutoConnector(host, lambda: preferences, delay_ms=0)
    connector.start()
    qapp.processEvents()
    host.poll()
    first = host.worker
    assert first.config == preferences.profile.to_connection_config()
    assert host.scanner is None and len(links) == 1
    assert [data[3] for data, _ in first.sent] == [HELLO]
    assert not host.capabilities.motion
    first.finished = True
    first.emit("closed", message="无线掉线")
    host.poll()
    for _ in range(3):
        connector.start()
        qapp.processEvents()
        host.poll()
    assert host.worker is None and len(links) == 1
    assert host.connect_device(preferences.profile.to_connection_config())
    host.poll()
    assert len(links) == 2 and host.worker.connection_id != first.connection_id
    assert [data[3] for data, _ in host.worker.sent] == [HELLO]


@pytest.mark.parametrize("action", ["connect", "scan", "alias", "mode", "protocol", "filter", "disable", "close"])
def test_ui_intervention_consumes_startup_attempt(qapp, real_host, action):
    host, links = real_host
    window = MainWindow(host)
    connector = StartupAutoConnector(host, window.current_preferences, delay_ms=0, parent=window)
    window.connection_interacted.connect(connector.cancel)
    window.show()
    connector.start()
    if action == "connect":
        window.connect_button.click()
    elif action == "scan":
        window.scan_button.click()
    elif action == "alias":
        window.bluetooth_alias.setText("台架小车")
    elif action == "mode":
        window.kind_combo.setCurrentIndex(window.kind_combo.findData("simulation"))
    elif action == "protocol":
        window.protocol_combo.setCurrentIndex(window.protocol_combo.findData("LEGACY_APP"))
    elif action == "filter":
        window.bluetooth_filter.setText("2A:A2")
    elif action == "disable":
        window.auto_connect_checkbox.setChecked(False)
    else:
        window.close()
    qapp.processEvents()
    host.poll()
    assert not connector.attempted and not connector.pending
    assert len(links) == (1 if action == "connect" else 0)
    window.close()
