"""Bounded receive evidence; clocks retain the time of the actual observation."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
import time

from .models import TelemetrySample
from .transport import IoEvent


def rx_metadata(event: IoEvent) -> dict:
    """Metadata passed to the parser without replacing its capture clock."""
    return {"rx_block_id": getattr(event, "rx_block_id", None),
            "parent_read_monotonic": getattr(event, "parent_read_monotonic", None),
            "worker_enqueued_monotonic": getattr(event, "worker_enqueued_monotonic", None)}


def rx_trace(event: IoEvent, started: float, completed: float) -> dict:
    return {"kind": "rx_block", "connection_id": event.connection_id,
            "rx_block_id": getattr(event, "rx_block_id", None),
            "captured_monotonic": event.monotonic, "captured_utc": event.utc,
            "parent_read_monotonic": getattr(event, "parent_read_monotonic", None),
            "worker_enqueued_monotonic": getattr(event, "worker_enqueued_monotonic", None),
            "controller_started_monotonic": started,
            "controller_completed_monotonic": completed,
            "byte_count": len(event.payload), "payload_hex": event.payload.hex()}


def clock_context() -> dict:
    clock = time.get_clock_info("monotonic")
    return {"clock": "time.monotonic", "implementation": clock.implementation,
            "resolution_seconds": clock.resolution, "monotonic": clock.monotonic,
            "adjustable": clock.adjustable,
            "capture_boundary": "transport read completed",
            "parent_boundary": "QProcess read completed; OS arrival is unobserved"}


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _counter_values(values, prefix=""):
    result = {}
    for key, value in (values or {}).items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            result.update(_counter_values(value, name))
        elif type(value) is int and value >= 0:
            result[name] = value
    return result


def _percentile(sorted_values, fraction):
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def _stats(values, total):
    numbers = sorted(value for value in values if value is not None and value >= 0)
    return {"count": len(numbers), "missing": total - len(numbers),
            "p50": _percentile(numbers, .5) if numbers else None,
            "p95": _percentile(numbers, .95) if numbers else None,
            "max": max(numbers) if numbers else None}


class ReceiveHealth:
    """A passive 60-second window, bounded independently of traffic rate."""

    METRICS = ("capture_to_parent_read", "parent_read_to_worker_enqueue",
               "worker_queue_wait", "capture_to_controller", "controller_processing")
    BASELINE_EVENT_CAPACITY = 128
    BASELINE_HISTORY_CAPACITY = 8

    def __init__(self, window_seconds: float = 60.0, capacity: int = 2048,
                 *, expected_state_period_ms: int = 50):
        if not _number(window_seconds) or window_seconds <= 0:
            raise ValueError("健康窗口必须为正秒数")
        if type(capacity) is not int or not 1 <= capacity <= 65536:
            raise ValueError("健康窗口容量必须为 1..65536")
        if type(expected_state_period_ms) is not int or expected_state_period_ms <= 0:
            raise ValueError("STATE 周期必须为正毫秒数")
        self.window_seconds = float(window_seconds)
        self.capacity = capacity
        self.expected_state_period_ms = expected_state_period_ms
        self.epoch = 0
        self.connection_id = ""
        self.session = None
        self._samples = deque(maxlen=capacity)
        self._batches = deque(maxlen=min(capacity, 256))
        self._latest = None
        self._baseline = {}
        self._counters = {}
        self._positive_increments = {}
        self._observer_positive_increments_total = {}
        self._counter_resets = set()
        self._epoch_history = deque(maxlen=self.BASELINE_HISTORY_CAPACITY)
        self._counter_reset_history = deque(maxlen=self.BASELINE_HISTORY_CAPACITY)
        self._baseline_events = deque(maxlen=self.BASELINE_EVENT_CAPACITY)
        self._baseline_events_dropped = 0
        self._started_monotonic = None
        self._capacity_evictions = 0
        self._last_evicted_observation = None

    def begin_epoch(self, connection_id: str, session: int | None = None,
                    *, counters: dict | None = None, now: float | None = None):
        now = time.monotonic() if now is None else now
        if not _number(now):
            raise ValueError("健康基线观测时刻必须为有限数值")
        if self.epoch and (connection_id, session) == (self.connection_id, self.session):
            self._update_counters(counters, now=now)
            return False
        if self.epoch:
            previous = dict(epoch=self.epoch, connection_id=self.connection_id, session=self.session,
                            started_monotonic=self._started_monotonic, ended_monotonic=now,
                            counter_baseline=dict(self._baseline), counter_current=dict(self._counters),
                            counter_positive_increments=dict(self._positive_increments))
            self._epoch_history.append(previous)
            self._baseline_event(dict(kind="receive_epoch_ended", **previous))
        self.epoch += 1
        self.connection_id, self.session = connection_id, session
        self._samples.clear()
        self._batches.clear()
        self._latest = None
        self._baseline = _counter_values(counters)
        self._counters = dict(self._baseline)
        self._positive_increments = dict.fromkeys(self._baseline, 0)
        for name in self._baseline:
            self._observer_positive_increments_total.setdefault(name, 0)
        self._counter_resets.clear()
        self._started_monotonic = now
        self._capacity_evictions = 0
        self._last_evicted_observation = None
        self._baseline_event(dict(kind="receive_epoch_started", epoch=self.epoch,
                                  connection_id=self.connection_id, session=self.session,
                                  observed_monotonic=now, counter_baseline=dict(self._baseline)))
        return True

    def _baseline_event(self, record):
        if len(self._baseline_events) == self.BASELINE_EVENT_CAPACITY:
            self._baseline_events_dropped += 1
        self._baseline_events.append(deepcopy(record))

    def pop_baseline_events(self):
        """Return bounded immutable snapshots for the session trace writer."""
        records = deepcopy(list(self._baseline_events))
        self._baseline_events.clear()
        return records

    @property
    def baseline_events_dropped(self):
        return self._baseline_events_dropped

    def _update_counters(self, values, *, now=None):
        initialized = {}
        for name, value in _counter_values(values).items():
            previous = self._counters.get(name)
            if previous is None:
                self._baseline[name] = value
                self._positive_increments[name] = 0
                self._observer_positive_increments_total.setdefault(name, 0)
                initialized[name] = value
            elif value < previous:
                record = dict(kind="counter_baseline_reset", epoch=self.epoch,
                              connection_id=self.connection_id, session=self.session,
                              observed_monotonic=now, counter=name, reason="value_decreased",
                              previous_baseline=self._baseline[name], previous_current=previous,
                              new_baseline=value, epoch_positive_increments=self._positive_increments[name],
                              observer_positive_increments_total=self._observer_positive_increments_total[name])
                self._baseline[name] = value
                self._counter_resets.add(name)
                self._counter_reset_history.append(record)
                self._baseline_event(record)
            elif value > previous:
                increase = value - previous
                self._positive_increments[name] += increase
                self._observer_positive_increments_total[name] += increase
            self._counters[name] = value
        if initialized:
            self._baseline_event(dict(kind="counter_baseline_initialized", epoch=self.epoch,
                                      connection_id=self.connection_id, session=self.session,
                                      observed_monotonic=now, counter_baseline=initialized))

    def invalidate_latest(self):
        """Keep diagnostic history while retiring a state invalidated by stream loss."""
        self._latest = None

    def _prune(self, now):
        cutoff = now - self.window_seconds
        for queue in (self._samples, self._batches):
            while queue and queue[0]["observed_monotonic"] < cutoff:
                queue.popleft()

    def observe_sample(self, sample: TelemetrySample, now: float):
        if not _number(now):
            raise ValueError("健康观测时刻必须为有限数值")
        self.begin_epoch(sample.connection_id, sample.device_session, now=now)
        self._prune(now)
        previous = self._latest
        captured = sample.received_monotonic
        parent = sample.parent_read_monotonic
        enqueued = sample.worker_enqueued_monotonic
        started = sample.controller_started_monotonic
        completed = sample.controller_completed_monotonic
        row = {"observed_monotonic": now, "frame_delta": None, "tick_delta_ms": None,
               "clock_anomalies": 0}
        for name, start, end in (
                ("capture_to_parent_read", captured, parent),
                ("parent_read_to_worker_enqueue", parent, enqueued),
                ("worker_queue_wait", enqueued, started),
                ("capture_to_controller", captured, started),
                ("controller_processing", started, completed)):
            delta = (end - start) * 1000 if _number(start) and _number(end) else None
            if delta is not None and delta < 0:
                row["clock_anomalies"] += 1
            row[name] = delta
        if previous is not None:
            if sample.frame_seq is not None and previous.frame_seq is not None:
                row["frame_delta"] = (sample.frame_seq - previous.frame_seq) & 0xFFFFFFFF
            if sample.device_time_ms is not None and previous.device_time_ms is not None:
                row["tick_delta_ms"] = (sample.device_time_ms - previous.device_time_ms) & 0xFFFFFFFF
        if len(self._samples) == self.capacity:
            self._capacity_evictions += 1
            self._last_evicted_observation = self._samples[0]["observed_monotonic"]
        self._samples.append(row)
        self._latest = sample
        self._update_counters({"device": {"rx_dropped": sample.rx_dropped,
                                           "tx_dropped": sample.tx_dropped}}, now=now)

    def observe_batch(self, record: dict):
        if not isinstance(record, dict):
            raise ValueError("接收批次必须为对象")
        observed = record.get("completed_monotonic", record.get("monotonic"))
        if not _number(observed):
            return
        self._prune(observed)
        self._batches.append({"observed_monotonic": observed,
                              **{name: record[name] for name in (
                                  "started_monotonic", "completed_monotonic", "event_count",
                                  "bytes_read", "remaining_bytes", "budget_exceeded") if name in record}})

    def snapshot(self, now: float, counters: dict | None = None, queue: dict | None = None):
        if not _number(now):
            raise ValueError("健康查询时刻必须为有限数值")
        self._prune(now)
        self._update_counters(counters, now=now)
        rows = tuple(self._samples)
        count = len(rows)
        age = (now - self._latest.received_monotonic) * 1000 if self._latest else None
        frame_deltas = [row["frame_delta"] for row in rows if row["frame_delta"] is not None]
        tick_deltas = [row["tick_delta_ms"] for row in rows if row["tick_delta_ms"] is not None]
        return {"epoch": self.epoch, "connection_id": self.connection_id, "session": self.session,
                "window_seconds": self.window_seconds, "capacity": self.capacity,
                "sample_count": count, "window_capacity_evictions": self._capacity_evictions,
                "window_truncated": (self._last_evicted_observation is not None
                                     and self._last_evicted_observation >= now - self.window_seconds),
                "latest_state_age_ms": age, "state_fresh": age is not None and 0 <= age < 500,
                "latest_frame_seq": self._latest.frame_seq if self._latest else None,
                "latest_tick_ms": self._latest.device_time_ms if self._latest else None,
                "timings_ms": {name: _stats([row[name] for row in rows], count) for name in self.METRICS},
                "frame_gap_count": sum(1 for value in frame_deltas if 1 < value < 0x80000000),
                "frame_missing_count": sum(value - 1 for value in frame_deltas if 1 < value < 0x80000000),
                "frame_replay_or_backwards_count": sum(1 for value in frame_deltas if value == 0 or value >= 0x80000000),
                "tick_gap_count": sum(1 for value in tick_deltas if self.expected_state_period_ms < value < 0x80000000),
                "clock_anomalies": sum(row["clock_anomalies"] for row in rows),
                "counter_baseline": dict(self._baseline), "counter_current": dict(self._counters),
                "counter_delta": {name: value - self._baseline[name] for name, value in self._counters.items()},
                "counter_resets": sorted(self._counter_resets),
                "counter_positive_increments": dict(self._positive_increments),
                "observer_positive_increments_total": dict(self._observer_positive_increments_total),
                "observer_increment_semantics": ("sum of positive differences between successive observations; "
                                                 "baseline resets and new epochs add zero; not MCU absolute totals"),
                "epoch_history": deepcopy(list(self._epoch_history)),
                "counter_reset_history": deepcopy(list(self._counter_reset_history)),
                "baseline_events_pending": len(self._baseline_events),
                "baseline_events_dropped": self._baseline_events_dropped,
                "queue": dict(queue or {}),
                "last_batch": dict(self._batches[-1]) if self._batches else None,
                "clock": clock_context()}
