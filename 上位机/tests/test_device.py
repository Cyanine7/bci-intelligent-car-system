"""Device capability and extension boundaries without real hardware."""

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from car_host.device import (
    DeviceCapabilities,
    DeviceResult,
    ParameterService,
    capabilities_for_protocol,
    create_protocol_adapter,
    register_protocol_adapter,
)
from car_host.protocol import LEGACY_APP, LegacyTelemetryAdapter
from car_host.tools import ToolRegistry, ToolSpec, create_default_tool_registry


class FakeGateway:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.sent_commands = []
        self.send_queue = []

    def send_command(self, command, values=None):
        self.sent_commands.append((command, values))
        self.send_queue.append((command, values))
        return DeviceResult(True, "queued", "已进入发送队列；未确认 MCU 接受或生效", "request-1")


def test_legacy_capabilities_are_frozen_and_never_enable_pid():
    capabilities = capabilities_for_protocol(LEGACY_APP)
    assert capabilities.telemetry and capabilities.raw_send
    assert not capabilities.parameter_read
    assert not capabilities.parameter_write
    assert not capabilities.open_loop
    assert not capabilities.motion
    assert capabilities.supports("telemetry")
    assert capabilities.reason_for("telemetry") == ""
    assert "MCU" in capabilities.reason_for("parameter_read")
    assert not capabilities.supports("unknown_feature")
    with pytest.raises(FrozenInstanceError):
        capabilities.parameter_write = True


def test_factory_preserves_incremental_parser_and_source_across_transports():
    for source in ("SERIAL", "BLUETOOTH_SPP", "SIMULATOR"):
        adapter = create_protocol_adapter(LEGACY_APP, source)
        assert isinstance(adapter, LegacyTelemetryAdapter)
        assert adapter.feed(b"{C12:") == []
        samples = adapter.feed(b"15:80}$", 100.0, "2026-10-03T00:00:00.000Z")
        assert samples[0].source == source
        assert samples[0].left_speed_abs_mps == 0.12
        assert samples[0].right_speed_abs_mps == 0.15
        assert adapter.pop_issues() == []
        adapter.reset()
        assert adapter.feed(b"80}$") == []


def test_project_protocol_is_registered_but_features_require_real_caps():
    from car_host.project_protocol import ProjectV1Adapter
    assert isinstance(create_protocol_adapter("PROJECT_V1", "SERIAL"), ProjectV1Adapter)
    assert not capabilities_for_protocol("PROJECT_V1").motion
    assert not capabilities_for_protocol("PROJECT_V1").parameter_write
    with pytest.raises(ValueError, match="未注册"):
        create_protocol_adapter("UNDEFINED_PROTOCOL", "SERIAL")
    with pytest.raises(ValueError, match="未注册"):
        capabilities_for_protocol("UNDEFINED_PROTOCOL")


def test_legacy_parameter_requests_are_rejected_without_entering_send_queue():
    gateway = FakeGateway(capabilities_for_protocol(LEGACY_APP))
    service = ParameterService(gateway)
    for result in (service.read_parameters(), service.apply_parameters({"example": 1})):
        assert not result.success
        assert result.status == "unsupported"
        assert "LEGACY_APP" in result.message
        assert result.request_id is None
    assert gateway.sent_commands == []
    assert gateway.send_queue == []


def test_parameter_service_uses_current_capability_snapshot():
    gateway = FakeGateway(DeviceCapabilities(parameter_read=True, parameter_write=True))
    service = ParameterService(gateway)
    assert service.read_parameters().status == "queued"
    values = {"fixture.coefficient": 1.25}
    result = service.apply_parameters(values)
    assert result.success and result.request_id == "request-1"
    values["fixture.coefficient"] = 9
    assert gateway.sent_commands == [
        ("read_parameters", None),
        ("apply_parameters", {"fixture.coefficient": 1.25}),
    ]
    gateway.capabilities = DeviceCapabilities(reason="新连接尚未确认参数能力")
    assert service.read_parameters().status == "unsupported"
    assert len(gateway.sent_commands) == 2


@pytest.mark.parametrize("values", [{}, None, [], {"": 1}, {1: 2}])
def test_parameter_shape_validation_does_not_guess_units_or_emit_commands(values):
    gateway = FakeGateway(DeviceCapabilities(parameter_write=True))
    result = ParameterService(gateway).apply_parameters(values)
    assert result.status == "invalid"
    assert not result.success
    assert gateway.send_queue == []


def test_protocol_registration_can_supply_future_semantics_without_changing_transport():
    protocol = "TEST_PROTOCOL_" + uuid4().hex

    class FixtureAdapter(LegacyTelemetryAdapter):
        def encode_command(self, command, values=None):
            # This test-only marker is not a PROJECT_V1 or real MCU frame.
            return b"fixture-only"

    FixtureAdapter.protocol = protocol
    register_protocol_adapter(
        protocol,
        FixtureAdapter,
        DeviceCapabilities(telemetry=True, parameter_read=True, parameter_write=True),
    )
    adapter = create_protocol_adapter(protocol, "BLUETOOTH_SPP")
    assert adapter.source == "BLUETOOTH_SPP"
    assert adapter.encode_command("read_parameters") == b"fixture-only"
    assert capabilities_for_protocol(protocol).protocol == protocol
    assert capabilities_for_protocol(protocol).parameter_read
    with pytest.raises(ValueError, match="已注册"):
        register_protocol_adapter(protocol, FixtureAdapter)


def test_registration_without_capabilities_defaults_to_disabled():
    protocol = "TEST_DEFAULT_" + uuid4().hex
    register_protocol_adapter(protocol, lambda source: None)
    assert not capabilities_for_protocol(protocol).parameter_write
    with pytest.raises(TypeError, match="feed"):
        create_protocol_adapter(protocol, "SERIAL")


def test_default_tools_remain_disabled_with_explanations():
    registry = create_default_tool_registry()
    capabilities = capabilities_for_protocol(LEGACY_APP)
    assert [tool.id for tool in registry.tools()] == ["open_loop", "pid", "motion"]
    for spec in registry.tools():
        assert spec.id == spec.tool_id
        assert spec.label == spec.title
        available = registry.availability(spec.id, capabilities)
        assert not available.enabled and available.reason
        with pytest.raises(RuntimeError, match="LEGACY_APP"):
            registry.create_panel(spec.id, FakeGateway(capabilities))
    assert registry.availability("pid", DeviceCapabilities(parameter_read=True, parameter_write=True)).enabled


def test_registered_panel_factory_runs_only_with_required_capabilities():
    registry = ToolRegistry()
    created = []
    registry.register(ToolSpec("fixture", "测试面板", ("parameter_read",), lambda gateway: created.append(gateway) or "panel"))
    gateway = FakeGateway(DeviceCapabilities())
    with pytest.raises(RuntimeError):
        registry.create_panel("fixture", gateway)
    assert created == []
    gateway.capabilities = DeviceCapabilities(parameter_read=True)
    assert registry.availability("fixture", gateway.capabilities).enabled
    assert registry.create_panel("fixture", gateway) == "panel"
    assert created == [gateway]
    with pytest.raises(ValueError, match="已注册"):
        registry.register(ToolSpec("fixture", "重复"))
    with pytest.raises(ValueError, match="未识别"):
        registry.register(ToolSpec("invalid", "错误能力", ("magic",)))
