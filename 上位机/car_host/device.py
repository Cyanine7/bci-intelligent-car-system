"""Device semantics, separate from byte transports and widgets.

Protocol registration is an application-startup operation. The shipped legacy
profile describes locally known behavior; it is not a MCU CAPS response.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .models import TelemetrySample
from .protocol import LEGACY_APP, LegacyTelemetryAdapter
from .project_protocol import PROJECT_V1, ProjectV1Adapter


CAPABILITY_NAMES = (
    "telemetry", "raw_send", "parameter_read", "parameter_write", "open_loop", "motion",
)


@dataclass(frozen=True)
class DeviceCapabilities:
    protocol: str = "UNKNOWN"
    telemetry: bool = False
    raw_send: bool = False
    parameter_read: bool = False
    parameter_write: bool = False
    open_loop: bool = False
    motion: bool = False
    reason: str = "设备尚未声明此能力"

    def supports(self, name: str) -> bool:
        return name in CAPABILITY_NAMES and bool(getattr(self, name))

    def reason_for(self, name: str) -> str:
        if self.supports(name):
            return ""
        if name not in CAPABILITY_NAMES:
            return f"未识别的设备能力：{name}"
        return self.reason or f"当前设备不支持 {name}"


@dataclass(frozen=True)
class DeviceResult:
    """A service result; queued success never means MCU acceptance or effect."""

    success: bool
    status: str
    message: str
    request_id: str | None = None
    values: Mapping[str, Any] | None = None


class ProtocolAdapter(Protocol):
    protocol: str

    def feed(
        self,
        chunk: bytes,
        received_monotonic: float | None = None,
        received_utc: str | None = None,
    ) -> list[TelemetrySample]: ...

    def reset(self) -> None: ...

    def pop_issues(self) -> list[str]: ...


class CommandGateway(Protocol):
    @property
    def capabilities(self) -> DeviceCapabilities: ...

    def send_command(
        self, command: str, values: Mapping[str, Any] | None = None,
    ) -> DeviceResult: ...


@dataclass(frozen=True)
class _ProtocolRegistration:
    factory: Callable[[str], ProtocolAdapter]
    capabilities: DeviceCapabilities


_PROTOCOLS: dict[str, _ProtocolRegistration] = {}


def register_protocol_adapter(
    protocol: str,
    factory: Callable[[str], ProtocolAdapter],
    capabilities: DeviceCapabilities | None = None,
) -> None:
    """Register an explicitly selected protocol; never probe by sending bytes.

Future adapters can additionally implement ``encode_command(command, values)``.
Only a capability-approved command gateway calls that optional entry point.
Actual wire layouts and acknowledgement handling belong to that future adapter.
"""
    if not isinstance(protocol, str) or not protocol or protocol != protocol.strip():
        raise ValueError("协议标识必须是非空且不含首尾空白的字符串")
    if not callable(factory):
        raise TypeError("协议工厂必须可调用")
    if protocol in _PROTOCOLS:
        raise ValueError(f"协议已注册：{protocol}")
    snapshot = capabilities or DeviceCapabilities()
    if not isinstance(snapshot, DeviceCapabilities):
        raise TypeError("设备能力必须是 DeviceCapabilities")
    _PROTOCOLS[protocol] = _ProtocolRegistration(factory, replace(snapshot, protocol=protocol))


def capabilities_for_protocol(protocol: str) -> DeviceCapabilities:
    try:
        return _PROTOCOLS[protocol].capabilities
    except KeyError as exc:
        raise ValueError(f"未注册的协议：{protocol}") from exc


def create_protocol_adapter(protocol: str, source: str) -> ProtocolAdapter:
    try:
        registration = _PROTOCOLS[protocol]
    except KeyError as exc:
        raise ValueError(f"未注册的协议：{protocol}") from exc
    adapter = registration.factory(source)
    for name in ("feed", "reset", "pop_issues"):
        if not callable(getattr(adapter, name, None)):
            raise TypeError(f"协议适配器缺少 {name} 接口")
    if getattr(adapter, "protocol", None) != protocol:
        raise ValueError("适配器协议标识与注册标识不一致")
    return adapter


class ParameterService:
    """A small semantic boundary, with no PID assumptions or wire encoding."""

    def __init__(self, controller_or_gateway: CommandGateway):
        self.gateway = controller_or_gateway

    def read_parameters(self) -> DeviceResult:
        capabilities = self.gateway.capabilities
        if not capabilities.supports("parameter_read"):
            return DeviceResult(False, "unsupported", capabilities.reason_for("parameter_read"))
        return self.gateway.send_command("read_parameters")

    def apply_parameters(self, values: Mapping[str, Any]) -> DeviceResult:
        capabilities = self.gateway.capabilities
        if not capabilities.supports("parameter_write"):
            return DeviceResult(False, "unsupported", capabilities.reason_for("parameter_write"))
        if not isinstance(values, Mapping) or not values:
            return DeviceResult(False, "invalid", "参数修改必须为非空键值映射")
        if any(not isinstance(key, str) or not key.strip() for key in values):
            return DeviceResult(False, "invalid", "参数标识必须为非空字符串")
        # Copy the caller's mapping; units/ranges and application conditions are
        # validated by the real parameter descriptor and gateway once frozen.
        return self.gateway.send_command("apply_parameters", dict(values))


class MotionService:
    """Single finite experiments; no lease renewal, repetition or auto arming."""

    def __init__(self, gateway: CommandGateway):
        self.gateway = gateway

    def arm_pwm(self):
        return self.gateway.send_command("arm_pwm")

    def arm_speed(self):
        return self.gateway.send_command("arm_speed")

    def set_pwm(self, left: int, right: int, duration_ms: int):
        return self.gateway.send_command("set_pwm", {"left": left, "right": right, "duration_ms": duration_ms})

    def set_speed(self, left_mm_s: int, right_mm_s: int, duration_ms: int):
        return self.gateway.send_command("set_speed", {"left": left_mm_s, "right": right_mm_s, "duration_ms": duration_ms})

    def stop(self):
        return self.gateway.send_command("stop")


register_protocol_adapter(
    LEGACY_APP,
    LegacyTelemetryAdapter,
    DeviceCapabilities(
        protocol=LEGACY_APP,
        telemetry=True,
        raw_send=True,
        reason="当前 LEGACY_APP 只提供速度幅值/估算电量遥测及原始发送；MCU 尚无参数读写、开环或运动命令协议",
    ),
)

register_protocol_adapter(
    PROJECT_V1, ProjectV1Adapter,
    DeviceCapabilities(protocol=PROJECT_V1,
                       reason="PROJECT_V1 等待有效 CAPS 握手；原始发送已禁用"),
)
