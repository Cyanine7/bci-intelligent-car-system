from pathlib import Path
import sys
import time

from PySide6.QtCore import QObject, Signal
import pytest

from car_host.bluetooth_service import SendPump
from car_host.bluetooth_ipc import encode_message
from car_host.bluetooth_worker import BluetoothScanner, BluetoothWorker
from car_host.transport import ConnectionConfig


def spin(qapp, condition, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


@pytest.fixture
def workers(qapp):
    active = []

    def create(mode="echo", scan=False):
        command = [sys.executable, str(Path(__file__).parent / "bluetooth_fixtures" / "fake_process.py"), mode]
        if scan:
            worker = BluetoothScanner("scan-session", process_command=command)
        else:
            worker = BluetoothWorker(ConnectionConfig(kind="bluetooth_spp", bluetooth_address="AA:BB:CC:DD:EE:FF"), "session", process_command=command)
        worker.CONNECT_TIMEOUT = 1.5
        worker.SCAN_TIMEOUT = 0.4
        worker.SEND_TIMEOUT = 0.25
        worker.STOP_TIMEOUT = 0.25
        active.append(worker)
        return worker

    yield create
    for worker in active:
        worker.request_stop()
    assert spin(qapp, lambda: all(worker.isFinished() for worker in active), 3)


def opened(qapp, worker):
    worker.start()
    assert spin(qapp, lambda: worker._opened or worker.isFinished())
    assert worker._opened, [(event.kind, event.message) for event in collect(worker)]


def collect(worker):
    events = []
    while batch := worker.drain_events():
        events.extend(batch)
    return events


def test_real_pipe_split_send_and_receive(qapp, workers):
    worker = workers()
    assert not worker.request_send(b"before connection")
    opened(qapp, worker)
    assert worker.request_send(b"test\x00bytes", "manual-1")
    assert not worker.request_send(b"duplicate", "manual-1")
    assert spin(qapp, lambda: not worker._queue and worker._inflight is None)
    worker.request_stop()
    assert not worker.request_send(b"after stop")
    assert spin(qapp, worker.isFinished)
    events = collect(worker)
    assert [event.payload for event in events if event.kind == "tx"] == [b"test\x00bytes"]
    assert [event.request_id for event in events if event.kind == "tx"] == ["manual-1"]
    assert any(event.payload == b"{C10:20:80}$" for event in events)
    assert all(event.connection_id == "session" for event in events)
    assert len([event for event in events if event.kind == "closed"]) == 1
    assert not any(event.kind == "error" for event in events)


def test_connect_hang_has_parent_deadline(qapp, workers):
    worker = workers("hang_connect")
    worker.CONNECT_TIMEOUT = 0.25
    worker.start()
    assert spin(qapp, worker.isFinished, 2)
    assert any("连接超时" in event.message for event in collect(worker))


def test_send_hang_is_killed_and_not_replayed(qapp, workers):
    worker = workers("hang_send")
    opened(qapp, worker)
    assert worker.request_send(b"a", "r1")
    assert worker.request_send(b"b", "r2")
    assert spin(qapp, worker.isFinished, 2)
    events = collect(worker)
    assert any(event.kind == "error" and "结果未知" in event.message and event.request_id == "r1" for event in events)
    assert not any(event.kind == "tx" for event in events)
    assert len(worker._queue) == 0


def test_stop_hang_never_waits_on_ui(qapp, workers):
    worker = workers("hang_stop")
    opened(qapp, worker)
    started = time.monotonic()
    worker.request_stop()
    assert time.monotonic() - started < 0.1
    assert not worker.wait(2000)
    assert spin(qapp, worker.isFinished, 2)
    assert worker.wait()
    assert any("停止超时" in event.message for event in collect(worker))


@pytest.mark.parametrize("mode", ["half_packet", "flood", "stderr_flood"])
def test_bad_process_output_is_bounded_and_terminal_preserved(qapp, workers, mode):
    worker = workers(mode)
    worker.start()
    assert spin(qapp, worker.isFinished, 3)
    events = collect(worker)
    assert len(events) <= worker.EVENT_QUEUE_CAPACITY
    assert any(event.kind == "error" for event in events)
    assert events[-1].kind == "closed"
    assert len(worker._stderr_tail) <= 2048
    assert worker._decoder.buffered_bytes <= 16384


def test_late_wrong_session_and_open_after_close_are_ignored(qapp, workers):
    worker = workers("stale")
    opened(qapp, worker)
    assert worker.request_send(b"test", "r")
    assert spin(qapp, lambda: not worker._queue and worker._inflight is None)
    worker.request_stop()
    assert spin(qapp, worker.isFinished)
    events = collect(worker)
    assert len([event for event in events if event.kind == "opened"]) == 1
    assert len([event for event in events if event.kind == "tx"]) == 1
    assert not worker._opened


def test_queue_and_lifecycle_capacity(qapp, workers):
    worker = workers()
    opened(qapp, worker)
    for number in range(worker.SEND_QUEUE_CAPACITY):
        assert worker.request_send(b"x", str(number))
    assert not worker.request_send(b"overflow", "extra")
    worker._emit("error", message="critical")
    for _ in range(3000):
        worker._emit("rx", payload=b"x")
        worker._emit("status", message="noise")
    assert len(worker._data) + len(worker._control) <= worker.EVENT_QUEUE_CAPACITY
    assert worker.take_overflow_count() > 0
    assert worker.take_overflow_count() == 0
    assert any(event.kind == "error" and event.message == "critical" for event in collect(worker))


def test_scanner_deduplicates_and_caps_devices(qapp, workers):
    scanner = workers("scan", scan=True)
    scanner.SCAN_TIMEOUT = 0.8
    scanner.start()
    assert spin(qapp, scanner.isFinished, 3)
    events = collect(scanner)
    devices = [event for event in events if event.kind == "device"]
    assert len(devices) == 128
    assert len({event.payload for event in devices}) == 128
    assert scanner.take_overflow_count() > 0
    assert not any(event.kind == "opened" for event in events)


def test_scanner_accepts_inflight_results_during_stop_grace(qapp, workers):
    scanner = workers("scan_grace", scan=True)
    scanner.start()
    assert spin(qapp, scanner.isFinished, 3)
    devices = [event for event in collect(scanner) if event.kind == "device"]
    assert [event.payload for event in devices] == [b"AA:BB:CC:DD:EE:00", b"AA:BB:CC:DD:EE:FF"]


def test_stop_before_start_never_launches_process(qapp, workers):
    worker = workers()
    worker.request_stop()
    worker.start()
    assert worker.isFinished()
    assert not worker._started
    assert [event.kind for event in collect(worker)] == ["closed"]


def test_failed_child_start_finishes(qapp):
    worker = BluetoothWorker(ConnectionConfig(kind="bluetooth_spp", bluetooth_address="AA:BB:CC:DD:EE:FF"), process_command=["missing-bluetooth-test-executable"])
    worker.start()
    assert spin(qapp, worker.isFinished)
    assert [event.kind for event in collect(worker)] == ["error", "closed"]


def test_real_backend_starts_and_stops_without_operating_bluetooth(qapp):
    class StopOnlyWorker(BluetoothWorker):
        def _initial_message(self):
            # Exercise the shipped module/reader/Qt event loop, with no scan or connect.
            return encode_message("stop", self.connection_id)

    worker = StopOnlyWorker(ConnectionConfig(kind="bluetooth_spp", bluetooth_address="AA:BB:CC:DD:EE:FF"))
    try:
        worker.start()
        assert spin(qapp, worker.isFinished, 6)
        assert worker._process.exitCode() == 0, worker._stderr_tail.decode("utf-8", "replace")
        events = collect(worker)
        assert [event.kind for event in events] == ["closed"]
    finally:
        worker.request_stop()
        assert spin(qapp, worker.isFinished, 3)


def test_parent_preserves_capture_time_and_critical_results_despite_rx_status_flood(qapp, workers):
    worker = workers()
    opened(qapp, worker)
    captured = time.monotonic() - 2
    worker._handle_message({"connection_id": "session", "kind": "rx", "payload": "eA==",
                            "monotonic": captured, "utc": "2026-10-03T01:00:00.000Z"})
    measurement = next(event for event in collect(worker) if event.payload == b"x")
    assert measurement.monotonic == captured
    assert measurement.utc == "2026-10-03T01:00:00.000Z"
    worker._emit("opened", message="protected-opened")
    worker._emit("tx", payload=b"success", request_id="protected-tx")
    worker._emit("error", message="protected-error")
    for _ in range(2000):
        worker._emit("rx", payload=b"noise")
        worker._emit("status", message="noise")
    worker.request_stop()
    assert spin(qapp, worker.isFinished, 3)
    events = collect(worker)
    assert any(event.message == "protected-opened" for event in events)
    assert any(event.request_id == "protected-tx" for event in events)
    assert any(event.message == "protected-error" for event in events)
    assert sum(event.kind == "closed" for event in events) == 1
    assert len(worker._control) + len(worker._data) <= 1024


class FakeSocket(QObject):
    bytesWritten = Signal("qint64")

    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.writes = []
        self.inside = False

    def write(self, payload):
        assert not self.inside, "recursive write from bytesWritten"
        self.inside = True
        try:
            self.writes.append(payload)
            if self.mode == "negative":
                self.bytesWritten.emit(-1)
                return -1
            count = min(2, len(payload)) if self.mode == "partial" else len(payload)
            self.bytesWritten.emit(count)
            return count
        finally:
            self.inside = False


@pytest.mark.parametrize("mode", ["complete", "partial", "negative"])
def test_send_pump_synchronous_signals_never_recurse(qapp, mode):
    socket = FakeSocket(mode)
    completed, failed = [], []
    pump = SendPump(socket, lambda payload, request_id: completed.append((payload, request_id)), lambda message, request_id: failed.append((message, request_id)))
    assert pump.send(b"abcdef", "r")
    assert not pump.send(b"busy", "other")
    assert spin(qapp, lambda: bool(completed or failed))
    if mode == "negative":
        assert failed[0][1] == "r"
        assert not completed
    else:
        assert completed == [(b"abcdef", "r")]
        assert not failed
        assert socket.writes == ([b"abcdef", b"cdef", b"ef"] if mode == "partial" else [b"abcdef"])
