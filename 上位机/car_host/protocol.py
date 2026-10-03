"""Bounded incremental decoding of the telemetry emitted by current firmware."""

from collections import deque
from datetime import datetime, timezone
import re
import time

from .models import TelemetrySample


LEGACY_APP = "LEGACY_APP"
MAX_FRAME_BYTES = 64
MAX_PENDING_ISSUES = 128
_FRAME_PATTERN = re.compile(rb"\{C([0-9]+):([0-9]+):([+-]?[0-9]+)\}\$")


class LegacyTelemetryAdapter:
    """Decode ``{Cleft:right:battery}$`` without depending on line endings.

    Wheel values are unsigned hundredths of m/s. The firmware's battery
    estimate can exceed 0–100, so its original value is retained and flagged.
    The frame buffer never exceeds 64 bytes, including frame delimiters.
    """

    protocol = LEGACY_APP

    def __init__(self, source: str) -> None:
        self.source = source
        self._buffer = bytearray()
        self._issues: deque[str] = deque(maxlen=MAX_PENDING_ISSUES)
        self._counters: dict[str, int] = {}
        self.reset()

    @property
    def diagnostics(self) -> dict[str, int]:
        """Return counters and current buffer size as an independent snapshot.

        ``frames_parsed`` counts accepted frames, including flagged battery
        readings. ``rejected_frames`` includes incomplete frames superseded by
        a new start marker and oversized frames. ``discarded_bytes`` counts
        noise and rejected candidate bytes. ``issues_dropped`` counts oldest
        pending issues removed when the bounded issue queue is full.
        """

        return {**self._counters, "buffered_bytes": len(self._buffer)}

    def reset(self) -> None:
        """Clear partial input, pending issues, and diagnostics for a new stream."""

        self._buffer.clear()
        self._issues.clear()
        self._counters = {
            "bytes_received": 0,
            "frames_parsed": 0,
            "rejected_frames": 0,
            "oversized_frames": 0,
            "discarded_bytes": 0,
            "issues_dropped": 0,
        }

    def pop_issues(self) -> list[str]:
        """Consume pending diagnostic messages, oldest first."""

        issues = list(self._issues)
        self._issues.clear()
        return issues

    def _issue(self, message: str) -> None:
        if len(self._issues) == MAX_PENDING_ISSUES:
            self._counters["issues_dropped"] += 1
        self._issues.append(message)

    def _reject(self, message: str, *, oversized: bool = False) -> None:
        self._counters["rejected_frames"] += 1
        self._counters["discarded_bytes"] += len(self._buffer)
        if oversized:
            self._counters["oversized_frames"] += 1
        self._buffer.clear()
        self._issue(message)

    def feed(
        self,
        chunk: bytes,
        received_monotonic: float | None = None,
        received_utc: str | None = None,
    ) -> list[TelemetrySample]:
        """Decode complete frames; timestamp them when the final bytes arrive."""

        if not isinstance(chunk, bytes):
            raise TypeError("chunk must be bytes")
        if received_monotonic is None:
            received_monotonic = time.monotonic()
        if received_utc is None:
            received_utc = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")

        self._counters["bytes_received"] += len(chunk)
        samples: list[TelemetrySample] = []
        noise_bytes = 0

        for value in chunk:
            if value == ord("{"):
                if self._buffer:
                    self._reject("重复帧起始标记，已丢弃不完整帧并重新同步。")
                self._buffer.append(value)
                continue

            if not self._buffer:
                self._counters["discarded_bytes"] += 1
                noise_bytes += 1
                continue

            if len(self._buffer) == MAX_FRAME_BYTES:
                self._reject("帧超过 64 字节，已丢弃并等待下一帧。", oversized=True)
                self._counters["discarded_bytes"] += 1
                noise_bytes += 1
                continue

            self._buffer.append(value)
            if not self._buffer.endswith(b"}$"):
                continue

            raw_frame = bytes(self._buffer)
            match = _FRAME_PATTERN.fullmatch(raw_frame)
            if match is None:
                self._reject("无效 LEGACY_APP 帧，要求 {C非负整数:非负整数:有符号整数}$。")
                continue

            self._buffer.clear()
            left, right, battery = (int(field) for field in match.groups())
            quality: tuple[str, ...] = ()
            if not 0 <= battery <= 100:
                quality = ("battery_out_of_range",)
                self._issue(f"电量估算值 {battery}% 超出 0–100，保留原始值。")

            samples.append(
                TelemetrySample(
                    received_monotonic=received_monotonic,
                    received_utc=received_utc,
                    source=self.source,
                    protocol=self.protocol,
                    left_speed_abs_mps=left / 100,
                    right_speed_abs_mps=right / 100,
                    battery_percent_raw=battery,
                    quality=quality,
                    raw_frame=raw_frame,
                )
            )
            self._counters["frames_parsed"] += 1

        if noise_bytes:
            self._issue(f"丢弃 {noise_bytes} 字节帧外数据。")
        return samples
