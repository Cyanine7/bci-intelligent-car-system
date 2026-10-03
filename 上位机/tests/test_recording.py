"""Persistent telemetry, communication logs, and bounded chart history."""

import csv
from dataclasses import replace
import json

import pytest

from car_host.history import TelemetryHistory
from car_host.models import TelemetrySample
from car_host.recording import CSV_FIELDS, LogEntry, SessionRecorder


def sample(when: float = 100.0, battery: int = 96) -> TelemetrySample:
    return TelemetrySample(
        received_monotonic=when,
        received_utc="2026-10-03T06:00:00.000Z",
        source="SIMULATOR",
        protocol="LEGACY_APP",
        left_speed_abs_mps=0.15,
        right_speed_abs_mps=0.08,
        battery_percent_raw=battery,
        quality=() if 0 <= battery <= 100 else ("battery_out_of_range",),
        raw_frame=f"{{C15:8:{battery}}}$".encode("ascii"),
    )


def read_csv(directory):
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


def test_csv_preserves_source_units_utc_raw_bytes_and_unavailable_fields(tmp_path):
    recorder = SessionRecorder()
    try:
        directory = recorder.start(tmp_path)
        assert recorder.record_sample(sample())
        assert recorder.record_sample(replace(sample(100.5, -7), source="SERIAL"))
        assert recorder.record_sample(sample(101.0, 125))
    finally:
        assert recorder.stop()
    fieldnames, rows = read_csv(directory)
    assert tuple(fieldnames) == CSV_FIELDS
    assert len(rows) == recorder.sample_count == 3
    first = rows[0]
    assert first["received_utc"] == "2026-10-03T06:00:00.000Z"
    assert first["received_monotonic"] == "100.0"
    assert first["source"] == "SIMULATOR"
    assert first["protocol"] == "LEGACY_APP"
    assert first["left_speed_abs_mps"] == "0.15"
    assert first["right_speed_abs_mps"] == "0.08"
    assert first["battery_percent_raw"] == "96"
    assert first["raw_frame_hex"] == sample().raw_frame.hex(" ").upper()
    assert first["quality"] == ""
    for name in ("device_time_ms", "signed_left_speed_mps", "signed_right_speed_mps",
                 "pwm_left", "pwm_right", "encoder_left", "encoder_right",
                 "imu_accel", "imu_gyro", "attitude"):
        assert first[name] == ""
    assert rows[1]["source"] == "SERIAL"
    assert rows[1]["battery_percent_raw"] == "-7"
    assert rows[2]["battery_percent_raw"] == "125"
    assert rows[1]["quality"] == rows[2]["quality"] == "battery_out_of_range"


def test_csv_future_fields_are_preserved_without_affecting_legacy_fields(tmp_path):
    recorder = SessionRecorder()
    enriched = replace(sample(), device_time_ms=1500, signed_left_speed_mps=-0.15,
                       signed_right_speed_mps=0.08, pwm_left=-5000, pwm_right=5000,
                       encoder_left=-123, encoder_right=456, imu_accel=(0.0, 0.0, 9.81),
                       imu_gyro=(0.1, 0.2, 0.3), attitude=(1.0, 2.0, 3.0))
    try:
        directory = recorder.start(tmp_path)
        assert recorder.record_sample(enriched)
    finally:
        assert recorder.stop()
    _, rows = read_csv(directory)
    assert rows[0]["device_time_ms"] == "1500"
    assert rows[0]["signed_left_speed_mps"] == "-0.15"
    assert rows[0]["signed_right_speed_mps"] == "0.08"
    assert rows[0]["pwm_left"] == "-5000"
    assert rows[0]["pwm_right"] == "5000"
    assert rows[0]["encoder_left"] == "-123"
    assert rows[0]["encoder_right"] == "456"
    assert json.loads(rows[0]["imu_accel"]) == [0.0, 0.0, 9.81]
    assert json.loads(rows[0]["imu_gyro"]) == [0.1, 0.2, 0.3]
    assert json.loads(rows[0]["attitude"]) == [1.0, 2.0, 3.0]


def test_log_file_preserves_full_utc_source_direction_text_and_hex(tmp_path):
    recorder = SessionRecorder()
    entry = LogEntry("2026-10-03T06:00:00.000Z", 100.0, "SERIAL", "INFO",
                     "收到原始数据", b"\xff\x00\r\n", "RX")
    try:
        directory = recorder.start(tmp_path)
        assert recorder.record_log(entry)
    finally:
        assert recorder.stop()
    content = (directory / "communication.log").read_text(encoding="utf-8")
    assert "2026-10-03T06:00:00.000Z [SERIAL] RX" in content
    assert "HEX=FF 00 0D 0A" in content
    assert "TEXT=b'\\xff\\x00\\r\\n'" in content
    assert recorder.log_count == 1
    assert "FF 00 0D 0A" in entry.format(display_hex=True)


def test_start_failure_leaves_no_recording_thread(tmp_path):
    occupied = tmp_path / "file"
    occupied.write_text("occupied", encoding="utf-8")
    recorder = SessionRecorder()
    with pytest.raises(OSError):
        recorder.start(occupied)
    assert not recorder.is_active
    assert not recorder.is_running
    assert not recorder.record_sample(sample())
    assert recorder.stop()


def test_explicit_stop_flushes_accepted_queue_and_allows_another_session(tmp_path):
    recorder = SessionRecorder()
    directories = []
    for _ in range(2):
        try:
            directories.append(recorder.start(tmp_path))
            for index in range(100):
                assert recorder.record_sample(sample(index * 0.05))
        finally:
            assert recorder.stop()
        assert not recorder.is_active
        assert not recorder.is_running
        assert recorder.sample_count == 100
        assert not recorder.record_sample(sample())
        assert recorder.take_notifications() == (None, 0)
    assert directories[0] != directories[1]
    assert all(len(read_csv(directory)[1]) == 100 for directory in directories)


def test_sixty_second_history_keeps_boundary_and_prunes_after_it():
    history = TelemetryHistory()
    history.append(sample(0.0))
    history.append(sample(59.99))
    history.append(sample(60.0))
    assert [item.received_monotonic for item in history.samples] == [0.0, 59.99, 60.0]
    history.prune(60.001)
    assert [item.received_monotonic for item in history.samples] == [59.99, 60.0]
    history.prune(120.001)
    assert list(history.samples) == []


def test_history_capacity_is_independent_of_visible_time_window():
    history = TelemetryHistory(window_seconds=60.0, capacity=3)
    for when in range(10):
        history.append(sample(float(when)))
    assert len(history.samples) == 3
    assert [item.received_monotonic for item in history.samples] == [7.0, 8.0, 9.0]
    history.clear()
    assert list(history.samples) == []


def test_recorder_persistent_drop_total_and_integrity_footer_survive_notification_read(
    tmp_path, monkeypatch
):
    import threading

    recorder = SessionRecorder(queue_capacity=1)
    directory = recorder.start(tmp_path)
    writing, release = threading.Event(), threading.Event()
    original_writerow = csv.DictWriter.writerow

    def blocked_writerow(self, row):
        writing.set()
        assert release.wait(2.0), "test did not release the recorder writer"
        return original_writerow(self, row)

    monkeypatch.setattr(csv.DictWriter, "writerow", blocked_writerow)
    try:
        assert recorder.record_sample(sample())
        assert writing.wait(1.0)
        assert recorder.record_sample(sample(100.05))
        assert not recorder.record_sample(sample(100.1))
        assert recorder.take_notifications() == (None, 1)
        assert recorder.take_notifications() == (None, 0)
        assert recorder.dropped_total == 1
        assert not recorder.stop(timeout=0.001)
    finally:
        release.set()
        assert recorder.stop()
    assert recorder.dropped_total == 1
    assert recorder.failure_message is None
    assert len(read_csv(directory)[1]) == 2
    footer = (directory / "communication.log").read_text(encoding="utf-8")
    assert "INTEGRITY" in footer
    assert "dropped_records=1" in footer
    assert "failure=None" in footer


def test_recorder_failure_message_and_integrity_footer_survive_notification_read(
    tmp_path, monkeypatch
):
    import time

    recorder = SessionRecorder()
    directory = recorder.start(tmp_path)

    def disk_full(self, row):
        raise OSError("disk full for integrity regression")

    monkeypatch.setattr(csv.DictWriter, "writerow", disk_full)
    try:
        assert recorder.record_sample(sample())
        deadline = time.monotonic() + 1.0
        while recorder.is_running and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not recorder.is_running
        error, dropped = recorder.take_notifications()
        assert "disk full for integrity regression" in error
        assert dropped == 0
        assert recorder.take_notifications() == (None, 0)
        assert "disk full for integrity regression" in recorder.failure_message
    finally:
        assert recorder.stop()
    footer = (directory / "communication.log").read_text(encoding="utf-8")
    assert "INTEGRITY" in footer
    assert "dropped_records=0" in footer
    assert "disk full for integrity regression" in footer
