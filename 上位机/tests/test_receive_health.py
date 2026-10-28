"""Host receive clocks and bounded evidence, using only local fake pipes."""

import csv
from dataclasses import replace
import json
import math
from pathlib import Path
import threading
import time

import pytest

from car_host.bluetooth_ipc import IpcError, encode_message
from car_host.bluetooth_worker import BluetoothWorker
from car_host.bluetooth_service import BluetoothService
from car_host.models import TelemetrySample
from car_host.receive_health import ReceiveHealth, rx_metadata, rx_trace
from car_host.recording import SessionRecorder
from car_host.transport import ConnectionConfig, IoEvent
from car_host.worker import TransportWorker


def sample(when=10.0, *, seq=1, tick=1000, session=42, connection="local-fake", rx=7):
    return TelemetrySample(when, "2026-10-04T00:00:00.000Z", "BLUETOOTH_SPP", "PROJECT_V1",
                           0.0, 0.0, 90, connection_id=connection, device_session=session,
                           device_time_ms=tick, frame_seq=seq, rx_dropped=rx, tx_dropped=0,
                           parent_read_monotonic=when + .02, worker_enqueued_monotonic=when + .03,
                           controller_started_monotonic=when + .05,
                           controller_completed_monotonic=when + .06)


def local_worker(qapp):
    # Construction neither starts a process nor imports a Bluetooth backend.
    worker = BluetoothWorker(ConnectionConfig(kind="bluetooth_spp", bluetooth_address="AA:BB:CC:DD:EE:FF"),
                             "local-fake")
    worker._opened = True
    return worker


class AvailablePipe:
    def __init__(self, data):
        self.data = bytearray(data)

    def setReadChannel(self, channel):
        pass

    def bytesAvailable(self):
        return len(self.data)

    def read(self, count):
        data = bytes(self.data[:count])
        del self.data[:count]
        return data


def test_pull_reads_already_available_ipc_without_a_readyread_callback(qapp, monkeypatch):
    worker = local_worker(qapp)
    line = encode_message("rx", "local-fake", payload=b"data", monotonic=100.0,
                          utc="2026-10-04T00:00:00.000Z", rx_block_id=23)
    worker._process = AvailablePipe(line)
    monkeypatch.setattr("car_host.bluetooth_worker.time.monotonic", lambda: 100.3)
    partial = worker.pull_available_input(max_bytes=20)
    assert partial == {"bytes_read": 20, "remaining_bytes": len(line) - 20,
                       "budget_exceeded": True, "reentrant": False}
    assert worker.drain_events() == []
    complete = worker.pull_available_input()
    assert not complete["budget_exceeded"] and complete["remaining_bytes"] == 0
    event, = worker.drain_events()
    assert event.payload == b"data" and event.rx_block_id == 23
    assert event.monotonic == 100.0
    assert event.parent_read_monotonic == event.worker_enqueued_monotonic == 100.3
    assert worker.event_queue_snapshot()["depth"] == 0


def test_drain_budget_preserves_global_control_data_order(qapp):
    worker = local_worker(qapp)
    worker._emit("rx", payload=b"1234", monotonic=2.0)
    worker._emit("error", message="must retain error", monotonic=2.1)
    worker._emit("rx", payload=b"56", monotonic=2.2)
    before = worker.event_queue_snapshot()
    assert before["depth"] == 3 and before["oldest_monotonic"] == 2.0
    assert [event.kind for event in worker.drain_events(max_count=2, max_bytes=3)] == []
    assert [event.kind for event in worker.drain_events(max_count=2, max_bytes=4)] == ["rx", "error"]
    assert [event.payload for event in worker.drain_events()] == [b"56"]


def test_parent_preserves_capture_and_assigns_unique_legacy_rx_blocks(qapp):
    worker = local_worker(qapp)
    for _ in range(2):
        worker._handle_message({"kind": "rx", "connection_id": "local-fake", "payload": "eA==",
                                "monotonic": 1.0}, parent_read_monotonic=2.0)
    events = worker.drain_events()
    assert [event.rx_block_id for event in events] == [1, 2]
    assert all(event.monotonic == 1.0 and event.parent_read_monotonic == 2.0 for event in events)


def test_service_capture_clock_is_after_socket_read_and_before_ipc_emission(qapp, monkeypatch):
    now = [100.0]
    delivered = []
    monkeypatch.setattr("car_host.bluetooth_service.time.monotonic", lambda: now[0])

    class Socket:
        data = [b"first", b"second"]

        def bytesAvailable(self):
            return sum(map(len, self.data))

        def read(self, count):
            now[0] += .005
            return self.data.pop(0)

    def emit(line):
        delivered.append(json.loads(line))
        now[0] += .025  # Deliberately slow serialization/pipe consumer.

    service = BluetoothService(emit)
    service._timer.stop()
    service.connection_id = "local-fake"
    service._socket = Socket()
    service._receive()
    assert [message["rx_block_id"] for message in delivered] == [1, 2]
    assert [message["monotonic"] for message in delivered] == pytest.approx([100.005, 100.035])


def test_transport_worker_keeps_capture_identity_and_persistent_overflow_count(qapp):
    worker = TransportWorker(ConnectionConfig(kind="simulation"))
    for index in range(worker.EVENT_QUEUE_CAPACITY + 2):
        worker._emit("rx", payload=b"x", monotonic=float(index), utc="capture")
    assert worker.event_queue_snapshot()["depth"] == worker.EVENT_QUEUE_CAPACITY
    assert worker.take_overflow_count() == 2
    assert worker.take_overflow_count() == 0
    assert worker.event_queue_snapshot()["overflow_total"] == 2
    event = worker.drain_events(max_count=1)[0]
    assert event.monotonic == 2.0 and event.rx_block_id == 3 and event.utc == "capture"
    assert worker.pull_available_input()["bytes_read"] == 0


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5, "1", 2**64])
def test_ipc_rx_block_ids_are_strict_bounded_integers(bad):
    with pytest.raises(IpcError):
        encode_message("rx", "local-fake", payload=b"x", rx_block_id=bad)
    with pytest.raises(IpcError):
        encode_message("tx", "local-fake", payload=b"x", rx_block_id=1)


def test_health_age_is_capture_based_and_query_does_not_refresh_it():
    health = ReceiveHealth()
    health.observe_sample(sample(), 10.06)
    healthy = health.snapshot(10.4)
    assert healthy["state_fresh"]
    assert healthy["latest_state_age_ms"] == pytest.approx(400)
    assert healthy["timings_ms"]["capture_to_parent_read"]["max"] == pytest.approx(20)
    assert healthy["timings_ms"]["worker_queue_wait"]["p50"] == pytest.approx(20)
    assert healthy["timings_ms"]["controller_processing"]["max"] == pytest.approx(10)
    assert not health.snapshot(10.5)["state_fresh"]
    assert health.snapshot(10.6)["latest_state_age_ms"] == pytest.approx(600)


def test_epoch_rebases_device_counters_and_keeps_counter_reset_visible():
    health = ReceiveHealth()
    health.observe_sample(sample(), 10.06)
    first = health.snapshot(10.1, counters={"parser": {"crc_errors": 3}})
    assert first["counter_baseline"]["device.rx_dropped"] == 7
    assert first["counter_delta"]["parser.crc_errors"] == 0
    health.observe_sample(sample(10.1, seq=2, tick=1050, rx=8), 10.16)
    second = health.snapshot(10.2, counters={"parser": {"crc_errors": 4}})
    assert second["counter_delta"]["device.rx_dropped"] == 1
    assert second["counter_delta"]["parser.crc_errors"] == 1
    reset = health.snapshot(10.2, counters={"parser": {"crc_errors": 0}})
    assert reset["counter_baseline"]["parser.crc_errors"] == 0
    assert reset["counter_delta"]["parser.crc_errors"] == 0
    assert reset["counter_resets"] == ["parser.crc_errors"]
    assert reset["counter_positive_increments"]["parser.crc_errors"] == 1
    assert reset["observer_positive_increments_total"]["device.rx_dropped"] == 1
    record = reset["counter_reset_history"][-1]
    assert record["previous_baseline"] == 3 and record["previous_current"] == 4
    assert record["new_baseline"] == 0 and record["reason"] == "value_decreased"
    health.observe_sample(sample(11.0, session=99, connection="new", rx=0), 11.06)
    final = health.snapshot(11.1)
    assert final["epoch"] == first["epoch"] + 1
    assert final["sample_count"] == 1 and final["counter_baseline"]["device.rx_dropped"] == 0
    assert final["counter_delta"]["device.rx_dropped"] == 0
    assert final["epoch_history"][-1]["counter_current"]["device.rx_dropped"] == 8
    assert final["observer_positive_increments_total"]["device.rx_dropped"] == 1


def test_counter_reset_and_epoch_events_can_be_drained_without_losing_evidence():
    health = ReceiveHealth()
    health.begin_epoch("first", 10, counters={"device": {"rx_dropped": 7}}, now=10)
    health.snapshot(11, counters={"device": {"rx_dropped": 10}})
    health.snapshot(12, counters={"device": {"rx_dropped": 2}})
    health.snapshot(13, counters={"device": {"rx_dropped": 4}})
    health.begin_epoch("second", 20, counters={"device": {"rx_dropped": 0}}, now=14)
    health.snapshot(15, counters={"device": {"rx_dropped": 1}})
    events = health.pop_baseline_events()
    assert [event["kind"] for event in events] == ["receive_epoch_started", "counter_baseline_reset",
                                                   "receive_epoch_ended", "receive_epoch_started"]
    reset = events[1]
    assert (reset["previous_baseline"], reset["previous_current"], reset["new_baseline"]) == (7, 10, 2)
    assert events[2]["counter_current"]["device.rx_dropped"] == 4
    assert events[2]["counter_positive_increments"]["device.rx_dropped"] == 5
    events[2]["counter_current"]["device.rx_dropped"] = 999
    assert health.pop_baseline_events() == []
    snapshot = health.snapshot(15)
    assert snapshot["epoch_history"][-1]["counter_current"]["device.rx_dropped"] == 4
    assert snapshot["counter_delta"]["device.rx_dropped"] == 1
    assert snapshot["observer_positive_increments_total"]["device.rx_dropped"] == 6
    assert "not MCU absolute totals" in snapshot["observer_increment_semantics"]


def test_baseline_evidence_is_bounded_and_typical_mcp_snapshot_fits_64kib():
    health = ReceiveHealth()
    counters = {f"counter_{index}_diagnostic": index for index in range(30)}
    for epoch in range(20):
        health.begin_epoch("x" * 32, epoch + 1, counters={"parser": counters}, now=epoch)
        health.snapshot(epoch + .1, counters={"parser": {key: value + 10 for key, value in counters.items()}})
    for index in range(200):
        # Each decrease must retain the previous count and reset its baseline.
        health.snapshot(21 + index * .01, counters={"device": {"rx_dropped": 2 if index % 2 else 0}})
    snapshot = health.snapshot(24)
    assert len(snapshot["epoch_history"]) == 8
    assert len(snapshot["counter_reset_history"]) == 8
    assert snapshot["baseline_events_pending"] == health.BASELINE_EVENT_CAPACITY
    assert snapshot["baseline_events_dropped"] > 0
    assert len(health.pop_baseline_events()) == health.BASELINE_EVENT_CAPACITY
    assert len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) < 65536
    assert snapshot["observer_positive_increments_total"]["device.rx_dropped"] == 200


def test_frame_tick_wrap_gaps_and_window_capacity_are_bounded():
    health = ReceiveHealth(window_seconds=1, capacity=3)
    for index, (seq, tick) in enumerate(((0xFFFFFFFF, 0xFFFFFFD0), (0, 2), (2, 102), (3, 152))):
        health.observe_sample(sample(10 + index * .1, seq=seq, tick=tick), 10.06 + index * .1)
    snapshot = health.snapshot(10.5)
    assert snapshot["sample_count"] == 3 and snapshot["window_truncated"]
    assert snapshot["frame_gap_count"] == snapshot["frame_missing_count"] == 1
    assert snapshot["frame_replay_or_backwards_count"] == 0
    assert snapshot["tick_gap_count"] == 1
    assert health.snapshot(11.5)["sample_count"] == 0
    assert not health.snapshot(11.5)["window_truncated"]


def test_missing_or_negative_timing_is_not_fabricated_as_zero():
    health = ReceiveHealth()
    unavailable = replace(sample(), parent_read_monotonic=None, worker_enqueued_monotonic=None,
                          controller_started_monotonic=9.9)
    health.observe_sample(unavailable, 10.1)
    snapshot = health.snapshot(10.2)
    timing = snapshot["timings_ms"]["capture_to_parent_read"]
    assert timing == {"count": 0, "missing": 1, "p50": None, "p95": None, "max": None}
    assert snapshot["clock_anomalies"] == 1


def test_trace_freezes_nested_content_and_profile_csv_preserves_extra_fields(tmp_path):
    recorder = SessionRecorder()
    event = IoEvent("rx", b"\x00\xff", monotonic=10, utc="2026-10-04T00:00:00.000Z",
                    connection_id="local-fake", rx_block_id=7, parent_read_monotonic=10.1,
                    worker_enqueued_monotonic=10.11)
    trace = rx_trace(event, 10.2, 10.21)
    trace["nested"] = {"seq": 1}
    enriched = replace(sample(), profile_seq=18, profile_start_tick=900, segment1_actual_tick=1300,
                       segment_index=1, profile_status=2, rx_first_block_id=6, rx_last_block_id=7,
                       rx_first_monotonic=9.99)
    directory = recorder.start(tmp_path)
    try:
        assert recorder.record_trace(trace)
        trace["nested"]["seq"] = 999
        assert recorder.record_sample(enriched)
    finally:
        assert recorder.stop()
    saved = json.loads((directory / "receive_trace.jsonl").read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1 and saved["nested"]["seq"] == 1
    assert saved["payload_hex"] == "00ff" and saved["captured_monotonic"] == 10
    assert recorder.trace_count == 1
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["frame_seq"] == "1" and row["profile_seq"] == "18"
    assert row["segment_index"] == "1" and row["rx_last_block_id"] == "7"
    assert rx_metadata(event) == {"rx_block_id": 7, "parent_read_monotonic": 10.1,
                                  "worker_enqueued_monotonic": 10.11}


def test_full_4096_byte_rx_trace_fits_bound_and_flushes_without_truncation(tmp_path):
    recorder = SessionRecorder()
    event = IoEvent("rx", bytes(range(256)) * 16, monotonic=10, utc="2026-10-04T00:00:00.000Z",
                    connection_id="x" * 128, rx_block_id=2**64 - 1)
    directory = recorder.start(tmp_path)
    try:
        assert recorder.record_trace(rx_trace(event, 10.1, 10.2))
    finally:
        assert recorder.stop()
    line = (directory / "receive_trace.jsonl").read_text(encoding="utf-8")
    assert len(line.encode("utf-8")) < 16384
    assert bytes.fromhex(json.loads(line)["payload_hex"]) == event.payload


def test_trace_overflow_shares_recording_integrity_and_keeps_accepted_item(tmp_path, monkeypatch):
    recorder = SessionRecorder(queue_capacity=1)
    directory = recorder.start(tmp_path)
    blocked, release = threading.Event(), threading.Event()
    original_writerow = csv.DictWriter.writerow

    def pause_writer(self, row):
        blocked.set()
        assert release.wait(2)
        return original_writerow(self, row)

    monkeypatch.setattr(csv.DictWriter, "writerow", pause_writer)
    try:
        assert recorder.record_sample(sample())
        assert blocked.wait(1)
        assert recorder.record_trace({"kind": "accepted"})
        assert not recorder.record_trace({"kind": "discarded"})
        assert recorder.dropped_total == 1
        assert recorder.take_notifications() == (None, 1)
    finally:
        release.set()
        assert recorder.stop()
    assert recorder.trace_count == 1
    trace = json.loads((directory / "receive_trace.jsonl").read_text(encoding="utf-8"))
    assert trace["kind"] == "accepted"
    assert "dropped_records=1" in (directory / "communication.log").read_text(encoding="utf-8")


def test_trace_write_failure_is_recording_failure(tmp_path, monkeypatch):
    original_open = Path.open

    class BrokenTrace:
        def __init__(self, stream):
            self.stream = stream

        def write(self, line):
            raise OSError("trace disk failure")

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def open_trace(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return BrokenTrace(stream) if path.name == "receive_trace.jsonl" else stream

    monkeypatch.setattr(Path, "open", open_trace)
    recorder = SessionRecorder()
    directory = recorder.start(tmp_path)
    try:
        assert recorder.record_trace({"kind": "rx_block"})
        deadline = time.monotonic() + 1
        while recorder.is_running and time.monotonic() < deadline:
            time.sleep(.005)
        assert not recorder.is_running and "trace disk failure" in recorder.failure_message
    finally:
        assert recorder.stop()
    assert "trace disk failure" in (directory / "communication.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("record", [{"age": math.nan}, {"age": math.inf}, {"schema_version": 2}, {"schema_version": True},
                                     {"payload": "x" * 20000}])
def test_bad_trace_cannot_poison_recording_thread(record):
    with pytest.raises(ValueError):
        SessionRecorder().record_trace(record)
