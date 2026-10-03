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

from .models import TelemetrySample


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
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
            writer.writeheader()
        except Exception:
            csv_file.close()
            if "log_file" in locals():
                log_file.close()
            raise
        self._queue = queue.Queue(maxsize=self._capacity)
        self._stop.clear()
        self._error = None
        self._dropped = 0
        self.dropped_total = 0
        self.failure_message = None
        self.sample_count = self.log_count = 0
        self.directory = directory
        self._accepting = True
        self._thread = threading.Thread(
            target=self._run, args=(csv_file, log_file, writer),
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

    def record_log(self, entry: LogEntry) -> bool:
        return self._offer(entry)

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

    def _run(self, csv_file, log_file, writer) -> None:
        last_flush = time.monotonic()
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
                elif isinstance(item, LogEntry):
                    log_file.write(item.persistent_text())
                    self.log_count += 1
                if time.monotonic() - last_flush >= 0.5:
                    csv_file.flush()
                    log_file.flush()
                    last_flush = time.monotonic()
            csv_file.flush()
            log_file.flush()
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
                               f"dropped_records={self.dropped_total} failure={self.failure_message!r}\n")
                log_file.flush()
            except Exception as exc:
                with self._lock:
                    self._error = f"完整性记录写入失败：{exc}"
                    self.failure_message = self._error
            for handle in (log_file,):
                try:
                    handle.close()
                except Exception as exc:
                    with self._lock:
                        self._error = f"录制文件关闭失败：{exc}"
                        self.failure_message = self._error
