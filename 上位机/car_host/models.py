"""Shared telemetry values, independent of transport and user interface."""

from dataclasses import dataclass


Vector3 = tuple[float, float, float]


@dataclass(frozen=True)
class EncoderTotals:
    """A coherent MCU snapshot of every control-cycle count; never a STATE."""

    received_monotonic: float
    received_utc: str
    source: str
    protocol: str
    device_session: int
    frame_seq: int
    epoch_tick: int
    device_time_ms: int
    sample_seq: int
    total_left: int
    total_right: int
    interval_ms: int
    delayed_samples: int
    initialized: bool
    raw_frame: bytes = b""
    connection_id: str = ""
    rx_first_block_id: int | None = None
    rx_last_block_id: int | None = None


@dataclass(frozen=True)
class TelemetrySample:
    """One received measurement; unavailable firmware fields remain ``None``."""

    received_monotonic: float
    received_utc: str
    source: str
    protocol: str
    left_speed_abs_mps: float
    right_speed_abs_mps: float
    battery_percent_raw: int
    quality: tuple[str, ...] = ()
    raw_frame: bytes = b""
    device_time_ms: int | None = None
    pwm_left: int | None = None
    pwm_right: int | None = None
    encoder_left: int | None = None
    encoder_right: int | None = None
    signed_left_speed_mps: float | None = None
    signed_right_speed_mps: float | None = None
    imu_accel: Vector3 | None = None
    imu_gyro: Vector3 | None = None
    attitude: Vector3 | None = None
    connection_id: str = ""
    battery_mv: int | None = None
    control_mode: int | None = None
    armed: bool | None = None
    stop_reason: int | None = None
    local_enable: bool | None = None
    rx_dropped: int | None = None
    tx_dropped: int | None = None
    parameter_revision: int | None = None
    device_last_seq: int | None = None
    device_session: int | None = None
    frame_seq: int | None = None
    profile_seq: int | None = None
    profile_start_tick: int | None = None
    segment1_actual_tick: int | None = None
    segment_index: int | None = None
    profile_status: int | None = None
    rx_first_block_id: int | None = None
    rx_last_block_id: int | None = None
    rx_first_monotonic: float | None = None
    parent_read_monotonic: float | None = None
    worker_enqueued_monotonic: float | None = None
    controller_started_monotonic: float | None = None
    controller_completed_monotonic: float | None = None
