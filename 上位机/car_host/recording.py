"""异步记录；显示暂停与文件录制互不影响。"""

from __future__ import annotations

import csv
import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import TelemetrySample, EncoderTotals


TRACE_SCHEMA_VERSION = 1
MAX_TRACE_BYTES = 16384


@dataclass(frozen=True)
class _TraceRecord:
    line: str


@dataclass(frozen=True)
class LogEntry:
    utc: str
    monotonic: float
    source: str
    level: str
    message: str
    payload: bytes | None = None
    direction: str | None = None
    connection_id: str = ""
    request_id: str = ""

    def format(self, display_hex: bool = False) -> str:
        stamp = self.utc[11:23] if len(self.utc) >= 23 else self.utc
        prefix = f"{stamp} [{self.source}] {self.direction or self.level}"
        if self.request_id:
            prefix += f" #{self.request_id[:8]}"
        if self.payload is not None:
            body = self.payload.hex(" ").upper() if display_hex else repr(self.payload.decode("utf-8", errors="backslashreplace"))[1:-1]
            return f"{prefix} | {body}"
        return f"{prefix} | {self.message}"

    def persistent_text(self) -> str:
        result = f"{self.utc} [{self.source}] {self.direction or self.level} | {self.message}"
        result += f" | connection_id={self.connection_id or '-'} request_id={self.request_id or '-'}"
        if self.payload is not None:
            result += f" | TEXT={self.payload!r} | HEX={self.payload.hex(' ').upper()}"
        return result + "\n"


CSV_FIELDS = (
    "received_utc", "received_monotonic", "source", "protocol",
    "left_speed_abs_mps", "right_speed_abs_mps", "battery_percent_raw",
    "quality", "raw_frame_hex", "device_time_ms", "signed_left_speed_mps",
    "signed_right_speed_mps", "pwm_left", "pwm_right", "encoder_left",
    "encoder_right", "imu_accel", "imu_gyro", "attitude", "connection_id",
    "battery_mv", "control_mode", "armed", "stop_reason", "local_enable",
    "rx_dropped", "tx_dropped", "parameter_revision", "device_last_seq", "device_session",
    "frame_seq", "profile_seq", "profile_start_tick", "segment1_actual_tick", "segment_index", "profile_status",
    "rx_first_block_id", "rx_last_block_id", "rx_first_monotonic", "parent_read_monotonic",
    "worker_enqueued_monotonic", "controller_started_monotonic", "controller_completed_monotonic",
)
ENCODER_CSV_FIELDS = (
    "received_utc", "received_monotonic", "source", "protocol", "connection_id",
    "device_session", "frame_seq", "epoch_tick", "device_time_ms", "sample_seq",
    "total_left", "total_right", "interval_ms", "delayed_samples", "initialized",
    "raw_frame_hex", "rx_first_block_id", "rx_last_block_id",
)


class SessionRecorder:
    """一个录制会话拥有一个有限队列和一个文件写入线程。"""

    def __init__(self, queue_capacity: int = 2048):
        self._capacity = queue_capacity
        self._queue: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._error: str | None = None
        self._dropped = 0
        self.dropped_total = 0
        self.failure_message: str | None = None
        self.directory: Path | None = None
        self.sample_count = 0
        self.log_count = 0
        self.trace_count = 0
        self.encoder_count = 0

    @property
    def is_active(self) -> bool:
        return self._accepting and self._thread is not None and self._thread.is_alive()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, base_directory: Path) -> Path:
        if self.is_running:
            raise RuntimeError("上一录制会话尚未结束")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")
        directory = Path(base_directory) / f"{stamp}_{uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        csv_file = (directory / "telemetry.csv").open("x", encoding="utf-8-sig", newline="")
        try:
            log_file = (directory / "communication.log").open("x", encoding="utf-8")
            trace_file = (directory / "receive_trace.jsonl").open("x", encoding="utf-8")
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
            writer.writeheader()
        except Exception:
            csv_file.close()
            if "log_file" in locals():
                log_file.close()
            if "trace_file" in locals():
                trace_file.close()
            raise
        self._queue = queue.Queue(maxsize=self._capacity)
        self._stop.clear()
        self._error = None
        self._dropped = 0
        self.dropped_total = 0
        self.failure_message = None
        self.sample_count = self.log_count = self.trace_count = 0
        self.encoder_count = 0
        self.directory = directory
        self._accepting = True
        self._thread = threading.Thread(
            target=self._run, args=(csv_file, log_file, trace_file, writer),
            name="car-host-recorder", daemon=False,
        )
        self._thread.start()
        return directory

    def _offer(self, item) -> bool:
        if not self.is_active:
            return False
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
                self.dropped_total += 1
            return False

    def record_sample(self, sample: TelemetrySample) -> bool:
        return self._offer(sample)

    def record_encoder_totals(self, snapshot: EncoderTotals) -> bool:
        return self._offer(snapshot)

    def record_log(self, entry: LogEntry) -> bool:
        return self._offer(entry)

    def record_trace(self, record: dict) -> bool:
        """Freeze a bounded JSON snapshot, then write it on the recording thread."""
        if not isinstance(record, dict):
            raise ValueError("接收诊断记录必须为对象")
        version = record.get("schema_version", TRACE_SCHEMA_VERSION)
        if type(version) is not int or version != TRACE_SCHEMA_VERSION:
            raise ValueError("接收诊断记录版本不支持")
        line = json.dumps(dict(record, schema_version=TRACE_SCHEMA_VERSION), ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False) + "\n"
        if len(line.encode("utf-8")) > MAX_TRACE_BYTES:
            raise ValueError("接收诊断记录超过 16 KiB")
        return self._offer(_TraceRecord(line))

    def take_notifications(self) -> tuple[str | None, int]:
        with self._lock:
            result = (self._error, self._dropped)
            self._error = None
            self._dropped = 0
            return result

    def stop(self, timeout: float = 3.0) -> bool:
        self._accepting = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return not self.is_running

    def _run(self, csv_file, log_file, trace_file, writer) -> None:
        last_flush = time.monotonic()
        encoder_file = encoder_writer = None
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    item = None
                if isinstance(item, TelemetrySample):
                    row = {name: getattr(item, name, None) for name in CSV_FIELDS}
                    row["quality"] = ";".join(item.quality)
                    row["raw_frame_hex"] = item.raw_frame.hex(" ").upper()
                    for name in ("imu_accel", "imu_gyro", "attitude"):
                        value = row[name]
                        row[name] = "" if value is None else json.dumps(value)
                    writer.writerow(row)
                    self.sample_count += 1
                elif isinstance(item, EncoderTotals):
                    if encoder_file is None:
                        encoder_file = (self.directory / "encoder_totals.csv").open("x", encoding="utf-8-sig", newline="")
                        encoder_writer = csv.DictWriter(encoder_file, fieldnames=ENCODER_CSV_FIELDS)
                        encoder_writer.writeheader()
                    row = {name: getattr(item, name, None) for name in ENCODER_CSV_FIELDS}
                    row["raw_frame_hex"] = item.raw_frame.hex(" ").upper()
                    encoder_writer.writerow(row)
                    self.encoder_count += 1
                elif isinstance(item, LogEntry):
                    log_file.write(item.persistent_text())
                    self.log_count += 1
                elif isinstance(item, _TraceRecord):
                    trace_file.write(item.line)
                    self.trace_count += 1
                if time.monotonic() - last_flush >= 0.5:
                    csv_file.flush()
                    log_file.flush()
                    trace_file.flush()
                    if encoder_file is not None:
                        encoder_file.flush()
                    last_flush = time.monotonic()
            csv_file.flush()
            log_file.flush()
            trace_file.flush()
            if encoder_file is not None:
                encoder_file.flush()
        except Exception as exc:
            with self._lock:
                self._error = f"录制写入失败，文件可能不完整：{exc}"
                self.failure_message = self._error
        finally:
            self._accepting = False
            try:
                csv_file.close()
            except Exception as exc:
                with self._lock:
                    self._error = f"遥测文件关闭失败：{exc}"
                    self.failure_message = self._error
            try:
                log_file.write(f"INTEGRITY samples={self.sample_count} logs={self.log_count} "
                               f"dropped_records={self.dropped_total} failure={self.failure_message!r} "
                               f"traces={self.trace_count} encoder_totals={self.encoder_count}\n")
                log_file.flush()
            except Exception as exc:
                with self._lock:
                    self._error = f"完整性记录写入失败：{exc}"
                    self.failure_message = self._error
            for handle in (log_file, trace_file, encoder_file):
                if handle is None:
                    continue
                try:
                    handle.close()
                except Exception as exc:
                    with self._lock:
                        self._error = f"录制文件关闭失败：{exc}"
                        self.failure_message = self._error
