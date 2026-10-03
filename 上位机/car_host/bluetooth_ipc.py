"""Bounded, versioned JSON-lines messages for the isolated Bluetooth process."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re

MAX_PAYLOAD_BYTES = 4096
MAX_LINE_BYTES = 16384
MAX_PIPE_BYTES = 65536
MAX_MESSAGE_CHARS = 2048
IPC_VERSION = 1
COMMAND_KINDS = frozenset({"scan", "connect", "send", "stop"})
EVENT_KINDS = frozenset({"opened", "rx", "tx", "device", "status", "error", "closed"})
_MAC = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\Z")


class IpcError(ValueError):
    """The process boundary received malformed or excessive input."""


def normalize_address(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("蓝牙 MAC 地址必须为文本")
    normalized = value.strip().replace("-", ":").upper()
    if not _MAC.fullmatch(normalized) or normalized == "00:00:00:00:00:00":
        raise ValueError("蓝牙 MAC 地址格式应为 AA:BB:CC:DD:EE:FF")
    return normalized


def validate_channel(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 30:
        raise ValueError("RFCOMM 通道必须为 1–30 的整数")
    return value


def _identifier(value, label: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise IpcError(f"{label}无效")
    return value


def validate_message(message: dict, *, commands: bool | None = None) -> dict:
    if not isinstance(message, dict):
        raise IpcError("IPC 消息必须为对象")
    if type(message.get("v")) is not int or message["v"] != IPC_VERSION:
        raise IpcError("IPC 版本不支持")
    kinds = COMMAND_KINDS if commands is True else EVENT_KINDS if commands is False else COMMAND_KINDS | EVENT_KINDS
    if not isinstance(message.get("kind"), str) or message["kind"] not in kinds:
        raise IpcError("IPC 消息类型不支持")
    _identifier(message.get("connection_id"), "连接标识")
    _identifier(message.get("request_id", ""), "请求标识")
    value = message.get("message", "")
    if not isinstance(value, str) or len(value) > MAX_MESSAGE_CHARS:
        raise IpcError("IPC 说明文本过长或无效")
    payload = message.get("payload", "")
    if not isinstance(payload, str) or len(payload) > ((MAX_PAYLOAD_BYTES + 2) // 3) * 4:
        raise IpcError("IPC 载荷过长或无效")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise IpcError("IPC 载荷不是有效 base64") from exc
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise IpcError("IPC 载荷超过 4096 字节")
    if "monotonic" in message:
        timestamp = message["monotonic"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise IpcError("IPC 时间戳无效")
    if "utc" in message and (not isinstance(message["utc"], str) or len(message["utc"]) > 64):
        raise IpcError("IPC UTC 时间戳无效")
    return message


def encode_message(kind: str, connection_id: str, *, request_id: str = "", payload: bytes = b"", **fields) -> bytes:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise IpcError("IPC 载荷必须为字节")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise IpcError("IPC 载荷超过 4096 字节")
    message = dict(fields, v=IPC_VERSION, kind=kind, connection_id=connection_id,
                   request_id=request_id, payload=base64.b64encode(payload).decode("ascii"))
    validate_message(message)
    line = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
    if len(line) > MAX_LINE_BYTES:
        raise IpcError("IPC 消息行过长")
    return line


def decode_payload(message: dict) -> bytes:
    return base64.b64decode(message.get("payload", ""), validate=True)


class MessageDecoder:
    """Only the incomplete final line is retained; oversized lines are fatal."""

    def __init__(self, *, commands: bool | None = None):
        self._buffer = bytearray()
        self.commands = commands

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[dict]:
        if len(data) > MAX_PIPE_BYTES:
            raise IpcError("IPC 输入突发超过缓存上限")
        self._buffer.extend(data)
        messages = []
        while True:
            boundary = self._buffer.find(b"\n")
            if boundary < 0:
                if len(self._buffer) >= MAX_LINE_BYTES:
                    self._buffer.clear()
                    raise IpcError("IPC 未完成行超过上限")
                break
            if boundary + 1 > MAX_LINE_BYTES:
                self._buffer.clear()
                raise IpcError("IPC 消息行超过上限")
            line = bytes(self._buffer[:boundary])
            del self._buffer[:boundary + 1]
            if not line:
                raise IpcError("IPC 消息行为空")
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError) as exc:
                raise IpcError("IPC 消息不是有效 JSON") from exc
            messages.append(validate_message(message, commands=self.commands))
        return messages

    def finish(self) -> None:
        if self._buffer:
            self._buffer.clear()
            raise IpcError("IPC 在半包处结束")
