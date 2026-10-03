"""Pure-Python protocol tests; no Qt application or serial hardware required."""

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from car_host.models import TelemetrySample
from car_host.protocol import LegacyTelemetryAdapter, MAX_FRAME_BYTES, MAX_PENDING_ISSUES


UTC_TIME = "2026-10-03T06:00:00.000Z"
FRAME = b"{C15:8:96}$"


def decode(adapter: LegacyTelemetryAdapter, data: bytes) -> list[TelemetrySample]:
    return adapter.feed(data, received_monotonic=123.5, received_utc=UTC_TIME)


def test_valid_frame_conversion_and_model_defaults() -> None:
    adapter = LegacyTelemetryAdapter("serial:COM3")
    sample, = decode(adapter, FRAME)
    assert sample.received_monotonic == 123.5
    assert sample.received_utc == UTC_TIME
    assert sample.source == "serial:COM3"
    assert sample.protocol == "LEGACY_APP"
    assert sample.left_speed_abs_mps == 0.15
    assert sample.right_speed_abs_mps == 0.08
    assert sample.battery_percent_raw == 96
    assert sample.quality == ()
    assert sample.raw_frame == FRAME
    for field in (
        "device_time_ms", "pwm_left", "pwm_right", "encoder_left", "encoder_right",
        "signed_left_speed_mps", "signed_right_speed_mps", "imu_accel", "imu_gyro",
        "attitude",
    ):
        assert getattr(sample, field) is None
    with pytest.raises(FrozenInstanceError):
        sample.battery_percent_raw = 0
    assert adapter.pop_issues() == []
    assert adapter.diagnostics["frames_parsed"] == 1


@pytest.mark.parametrize("split", range(len(FRAME) + 1))
def test_every_split_point(split: int) -> None:
    adapter = LegacyTelemetryAdapter("test")
    samples = decode(adapter, FRAME[:split]) + decode(adapter, FRAME[split:])
    assert len(samples) == 1
    assert samples[0].raw_frame == FRAME
    assert adapter.diagnostics["buffered_bytes"] == 0


def test_byte_by_byte_and_coalesced_frames() -> None:
    adapter = LegacyTelemetryAdapter("test")
    samples = []
    for value in FRAME:
        samples.extend(decode(adapter, bytes([value])))
    samples.extend(decode(adapter, b"{C0:0:0}${C250:125:100}$"))
    assert [sample.left_speed_abs_mps for sample in samples] == [0.15, 0.0, 2.5]
    assert adapter.diagnostics["frames_parsed"] == 3


def test_noise_and_duplicate_start_resynchronize() -> None:
    adapter = LegacyTelemetryAdapter("test")
    samples = decode(adapter, b"noise\x00\xff{incomplete{" + FRAME[1:] + b"\r\n")
    assert len(samples) == 1
    assert samples[0].raw_frame == FRAME
    assert adapter.diagnostics["rejected_frames"] == 1
    assert adapter.diagnostics["discarded_bytes"] > 0
    assert adapter.pop_issues()
    assert adapter.pop_issues() == []


@pytest.mark.parametrize("bad_frame", [
    b"{C-1:2:50}$", b"{C1:-2:50}$", b"{C1.5:2:50}$", b"{C1:2}$",
    b"{C1:2:NaN}$", b"{C1:2:50:7}$", b"{D1:2:50}$", b"{C 1:2:50}$",
    b"{C1:2:50}\n$", b"{C1:\xff:50}$",
])
def test_bad_frames_rejected_and_next_valid_frame_recovers(bad_frame: bytes) -> None:
    adapter = LegacyTelemetryAdapter("test")
    samples = decode(adapter, bad_frame + FRAME)
    assert [sample.raw_frame for sample in samples] == [FRAME]
    assert adapter.diagnostics["rejected_frames"] >= 1
    assert adapter.pop_issues()


@pytest.mark.parametrize("battery", [-10, 101, 999])
def test_out_of_range_battery_preserved_and_flagged(battery: int) -> None:
    adapter = LegacyTelemetryAdapter("test")
    sample, = decode(adapter, f"{{C1:2:{battery}}}$".encode("ascii"))
    assert sample.battery_percent_raw == battery
    assert sample.quality == ("battery_out_of_range",)
    assert adapter.diagnostics["frames_parsed"] == 1
    assert adapter.diagnostics["rejected_frames"] == 0
    assert str(battery) in adapter.pop_issues()[0]


def test_explicit_positive_battery_sign() -> None:
    adapter = LegacyTelemetryAdapter("test")
    sample, = decode(adapter, b"{C1:2:+50}$")
    assert sample.battery_percent_raw == 50


def test_oversized_frame_and_large_noise_recover_with_bounded_storage() -> None:
    adapter = LegacyTelemetryAdapter("test")
    assert decode(adapter, b"{" + b"x" * (MAX_FRAME_BYTES - 1)) == []
    assert adapter.diagnostics["buffered_bytes"] == MAX_FRAME_BYTES
    samples = decode(adapter, b"x" * 100_000 + FRAME)
    assert [sample.raw_frame for sample in samples] == [FRAME]
    assert adapter.diagnostics["oversized_frames"] == 1
    assert adapter.diagnostics["buffered_bytes"] == 0
    assert adapter.diagnostics["discarded_bytes"] == MAX_FRAME_BYTES + 100_000


def test_exact_maximum_frame_size_is_accepted() -> None:
    adapter = LegacyTelemetryAdapter("test")
    frame = b"{C" + b"0" * (MAX_FRAME_BYTES - 10) + b"1:2:50}$"
    assert len(frame) == MAX_FRAME_BYTES
    sample, = decode(adapter, frame)
    assert sample.left_speed_abs_mps == 0.01
    assert adapter.diagnostics["oversized_frames"] == 0


def test_issue_queue_and_diagnostics_snapshot_are_bounded_and_independent() -> None:
    adapter = LegacyTelemetryAdapter("test")
    decode(adapter, b"{bad}$" * (MAX_PENDING_ISSUES + 10))
    snapshot = adapter.diagnostics
    assert snapshot["issues_dropped"] == 10
    snapshot["frames_parsed"] = 999
    assert adapter.diagnostics["frames_parsed"] == 0
    assert len(adapter.pop_issues()) == MAX_PENDING_ISSUES


def test_reset_discards_partial_frame_and_clears_diagnostics() -> None:
    adapter = LegacyTelemetryAdapter("test")
    decode(adapter, b"noise{C12:")
    adapter.reset()
    assert all(value == 0 for value in adapter.diagnostics.values())
    assert adapter.pop_issues() == []
    assert decode(adapter, b"3:50}$") == []
    assert len(decode(adapter, FRAME)) == 1


def test_completion_call_provides_timestamp_and_defaults_are_utc() -> None:
    adapter = LegacyTelemetryAdapter("test")
    assert adapter.feed(FRAME[:3], 10.0, "2026-10-03T05:00:00Z") == []
    sample, = adapter.feed(FRAME[3:], 20.0, UTC_TIME)
    assert sample.received_monotonic == 20.0
    assert sample.received_utc == UTC_TIME
    default_sample, = adapter.feed(FRAME)
    assert default_sample.received_monotonic > 0
    assert datetime.fromisoformat(default_sample.received_utc.replace("Z", "+00:00")).utcoffset().total_seconds() == 0


def test_input_must_be_bytes() -> None:
    adapter = LegacyTelemetryAdapter("test")
    with pytest.raises(TypeError):
        adapter.feed("{C1:2:50}$")
