"""IPC-only test peer; never imports or operates Qt Bluetooth."""

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from car_host.bluetooth_ipc import encode_message


def emit(kind, connection_id, **fields):
    sys.stdout.buffer.write(encode_message(kind, connection_id, **fields))
    sys.stdout.buffer.flush()


mode = sys.argv[1]
initial = json.loads(sys.stdin.buffer.readline())
session = initial["connection_id"]
if initial["kind"] == "stop":
    emit("closed", session)
    raise SystemExit
if mode == "hang_connect":
    time.sleep(60)
    raise SystemExit
if mode in {"scan", "scan_grace"}:
    count = 1 if mode == "scan_grace" else 140
    for number in range(count):
        address = f"AA:BB:CC:DD:EE:{number:02X}".encode()
        emit("device", session, payload=address, message=f"设备 {number}")
        emit("device", session, payload=address, message="重复设备")
    for line in sys.stdin.buffer:
        if json.loads(line)["kind"] == "stop":
            if mode == "scan_grace":
                emit("device", session, payload=b"AA:BB:CC:DD:EE:FF", message="停止时已发现设备")
            emit("closed", session)
            raise SystemExit
if mode == "stale":
    emit("opened", "old-session")
emit("opened", session)
if mode == "half_packet":
    sys.stdout.buffer.write(b'{"v":1,"kind":"rx"')
    sys.stdout.buffer.flush()
    raise SystemExit
if mode in {"flood", "stderr_flood"}:
    if mode == "flood":
        packet = encode_message("rx", session, payload=b"x" * 4096)
        for _ in range(2000):
            sys.stdout.buffer.write(packet)
        sys.stdout.buffer.flush()
    else:
        sys.stderr.buffer.write(b"diagnostic" * 100000)
        sys.stderr.buffer.flush()
    time.sleep(60)
emit("rx", session, payload=b"{C10:20:80}$")
for line in sys.stdin.buffer:
    command = json.loads(line)
    if command["kind"] == "stop":
        if mode == "hang_stop":
            time.sleep(60)
        emit("closed", session)
        emit("opened", session)  # Must not revive a closed session.
        raise SystemExit
    if command["kind"] == "send":
        if mode == "hang_send":
            time.sleep(60)
        payload = __import__("base64").b64decode(command["payload"])
        if mode == "stale":
            emit("tx", "old-session", request_id=command["request_id"], payload=payload)
        frame = encode_message("tx", session, request_id=command["request_id"], payload=payload)
        # Real OS pipes split a message across separate writes.
        sys.stdout.buffer.write(frame[:7])
        sys.stdout.buffer.flush()
        time.sleep(0.02)
        sys.stdout.buffer.write(frame[7:])
        sys.stdout.buffer.flush()
