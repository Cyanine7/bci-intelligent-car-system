import json

import pytest

from car_host.bluetooth_ipc import (
    IpcError, MAX_LINE_BYTES, MAX_PAYLOAD_BYTES, MAX_PIPE_BYTES, MessageDecoder,
    decode_payload, encode_message, normalize_address, validate_channel,
)


def test_mac_normalization():
    assert normalize_address(" aa-bb-cc-dd-ee-ff ") == "AA:BB:CC:DD:EE:FF"


@pytest.mark.parametrize("address", ["", "localhost", "AA:BB:CC:DD:EE", "00:00:00:00:00:00", 123, "GG:00:00:00:00:01"])
def test_mac_rejects_invalid(address):
    with pytest.raises(ValueError):
        normalize_address(address)


@pytest.mark.parametrize("channel", [0, 31, "1", True, None, 1.5])
def test_channel_rejects_invalid(channel):
    with pytest.raises(ValueError):
        validate_channel(channel)


def test_channel_boundaries():
    assert validate_channel(1) == 1
    assert validate_channel(30) == 30


def test_bytewise_split_and_concatenated_messages():
    payload = bytes(range(256))
    frame = encode_message("rx", "session", payload=payload, message="中文")
    decoder = MessageDecoder(commands=False)
    results = []
    for byte in frame:
        results.extend(decoder.feed(bytes([byte])))
    assert len(results) == 1
    assert decode_payload(results[0]) == payload
    assert results[0]["message"] == "中文"
    assert len(decoder.feed(frame + frame)) == 2
    assert decoder.buffered_bytes == 0
    decoder.finish()


@pytest.mark.parametrize("line", [
    b"not json\n", b"\n", b"[]\n", b'{"v":1,"kind":[],"connection_id":"s"}\n',
    b'{"v":true,"kind":"rx","connection_id":"s"}\n',
    b'{"v":1,"kind":"rx","connection_id":"s","payload":"!"}\n',
    b'{"v":1,"kind":"rx","connection_id":"s","monotonic":NaN}\n',
    b'{"v":1,"kind":"rx","connection_id":"s","request_id":[]}\n',
])
def test_invalid_messages_raise_typed_error(line):
    with pytest.raises(IpcError):
        MessageDecoder().feed(line)


def test_oversized_and_unfinished_input_is_bounded():
    decoder = MessageDecoder()
    with pytest.raises(IpcError):
        decoder.feed(b"x" * MAX_LINE_BYTES)
    assert decoder.buffered_bytes == 0
    with pytest.raises(IpcError):
        decoder.feed(b"x" * (MAX_PIPE_BYTES + 1))
    decoder.feed(b"{")
    with pytest.raises(IpcError, match="半包"):
        decoder.finish()
    assert decoder.buffered_bytes == 0
    with pytest.raises(IpcError):
        encode_message("send", "s", payload=b"x" * (MAX_PAYLOAD_BYTES + 1))


def test_command_and_event_boundary():
    with pytest.raises(IpcError):
        MessageDecoder(commands=True).feed(encode_message("opened", "s"))
    with pytest.raises(IpcError):
        MessageDecoder(commands=False).feed(encode_message("connect", "s"))
