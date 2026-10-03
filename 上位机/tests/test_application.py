"""Controller integration tests with Qt events and hardware-free transports."""

from collections import deque
import csv
import os
from pathlib import Path
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from car_host.controller import HostController, encode_manual_payload
from car_host.transport import ConnectionConfig, IoEvent


def event(kind: str, payload: bytes = b"", *, when: float | None = None,
          message: str = "") -> IoEvent:
    return IoEvent(kind, payload, message, time.monotonic() if when is None else when,
                   "2026-10-03T06:00:00.000Z")


class FakeWorker:
    """Imitate the worker's bounded delivery and lifecycle without a QThread."""

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.events: deque[IoEvent] = deque()
        self.started = False
        self.stopping = False
        self.finished = False
        self.send_allowed = True
        self.wait_result = True
        self.sent: list[bytes] = []
        self.overflows = 0
        self.wait_calls: list[int] = []
        from uuid import uuid4
        self.connection_id = uuid4().hex

    def start(self) -> None:
        self.started = True
        self.events.append(event("opened"))

    def request_send(self, payload: bytes, request_id: str = "") -> bool:
        if not self.started or self.stopping or not self.send_allowed:
            return False
        self.sent.append(payload)
        from dataclasses import replace
        self.events.append(replace(event("tx", payload), request_id=request_id,
                                   connection_id=self.connection_id))
        return True

    def request_stop(self) -> None:
        self.stopping = True
        if self.wait_result and not self.finished:
            self.finished = True
            self.events.append(event("closed"))

    def drain_events(self, max_count: int = 256) -> list[IoEvent]:
        return [self.events.popleft() for _ in range(min(max(0, max_count), len(self.events)))]

    def take_overflow_count(self) -> int:
        result, self.overflows = self.overflows, 0
        return result

    def isFinished(self) -> bool:
        return self.finished

    def wait(self, milliseconds: int) -> bool:
        self.wait_calls.append(milliseconds)
        if self.wait_result:
            self.finished = True
        return self.wait_result


@pytest.fixture(scope="session")
def application_qt():
    # A QApplication is also a QCoreApplication and can be shared by UI tests.
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def fake_controller(application_qt, tmp_path):
    workers: list[FakeWorker] = []

    def factory(config: ConnectionConfig) -> FakeWorker:
        worker = FakeWorker(config)
        workers.append(worker)
        return worker

    controller = HostController(tmp_path, worker_factory=factory)
    controller.timer.stop()
    yield controller, workers
    for worker in workers:
        worker.wait_result = True
    spin_until(application_qt, controller.shutdown)


def connect_fake(controller: HostController) -> None:
    assert controller.connect_device(ConnectionConfig(kind="simulation"))
    controller.poll()
    assert controller.connected


def spin_until(application, condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if condition():
            return
        time.sleep(0.005)
    application.processEvents()
    assert condition(), "Qt event loop did not reach the expected state"


def read_rows(directory: Path) -> list[dict[str, str]]:
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_real_simulation_event_loop_send_record_and_reconnect(application_qt, tmp_path):
    controller = HostController(tmp_path)
    try:
        for _ in range(3):
            assert controller.connect_device(ConnectionConfig(kind="simulation"))
            spin_until(application_qt, lambda: controller.connected and controller.frames_received >= 2)
            assert controller.latest.source == "SIMULATOR"
            assert controller.latest.protocol == "LEGACY_APP"
            assert controller.telemetry_status == "接收正常"
            assert controller.receive_frequency > 0
            assert controller.send_manual("7B 43 31 3A 32 3A 39 30 7D 24", True)
            spin_until(application_qt, lambda: controller.bytes_sent == 10)
            assert any(entry.direction == "TX" and entry.source == "SIMULATOR" for entry in controller.logs)
            assert controller.start_recording()
            directory = controller.recorder.directory
            spin_until(application_qt, lambda: controller.recorder.sample_count >= 1)
            controller.stop_recording()
            spin_until(application_qt, lambda: not controller.recorder.is_running)
            rows = read_rows(directory)
            assert rows and {row["source"] for row in rows} == {"SIMULATOR"}
            assert all(row["signed_left_speed_mps"] == "" for row in rows)
            controller.disconnect_device()
            spin_until(application_qt, lambda: controller.worker is None)
            assert not controller.connected
            assert controller.connection_status == "未连接"
        assert len(list(tmp_path.glob("*/telemetry.csv"))) == 3
    finally:
        spin_until(application_qt, controller.shutdown)
    assert not controller.timer.isActive()
    assert not controller.recorder.is_running


def test_bad_raw_receive_does_not_refresh_last_valid_measurement(fake_controller):
    controller, workers = fake_controller
    connect_fake(controller)
    valid_time = time.monotonic() - 2.0
    workers[-1].events.append(event("rx", b"{C15:8:96}$", when=valid_time))
    controller.poll()
    previous = controller.latest
    assert controller.telemetry_status == "数据过期"
    workers[-1].events.append(event("rx", b"garbage{C-1:2:50}$"))
    controller.poll()
    assert controller.latest is previous
    assert controller.latest.received_monotonic == valid_time
    assert controller.frames_received == 1
    assert controller.bytes_received == len(b"{C15:8:96}$garbage{C-1:2:50}$")
    assert controller.telemetry_status == "数据过期"
    assert controller.receive_frequency == 0
    assert any(entry.direction == "RX" and entry.payload == b"garbage{C-1:2:50}$"
               for entry in controller.logs)


def test_connection_guards_and_new_session_clear_measurements(fake_controller):
    controller, workers = fake_controller
    assert not controller.connect_device(ConnectionConfig(port=" "))
    assert workers == []
    connect_fake(controller)
    assert not controller.connect_device(ConnectionConfig(kind="simulation"))
    workers[-1].events.append(event("rx", b"{C10:20:90}$"))
    controller.poll()
    assert controller.frames_received == 1
    assert len(controller.history.samples) == 1
    controller.disconnect_device()
    controller.poll()
    assert controller.worker is None
    assert controller.telemetry_status == "连接已断开"
    assert controller.connect_device(ConnectionConfig(kind="simulation"))
    assert controller.latest is None
    assert controller.frames_received == controller.bytes_received == controller.bytes_sent == 0
    assert len(controller.history.samples) == 0
    assert list(controller.frame_times) == []
    controller.poll()
    assert controller.telemetry_status == "等待有效遥测"


def test_finished_worker_retains_all_events_beyond_one_poll_batch(fake_controller):
    controller, workers = fake_controller
    assert controller.connect_device(ConnectionConfig(kind="simulation"))
    worker = workers[-1]
    worker.events.extend(event("rx", b"{C1:2:90}$") for _ in range(300))
    worker.events.append(event("closed", message="last close event"))
    worker.finished = True
    for _ in range(4):
        controller.poll()
    assert controller.frames_received == 300
    assert controller.bytes_received == len(b"{C1:2:90}$") * 300
    assert controller.worker is None
    assert not controller.connected
    assert any(entry.message == "last close event" for entry in controller.logs)


def test_manual_send_validation_disconnected_and_queue_rejection(fake_controller):
    controller, workers = fake_controller
    assert not controller.send_manual("FF", True)
    connect_fake(controller)
    assert not controller.send_manual("0xFF", True)
    assert workers[-1].sent == []
    assert controller.send_manual("00 ff\n7B", True)
    controller.poll()
    assert workers[-1].sent == [b"\x00\xff{"]
    assert controller.bytes_sent == 3
    workers[-1].send_allowed = False
    assert not controller.send_manual("text", False, "\r\n")
    assert any(entry.level == "ERROR" and "队列已满" in entry.message for entry in controller.logs)


def test_event_overflow_keeps_new_candidate_after_gap_and_is_visible(fake_controller):
    controller, workers = fake_controller
    connect_fake(controller)
    worker = workers[-1]
    worker.events.append(event("rx", b"{old partial"))
    controller.poll()
    worker.events.append(event("rx", b"{C1:"))
    worker.overflows = 5
    controller.poll()
    assert controller.latest is None
    worker.events.append(event("rx", b"2:50}$"))
    controller.poll()
    assert controller.latest.raw_frame == b"{C1:2:50}$"
    assert controller.frames_received == 1
    assert any(entry.level == "ERROR" and "丢失 5 个事件" in entry.message for entry in controller.logs)
    worker.events.append(event("rx", b"{C1:2:50}$"))
    controller.poll()
    assert controller.frames_received == 2


def test_recording_create_failure_is_visible_to_controller(fake_controller, tmp_path):
    controller, _ = fake_controller
    occupied = tmp_path / "occupied"
    occupied.write_text("file, not a directory", encoding="utf-8")
    controller.data_directory = occupied
    connect_fake(controller)
    assert not controller.start_recording()
    assert controller.recording_status == "录制启动失败"
    assert not controller.recorder.is_running
    assert any(entry.level == "ERROR" and "录制启动失败" in entry.message for entry in controller.logs)


def test_background_recording_failure_is_visible(fake_controller, monkeypatch):
    controller, workers = fake_controller
    connect_fake(controller)
    assert controller.start_recording()

    def disk_full(self, row):
        raise OSError("disk full during telemetry write")

    monkeypatch.setattr(csv.DictWriter, "writerow", disk_full)
    workers[-1].events.append(event("rx", b"{C1:2:50}$"))
    controller.poll()
    deadline = time.monotonic() + 1.0
    while controller.recorder.is_running and time.monotonic() < deadline:
        time.sleep(0.005)
    controller.poll()
    assert controller.recording_status == "录制失败"
    assert any(entry.level == "ERROR" and "disk full" in entry.message for entry in controller.logs)


def test_shutdown_closes_worker_and_finishes_recording(fake_controller, application_qt):
    controller, workers = fake_controller
    connect_fake(controller)
    assert controller.start_recording()
    workers[-1].events.append(event("rx", b"{C1:2:-7}$"))
    controller.poll()
    directory = controller.recorder.directory
    spin_until(application_qt, controller.shutdown)
    assert workers[-1].stopping
    assert workers[-1].wait_calls == []
    assert controller.worker is None
    assert not controller.timer.isActive()
    assert not controller.recorder.is_running
    assert read_rows(directory)[0]["battery_percent_raw"] == "-7"


def test_shutdown_never_waits_for_worker_and_retries_without_blocking(fake_controller):
    controller, workers = fake_controller
    connect_fake(controller)
    worker = workers[-1]
    worker.wait_result = False
    assert not controller.shutdown()
    assert controller.worker is worker
    assert worker.wait_calls == []
    assert controller._shutting_down
    worker.wait_result = True
    worker.request_stop()
    assert controller.shutdown()


@pytest.mark.parametrize("text", ["", " ", "A", "GG", "0xFF", "FF-00", "00:11"])
def test_hex_rejects_invalid_or_incomplete_bytes(text):
    with pytest.raises(ValueError):
        encode_manual_payload(text, True)


def test_manual_payload_utf8_line_endings_hex_and_size_limit():
    assert encode_manual_payload("小车", False, "\r\n") == "小车\r\n".encode("utf-8")
    assert encode_manual_payload("00\tFF\n7b", True, "\r\n") == b"\x00\xff{"
    assert len(encode_manual_payload("FF" * 4096, True)) == 4096
    assert len(encode_manual_payload("a" * 4096, False)) == 4096
    for text, as_hex in (("", False), ("a" * 4097, False), ("FF" * 4097, True), ("车" * 1366, False)):
        with pytest.raises(ValueError):
            encode_manual_payload(text, as_hex)


def test_worker_finishing_during_drain_keeps_newly_enqueued_tail(fake_controller):
    controller, workers = fake_controller

    class FinishDuringDrainWorker(FakeWorker):
        def drain_events(self, max_count: int = 256) -> list[IoEvent]:
            batch = super().drain_events(max_count)
            if not self.finished:
                self.events.append(event("rx", b"{C12:34:90}$"))
                self.events.append(event("closed", message="closed during drain"))
                self.finished = True
            return batch

    def factory(config):
        worker = FinishDuringDrainWorker(config)
        workers.append(worker)
        return worker

    controller.worker_factory = factory
    assert controller.connect_device(ConnectionConfig(kind="simulation"))
    for _ in range(3):
        controller.poll()
    assert controller.frames_received == 1
    assert controller.latest.raw_frame == b"{C12:34:90}$"
    assert any(entry.message == "closed during drain" for entry in controller.logs)
    assert controller.worker is None


def test_overflow_resets_partial_candidate_before_feeding_new_tail(fake_controller):
    controller, workers = fake_controller
    connect_fake(controller)
    worker = workers[-1]
    worker.events.append(event("rx", b"{C1:2:"))
    controller.poll()
    assert controller.adapter.diagnostics["buffered_bytes"] > 0
    worker.overflows = 1
    worker.events.append(event("rx", b"90}$"))
    controller.poll()
    assert controller.latest is None
    assert controller.frames_received == 0
    worker.events.append(event("rx", b"{C1:2:90}$"))
    controller.poll()
    assert controller.frames_received == 1


def test_recording_drops_remain_incomplete_after_notification_consumed_and_stop(
    fake_controller, monkeypatch, application_qt
):
    import threading

    from car_host.recording import SessionRecorder

    controller, _ = fake_controller
    controller.recorder = SessionRecorder(queue_capacity=4)
    connect_fake(controller)
    assert controller.start_recording()
    deadline = time.monotonic() + 1.0
    while controller.recorder.log_count < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller.recorder.log_count >= 1

    writing, release = threading.Event(), threading.Event()
    original_writerow = csv.DictWriter.writerow

    def blocked_writerow(self, row):
        writing.set()
        assert release.wait(2.0), "test did not release the recorder writer"
        return original_writerow(self, row)

    monkeypatch.setattr(csv.DictWriter, "writerow", blocked_writerow)
    measurement = controller.adapter.feed(b"{C1:2:90}$")[0]
    try:
        assert controller.recorder.record_sample(measurement)
        assert writing.wait(1.0)
        for _ in range(4):
            assert controller.recorder.record_sample(measurement)
        assert not controller.recorder.record_sample(measurement)
        controller.poll()
        assert controller.recording_incomplete
        assert controller.recorder.dropped_total >= 1
        assert controller.recorder.take_notifications() == (None, 0)
    finally:
        release.set()
    directory = controller.recorder.directory
    controller.stop_recording()
    spin_until(application_qt, lambda: not controller.recorder.is_running)
    controller.poll()
    assert controller.recording_status == "录制不完整"
    assert controller.recording_incomplete
    footer = (directory / "communication.log").read_text(encoding="utf-8")
    assert "INTEGRITY" in footer
    assert f"dropped_records={controller.recorder.dropped_total}" in footer
    assert controller.start_recording()
    assert not controller.recording_incomplete
    assert controller.recorder.dropped_total == 0
    assert controller.recorder.failure_message is None
    deadline = time.monotonic() + 1.0
    while controller.recorder.log_count < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller.recorder.log_count >= 1
    controller.stop_recording()
    spin_until(application_qt, lambda: not controller.recorder.is_running)
    controller.poll()
    assert controller.recording_status == "录制已保存"


@pytest.mark.parametrize("dropped", [0, 2])
def test_timed_out_recording_stop_updates_when_writer_naturally_finishes(
    fake_controller, dropped
):
    controller, _ = fake_controller

    class DeferredRecorder:
        def __init__(self):
            self.active = False
            self.running = False
            self.dropped_total = dropped
            self.failure_message = None
            self.notifications = 0
            self.directory = None

        @property
        def is_active(self):
            return self.active and self.running

        @property
        def is_running(self):
            return self.running

        def start(self, directory):
            self.directory = directory / "deferred"
            self.active = self.running = True
            self.notifications = dropped
            return self.directory

        def record_log(self, entry):
            return self.is_active

        def record_sample(self, sample):
            return self.is_active

        def take_notifications(self):
            result, self.notifications = (None, self.notifications), 0
            return result

        def stop(self, timeout=0):
            self.active = False
            return not self.running

    recorder = DeferredRecorder()
    controller.recorder = recorder
    connect_fake(controller)
    assert controller.start_recording()
    controller.poll()
    assert not controller.stop_recording()
    assert controller.recording_status == "文件仍在保存"
    controller.poll()
    assert controller.recording_status == "文件仍在保存"
    recorder.running = False
    controller.poll()
    assert controller.recording_status == ("录制不完整" if dropped else "录制已保存")


def test_shutdown_drains_full_worker_event_capacity_before_stopping_recorder(fake_controller, application_qt):
    from car_host.recording import SessionRecorder

    controller, workers = fake_controller
    controller.recorder = SessionRecorder(queue_capacity=4096)
    connect_fake(controller)
    assert controller.start_recording()
    worker = workers[-1]
    worker.events.extend(event("rx", b"{C1:2:90}$") for _ in range(1023))
    directory = controller.recorder.directory
    spin_until(application_qt, controller.shutdown)
    assert controller.worker is None
    assert controller.frames_received == 1023
    assert not controller.recorder.is_running
    assert len(read_rows(directory)) == 1023
    assert worker.events == deque()
