from dataclasses import FrozenInstanceError
import json

import pytest

from car_host.preferences import (
    AppPreferences,
    BluetoothProfile,
    MAX_ALIAS_LENGTH,
    MAX_DEVICE_NAME_LENGTH,
    MAX_PREFERENCES_BYTES,
    PreferencesStore,
)
from car_host.transport import ConnectionConfig


def _document(**updates):
    document = {
        "schema_version": 1,
        "profile": {
            "alias": "我的小车",
            "address": "2A:A2:19:07:1B:8B",
            "channel": 1,
            "protocol": "PROJECT_V1",
            "device_name": "WHEELTEC",
        },
        "auto_connect": True,
    }
    document.update(updates)
    return document


def _write_document(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _assert_unsafe_load_suppressed(result):
    assert result.preferences == AppPreferences(auto_connect=False)
    assert result.preferences.auto_connect is False
    assert result.auto_connect_allowed is False
    assert "关闭本次启动自动连接" in result.warning


def test_missing_file_returns_confirmed_device_defaults_without_creating_file(tmp_path):
    path = tmp_path / "preferences.json"
    result = PreferencesStore(path).load()
    assert result.preferences == AppPreferences()
    assert result.preferences.profile == BluetoothProfile()
    assert result.preferences.profile.to_connection_config() == ConnectionConfig(
        kind="bluetooth_spp",
        bluetooth_address="2A:A2:19:07:1B:8B",
        device_name="WHEELTEC",
        rfcomm_channel=1,
        protocol="PROJECT_V1",
    )
    assert result.auto_connect_allowed is True
    assert result.warning == ""
    assert not path.exists()


def test_models_are_frozen_and_profiles_are_independent():
    first, second = AppPreferences(), AppPreferences()
    assert first.profile is not second.profile
    with pytest.raises(FrozenInstanceError):
        first.auto_connect = False
    with pytest.raises(FrozenInstanceError):
        first.profile.address = "00:11:22:33:44:55"


def test_roundtrip_contains_only_versioned_connection_preferences(tmp_path):
    path = tmp_path / "local" / "preferences.json"
    saved = AppPreferences(
        profile=BluetoothProfile(alias="实验车", address="ab-cd-ef-01-23-45", channel=30,
                                 protocol="LEGACY_APP", device_name="另一台 WHEELTEC"),
        auto_connect=False,
    )
    store = PreferencesStore(path)
    store.save(saved)
    result = store.load()
    assert result.preferences == saved
    assert result.auto_connect_allowed is True
    assert result.warning == ""
    assert json.loads(path.read_text(encoding="utf-8")) == _document(
        profile={"alias": "实验车", "address": "AB:CD:EF:01:23:45", "channel": 30,
                 "protocol": "LEGACY_APP", "device_name": "另一台 WHEELTEC"},
        auto_connect=False,
    )
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("address", [" 2a:a2:19:07:1b:8b ", "2a-a2-19-07-1b-8b", "2aa219071b8b"])
def test_address_normalization_is_used_for_connection(address):
    profile = BluetoothProfile(address=address, alias=" 小车 ", device_name=" WHEELTEC ", protocol=" project_v1 ")
    assert profile.address == "2A:A2:19:07:1B:8B"
    assert profile.alias == "小车"
    assert profile.device_name == "WHEELTEC"
    assert profile.to_connection_config().bluetooth_address == "2A:A2:19:07:1B:8B"
    assert profile.to_connection_config().protocol == "PROJECT_V1"


@pytest.mark.parametrize("address", ["WHEELTEC", "2A:A2:19:07:1B", "2A:A2:19:07:1B:GG", "2A-A2:19:07:1B:8B",
                                     "00:00:00:00:00:00", "00-00-00-00-00-00", "000000000000", "", None, 2])
def test_invalid_address_is_rejected(address):
    with pytest.raises(ValueError, match="MAC"):
        BluetoothProfile(address=address)


@pytest.mark.parametrize("channel", [True, False, 1.0, "1", None, 0, -1, 31])
def test_channel_requires_actual_integer_in_transport_range(channel):
    with pytest.raises(ValueError, match="通道"):
        BluetoothProfile(channel=channel)


@pytest.mark.parametrize("auto_connect", [1, 0, "true", "false", None, [], {}])
def test_auto_connect_requires_actual_boolean(auto_connect):
    with pytest.raises(ValueError, match="布尔"):
        AppPreferences(auto_connect=auto_connect)


@pytest.mark.parametrize("updates", [
    {"alias": ""}, {"alias": " "}, {"alias": "a" * (MAX_ALIAS_LENGTH + 1)},
    {"alias": "车\n辆"}, {"alias": None},
    {"device_name": "a" * (MAX_DEVICE_NAME_LENGTH + 1)}, {"device_name": "车\x00辆"},
    {"device_name": 2}, {"protocol": "UNKNOWN"}, {"protocol": None},
])
def test_display_text_and_protocol_validation(updates):
    with pytest.raises(ValueError):
        BluetoothProfile(**updates)


def test_display_text_bounds_and_empty_device_name_are_allowed():
    profile = BluetoothProfile(alias="车" * MAX_ALIAS_LENGTH, device_name="")
    assert profile.device_name == ""
    assert BluetoothProfile(device_name="a" * MAX_DEVICE_NAME_LENGTH).device_name == "a" * MAX_DEVICE_NAME_LENGTH


@pytest.mark.parametrize("data", [b"{broken", b"", b"\xff", b"[]", b"null",
    b'{"schema_version":1,"auto_connect":false,"auto_connect":true,"profile":{}}',
    b'{"schema_version":NaN}', b"[" * 2000 + b"]" * 2000,
])
def test_corrupt_file_never_silently_auto_connects(tmp_path, data):
    path = tmp_path / "preferences.json"
    path.write_bytes(data)
    _assert_unsafe_load_suppressed(PreferencesStore(path).load())
    assert path.read_bytes() == data


@pytest.mark.parametrize("version", [0, 2, True, 1.0, "1", None])
def test_schema_mismatch_suppresses_startup_auto_connect(tmp_path, version):
    path = tmp_path / "preferences.json"
    _write_document(path, _document(schema_version=version))
    result = PreferencesStore(path).load()
    _assert_unsafe_load_suppressed(result)
    assert "版本" in result.warning


@pytest.mark.parametrize("update", [
    {"auto_connect": 1}, {"auto_connect": "false"}, {"auto_connect": None},
    {"profile": None}, {"profile": {}}, {"connected": True},
])
def test_invalid_saved_document_suppresses_startup_auto_connect(tmp_path, update):
    path = tmp_path / "preferences.json"
    _write_document(path, _document(**update))
    _assert_unsafe_load_suppressed(PreferencesStore(path).load())


@pytest.mark.parametrize("update", [{"address": "WHEELTEC"}, {"address": "00:00:00:00:00:00"},
                                     {"address": "00-00-00-00-00-00"}, {"address": "000000000000"},
                                     {"channel": True}, {"channel": 1.0},
                                     {"channel": 31}, {"protocol": "FUTURE"}, {"alias": ""},
                                     {"kp": 100}])
def test_invalid_saved_profile_suppresses_startup_auto_connect(tmp_path, update):
    path = tmp_path / "preferences.json"
    document = _document()
    document["profile"].update(update)
    _write_document(path, document)
    _assert_unsafe_load_suppressed(PreferencesStore(path).load())


def test_missing_required_field_suppresses_startup_auto_connect(tmp_path):
    path = tmp_path / "preferences.json"
    document = _document()
    del document["auto_connect"]
    _write_document(path, document)
    _assert_unsafe_load_suppressed(PreferencesStore(path).load())


def test_oversize_file_is_bounded_and_suppresses_startup_auto_connect(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_bytes(b" " * (MAX_PREFERENCES_BYTES + 1))
    result = PreferencesStore(path).load()
    _assert_unsafe_load_suppressed(result)
    assert "64 KiB" in result.warning


def test_read_failure_suppresses_startup_auto_connect(tmp_path):
    directory = tmp_path / "preferences.json"
    directory.mkdir()
    result = PreferencesStore(directory).load()
    _assert_unsafe_load_suppressed(result)
    assert "无法读取" in result.warning


def test_save_replace_failure_preserves_previous_file_and_surfaces_error(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    store = PreferencesStore(path)
    store.save(AppPreferences())
    previous = path.read_bytes()

    def fail_replace(source, target):
        assert source.parent == path.parent
        assert target == path
        assert source.read_bytes() != previous
        raise OSError("replacement denied")

    monkeypatch.setattr("car_host.preferences.os.replace", fail_replace)
    with pytest.raises(OSError, match="replacement denied"):
        store.save(AppPreferences(auto_connect=False))
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_save_directory_failure_surfaces_error(tmp_path):
    parent = tmp_path / "blocked"
    parent.write_text("existing file", encoding="utf-8")
    with pytest.raises(OSError):
        PreferencesStore(parent / "preferences.json").save(AppPreferences())
