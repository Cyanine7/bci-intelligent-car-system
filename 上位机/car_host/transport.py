"""Byte transports for the desktop tool; adapters contain no GUI logic."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Callable, Protocol


@dataclass(frozen=True)
class ConnectionConfig:
    kind: str = "serial"
    port: str = ""
    baudrate: int = 230400
    bluetooth_address: str = ""
    device_name: str = ""
    rfcomm_channel: int = 1
    protocol: str = "LEGACY_APP"


@dataclass(frozen=True)
class IoEvent:
    kind: str
    payload: bytes = b""
    message: str = ""
    monotonic: float = 0
    utc: str = ""
    connection_id: str = ""
    request_id: str = ""


def source_for_kind(kind: str) -> str:
    """Keep physical link identity independent of the selected MCU protocol."""
    sources = {"serial": "SERIAL", "simulation": "SIMULATOR", "bluetooth_spp": "BLUETOOTH_SPP"}
    try:
        return sources[kind]
    except KeyError:
        raise ValueError(f"不支持的连接方式：{kind}") from None


class TransportAdapter(Protocol):
    def open(self) -> None: ...

    def read(self, max_bytes: int = 4096) -> bytes: ...

    def write(self, payload: bytes) -> int: ...

    def close(self) -> None: ...


def list_serial_ports() -> list[tuple[str, str]]:
    """Enumerate OS serial ports, including Bluetooth virtual COM ports."""
    from serial.tools import list_ports

    return sorted((item.device, item.description) for item in list_ports.comports())


class SerialTransport:
    """A real serial adapter, owned and called exclusively by the I/O worker."""

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self._serial = None

    def open(self) -> None:
        if self._serial is not None:
            raise RuntimeError("串口已经打开")
        if not self.config.port:
            raise ValueError("请先选择串口")
        import serial

        device = serial.Serial(
            port=None,
            baudrate=self.config.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.02,
            write_timeout=0.3,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        # Set the requested line state before opening; never request a DTR reset.
        # Some OS drivers can still pulse lines during open; this is not a
        # guarantee about the physical USB-to-serial adapter.
        device.dtr = False
        device.rts = False
        device.port = self.config.port
        try:
            device.open()
        except Exception:
            device.close()
            raise
        self._serial = device

    def read(self, max_bytes: int = 4096) -> bytes:
        device = self._require_open()
        if max_bytes <= 0:
            return b""
        # Wait for one byte instead of waiting for a complete 4096-byte block.
        first = device.read(1)
        if not first:
            return b""
        waiting = min(device.in_waiting, max_bytes - 1)
        return first + (device.read(waiting) if waiting > 0 else b"")

    def write(self, payload: bytes) -> int:
        return self._require_open().write(payload)

    def close(self) -> None:
        device, self._serial = self._serial, None
        if device is not None:
            device.close()

    def _require_open(self):
        if self._serial is None:
            raise RuntimeError("串口尚未打开")
        return self._serial


class SimulationTransport:
    """Generate deterministic legacy telemetry, without simulating control."""

    FRAME_PERIOD = 0.05

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._clock = clock
        self._sleep = sleeper
        self._opened = False
        self._frame_number = 0
        self._next_due = 0.0
        self._pending = b""
        self.written_payloads: deque[bytes] = deque(maxlen=128)

    def open(self) -> None:
        if self._opened:
            raise RuntimeError("模拟连接已经打开")
        self._opened = True
        self._frame_number = 0
        self._pending = b""
        self.written_payloads.clear()
        self._next_due = self._clock() + self.FRAME_PERIOD

    def read(self, max_bytes: int = 4096) -> bytes:
        self._require_open()
        if max_bytes <= 0:
            return b""
        if self._pending:
            return self._take_pending(max_bytes)
        remaining = self._next_due - self._clock()
        if remaining > 0:
            self._sleep(min(remaining, 0.02))
        now = self._clock()
        if now + 1e-9 < self._next_due:
            return b""

        self._frame_number += 1
        number = self._frame_number
        left = 4 + self._triangle(number, 60) // 3
        right = 4 + self._triangle(number + 7, 60) // 3
        battery = 90 - (number // 200) % 6
        packet = f"{{C{left}:{right}:{battery}}}$".encode("ascii")
        self._next_due += self.FRAME_PERIOD
        if self._next_due <= now:
            # Do not create a backlog after a delayed read.
            self._next_due = now + self.FRAME_PERIOD
        self._pending = packet
        if number % 5 == 0:
            return self._take_pending(min(max_bytes, len(packet) // 2))
        return self._take_pending(max_bytes)

    def write(self, payload: bytes) -> int:
        self._require_open()
        self.written_payloads.append(bytes(payload))
        return len(payload)

    def close(self) -> None:
        self._opened = False
        self._pending = b""

    def _take_pending(self, max_bytes: int) -> bytes:
        result, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return result

    @staticmethod
    def _triangle(number: int, period: int) -> int:
        phase = number % period
        return phase if phase <= period // 2 else period - phase

    def _require_open(self) -> None:
        if not self._opened:
            raise RuntimeError("模拟连接尚未打开")


def create_adapter(config: ConnectionConfig) -> TransportAdapter:
    if config.kind == "simulation":
        return SimulationTransport()
    if config.kind == "serial":
        return SerialTransport(config)
    raise ValueError(f"未知连接类型：{config.kind}")
