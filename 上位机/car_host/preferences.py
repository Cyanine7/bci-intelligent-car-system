"""Bounded, versioned connection preferences without runtime or control state."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

from .transport import ConnectionConfig


SCHEMA_VERSION = 1
MAX_PREFERENCES_BYTES = 64 * 1024
MAX_ALIAS_LENGTH = 64
MAX_DEVICE_NAME_LENGTH = 248
SUPPORTED_PROTOCOLS = frozenset({"LEGACY_APP", "PROJECT_V1"})


def _display_text(value: str, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{label}不能为空")
    if len(text) > limit:
        raise ValueError(f"{label}不能超过 {limit} 个字符")
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError(f"{label}不能包含控制字符")
    return text


def _normalize_address(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("蓝牙 MAC 地址必须是文本")
    address = value.strip().upper()
    if address.replace(":", "").replace("-", "") == "000000000000":
        raise ValueError("蓝牙 MAC 地址不能为全零地址")
    if re.fullmatch(r"[0-9A-F]{12}", address):
        return ":".join(address[index:index + 2] for index in range(0, 12, 2))
    if re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
        return address
    if re.fullmatch(r"(?:[0-9A-F]{2}-){5}[0-9A-F]{2}", address):
        return address.replace("-", ":")
    raise ValueError("蓝牙 MAC 地址必须包含 6 组十六进制字节，例如 2A:A2:19:07:1B:8B")


@dataclass(frozen=True)
class BluetoothProfile:
    """A saved device identity; only its MAC determines the connection target."""

    alias: str = "我的小车"
    address: str = "2A:A2:19:07:1B:8B"
    channel: int = 1
    protocol: str = "PROJECT_V1"
    device_name: str = "WHEELTEC"

    def __post_init__(self) -> None:
        object.__setattr__(self, "alias", _display_text(self.alias, "本地别名", MAX_ALIAS_LENGTH))
        object.__setattr__(self, "address", _normalize_address(self.address))
        if type(self.channel) is not int or not 1 <= self.channel <= 30:
            raise ValueError("RFCOMM 通道必须是 1 到 30 的整数")
        if not isinstance(self.protocol, str):
            raise ValueError("协议必须是文本")
        protocol = self.protocol.strip().upper()
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError("协议必须为 LEGACY_APP 或 PROJECT_V1")
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "device_name", _display_text(
            self.device_name, "蓝牙设备名称", MAX_DEVICE_NAME_LENGTH, allow_empty=True,
        ))

    def to_connection_config(self) -> ConnectionConfig:
        return ConnectionConfig(
            kind="bluetooth_spp",
            bluetooth_address=self.address,
            device_name=self.device_name,
            rfcomm_channel=self.channel,
            protocol=self.protocol,
        )


@dataclass(frozen=True)
class AppPreferences:
    profile: BluetoothProfile = field(default_factory=BluetoothProfile)
    auto_connect: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.profile, BluetoothProfile):
            raise ValueError("设备配置必须是 BluetoothProfile")
        if type(self.auto_connect) is not bool:
            raise ValueError("启动自动连接开关必须是布尔值")


@dataclass(frozen=True)
class PreferencesLoadResult:
    preferences: AppPreferences
    warning: str = ""
    auto_connect_allowed: bool = True


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 含重复字段")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("JSON 含无效数值")


def _preferences_from_document(document: object) -> AppPreferences:
    if not isinstance(document, dict):
        raise ValueError("配置最外层必须是 JSON 对象")
    if type(document.get("schema_version")) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("配置版本不受支持，要求 schema_version=1")
    if set(document) != {"schema_version", "profile", "auto_connect"}:
        raise ValueError("配置字段缺失或不受支持")
    profile = document["profile"]
    if not isinstance(profile, dict) or set(profile) != {"alias", "address", "channel", "protocol", "device_name"}:
        raise ValueError("蓝牙设备配置字段缺失或不受支持")
    return AppPreferences(profile=BluetoothProfile(**profile), auto_connect=document["auto_connect"])


class PreferencesStore:
    """Read/write the explicit project file; no OS-wide preferences are used."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _failed_load(reason: str) -> PreferencesLoadResult:
        return PreferencesLoadResult(
            preferences=AppPreferences(auto_connect=False),
            warning=f"连接配置加载失败：{reason}。已使用默认配置并关闭本次启动自动连接，请确认后手动连接或重新保存。",
            auto_connect_allowed=False,
        )

    def load(self) -> PreferencesLoadResult:
        try:
            with self.path.open("rb") as stream:
                data = stream.read(MAX_PREFERENCES_BYTES + 1)
        except FileNotFoundError:
            return PreferencesLoadResult(AppPreferences())
        except OSError as error:
            return self._failed_load(f"无法读取文件（{error}）")
        if len(data) > MAX_PREFERENCES_BYTES:
            return self._failed_load("文件超过 64 KiB 上限")
        try:
            document = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_json_object,
                parse_constant=_reject_json_constant,
            )
            preferences = _preferences_from_document(document)
        except (ValueError, UnicodeError, RecursionError) as error:
            return self._failed_load(str(error))
        return PreferencesLoadResult(preferences)

    def save(self, preferences: AppPreferences) -> None:
        if not isinstance(preferences, AppPreferences):
            raise ValueError("保存内容必须是 AppPreferences")
        profile = preferences.profile
        document = {
            "schema_version": SCHEMA_VERSION,
            "profile": {
                "alias": profile.alias,
                "address": profile.address,
                "channel": profile.channel,
                "protocol": profile.protocol,
                "device_name": profile.device_name,
            },
            "auto_connect": preferences.auto_connect,
        }
        encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_PREFERENCES_BYTES:
            raise ValueError("连接配置超过 64 KiB 上限")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    # Cleanup must not hide the original write or replacement error.
                    pass
