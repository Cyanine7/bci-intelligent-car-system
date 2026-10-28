"""Capability-aware registration for independent future feature panels."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .device import CAPABILITY_NAMES, DeviceCapabilities


@dataclass(frozen=True)
class ToolSpec:
    id: str
    label: str
    required_capabilities: tuple[str, ...] = ()
    factory: Callable[[Any], Any] | None = None
    protocols: tuple[str, ...] = ()

    @property
    def tool_id(self) -> str:
        return self.id

    @property
    def title(self) -> str:
        return self.label


@dataclass(frozen=True)
class ToolAvailability:
    enabled: bool
    reason: str = ""


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not isinstance(spec, ToolSpec):
            raise TypeError("工具定义必须是 ToolSpec")
        if not spec.id or spec.id != spec.id.strip() or not spec.label:
            raise ValueError("工具标识和显示名称不能为空")
        if spec.id in self._tools:
            raise ValueError(f"工具已注册：{spec.id}")
        if any(name not in CAPABILITY_NAMES for name in spec.required_capabilities):
            raise ValueError("工具声明了未识别的设备能力")
        if spec.factory is not None and not callable(spec.factory):
            raise TypeError("面板工厂必须可调用")
        self._tools[spec.id] = spec

    def tools(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def get(self, tool_id: str) -> ToolSpec:
        return self._tools[tool_id]

    def availability(self, tool_id: str, capabilities: DeviceCapabilities) -> ToolAvailability:
        spec = self.get(tool_id)
        if spec.protocols and capabilities.protocol not in spec.protocols:
            return ToolAvailability(False, f"{spec.label}需要{' / '.join(spec.protocols)}；当前{capabilities.protocol}")
        for name in spec.required_capabilities:
            if not capabilities.supports(name):
                return ToolAvailability(False, capabilities.reason_for(name))
        if spec.factory is None:
            return ToolAvailability(False, f"{spec.label}面板尚未实现；需完成 MCU 协议与服务联调")
        return ToolAvailability(True)

    def create_panel(self, tool_id: str, gateway: Any) -> Any:
        available = self.availability(tool_id, gateway.capabilities)
        if not available.enabled:
            raise RuntimeError(available.reason)
        factory = self.get(tool_id).factory
        assert factory is not None
        return factory(gateway)


def create_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    def motion_panel(gateway, mode):
        from .project_panels import MotionPanel
        return MotionPanel(gateway, mode)

    def parameters_panel(gateway):
        from .project_panels import ParameterPanel
        return ParameterPanel(gateway)

    def encoder_panel(gateway):
        from .encoder_panel import EncoderTotalsPanel
        return EncoderTotalsPanel(gateway)

    def speed_panel(gateway):
        from .speed_control_panel import SpeedControlPanel
        return SpeedControlPanel(gateway)

    for spec in (
        ToolSpec("open_loop", "开环实验", ("open_loop",), lambda gateway: motion_panel(gateway, 1)),
        ToolSpec("pid", "PID / PI调参", ("parameter_read", "parameter_write"), parameters_panel),
        ToolSpec("motion", "运动控制", ("motion",), lambda gateway: motion_panel(gateway, 2)),
        ToolSpec("encoder", "全量编码器 · 五圈计数", ("telemetry",), encoder_panel, ("PROJECT_V1",)),
        # Offline model/PI design is available before connecting. Device actions
        # are gated by fresh STATE and negotiated capabilities inside the panel.
        ToolSpec("speed_control", "速度环实验", (), speed_panel),
    ):
        registry.register(spec)
    return registry
