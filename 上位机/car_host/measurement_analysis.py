"""Finite profile observations and independent raw-byte verification."""

import binascii
from collections import Counter
import csv
import math
from pathlib import Path
import statistics
import struct
import json
import os
from uuid import uuid4


def _delta(tick, origin):
    return (tick - origin) & 0xffffffff


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def summarize_profile(trial):
    seq = trial.get("profile_seq")
    valid = [s for s in trial.get("samples", []) if s.get("connection_id") == trial.get("connection_id")
        and s.get("device_session") == trial.get("session") and s.get("profile_seq") == seq
        and type(seq) is int and not s.get("quality") and type(s.get("device_time_ms")) is int
        and type(s.get("profile_start_tick")) is int and s.get("profile_status") in (1, 2)]
    active, seen = [], set()
    for sample in valid:
        identity = (sample["device_time_ms"], sample.get("frame_seq"))
        if identity not in seen and sample.get("profile_status") == 1 and sample.get("armed") is True and sample.get("control_mode") == 1:
            active.append(sample)
            seen.add(identity)
    origin = valid[0]["profile_start_tick"] if valid else None
    if origin is not None:
        active.sort(key=lambda s: _delta(s["device_time_ms"], origin))
    conflicting = (len({s["profile_start_tick"] for s in valid}) > 1
        or len({s.get("segment1_actual_tick") for s in valid if s.get("segment_index") == 1}) > 1
        or any(a.get("segment_index") == 1 and b.get("segment_index") == 0 for a, b in zip(active, active[1:])))
    result, stage_rows = {}, []
    for index in (0, 1):
        samples = [s for s in active if s.get("segment_index") == index]
        ticks = [s["device_time_ms"] for s in samples]
        spans = [_delta(ticks[i], ticks[i - 1]) for i in range(1, len(ticks))]
        span = _delta(ticks[-1], ticks[0]) if ticks else 0
        actual = (samples[0]["profile_start_tick"] if index == 0 else samples[0].get("segment1_actual_tick")) if samples else None
        expected = trial["segments"][index]
        pwms_match = all(s.get("pwm_left") == expected["left_pwm"] and s.get("pwm_right") == expected["right_pwm"] for s in samples)
        # Four points alone cover only 150 ms at 20 Hz: require broad coverage.
        sufficient = (len(samples) >= 4 and span >= max(100, expected["duration_ms"] - 150)
                      and max(spans, default=0) <= 100 and pwms_match and not conflicting)
        stage_rows.append(dict(segment_index=index, sample_count=len(samples), observed_span_ms=span,
            actual_start_tick=actual, planned_start_offset_ms=0 if index == 0 else trial["segments"][0]["duration_ms"],
            largest_gap_ms=max(spans, default=0), observation_sufficient=sufficient,
            pwm_matches_plan=pwms_match, actual_start_offset_ms=_delta(actual, origin) if actual is not None and origin is not None else None))
    terminated = [s for s in valid if s.get("profile_status") == 2]
    duration = trial["duration_ms"]
    deadline = trial.get("deadline_tick")
    clipped = bool(origin is not None and type(deadline) is int and _delta(deadline, origin) < duration)
    stop_reason = terminated[0].get("stop_reason") if terminated else None
    interrupted = (stop_reason is not None and stop_reason != 2) or trial.get("status") in ("aborted", "failed")
    for side in ("left", "right"):
        startup_pwm = trial["segments"][0][f"{side}_pwm"]
        if not startup_pwm:
            result[side] = dict(startup="not_tested", maintenance="not_tested", tail4_mean_mps=None)
            continue
        speed_key = f"signed_{side}_speed_mps"
        first = [s for s in active if s.get("segment_index") == 0 and _number(s.get(speed_key))]
        second = [s for s in active if s.get("segment_index") == 1 and _number(s.get(speed_key))]
        startup = ("insufficient" if not stage_rows[0]["observation_sufficient"] else
            "established" if sum(s[speed_key] * startup_pwm > 0 for s in first[-4:]) >= 3 else "not_established")
        tail = second[-4:]
        sustain = len(tail) == 4 and sum(s[speed_key] * startup_pwm > 0 for s in tail) >= 3
        if startup != "established":
            maintenance = "startup_not_established" if startup == "not_established" else "insufficient"
        elif not second:
            maintenance = "segment_missing"
        elif clipped:
            maintenance = "deadline_clipped"
        elif interrupted:
            maintenance = "stopped_early" if stop_reason is not None and stop_reason != 2 else "execution_aborted"
        elif not stage_rows[1]["observation_sufficient"]:
            maintenance = "insufficient"
        else:
            maintenance = "window_sustained" if sustain else "window_not_sustained"
        early = [s[speed_key] for s in second[:4]]
        late = [s[speed_key] for s in tail]
        trend = (statistics.mean(late) - statistics.mean(early)) * (1 if startup_pwm > 0 else -1) if early and late else None
        first_nonzero = next((s for s in first if s[speed_key] * startup_pwm > 0), None)
        result[side] = dict(startup=startup, maintenance=maintenance,
            tail4_compatibility="sustained" if sustain else "not_sustained" if len(tail) == 4 else "insufficient",
            tail4_mean_mps=statistics.mean(late) if late else None,
            tail4_direction_mean_mps=statistics.mean(late) * (1 if startup_pwm > 0 else -1) if late else None,
            early_to_late_direction_delta_mps=trend,
            first_feedback_device_offset_ms=_delta(first_nonzero["device_time_ms"], origin) if first_nonzero else None,
            inertia_conclusion="comparison_required", zero_output_semantics="使能PWM模式中的软件PWM0；不推定断电或自由滑行")
    return dict(profile_metrics=result, segment_observations=stage_rows, profile_segment_observed=bool(stage_rows[1]["sample_count"]),
        profile_deadline_clipped=clipped, profile_start_tick=origin,
        profile_stop_truncated=interrupted, profile_termination_reason=stop_reason,
        profile_evidence_conflict=conflicting,
        profile_terminated_observed=bool(terminated), conclusion_window_ms=trial["segments"][1]["duration_ms"])


def compare_controls(rows):
    """Conservative descriptive comparison; no steady-state or reliability claim."""
    for row in rows:
        if not row.get("segments"):
            continue
        for side in ("left", "right"):
            metric = row["profile_metrics"][side]
            if metric.get("startup") != "established":
                continue
            start = row["segments"][0]
            comparables = [r for r in rows if r.get("segments") and r.get("block_id") == row.get("block_id")
                and r.get("connection_id") == row.get("connection_id") and r.get("session") == row.get("session")
                and r["segments"][0] == start and r["segments"][1]["duration_ms"] == row["segments"][1]["duration_ms"]
                and r["status"] == "completed" and r["profile_metrics"][side].get("startup") == "established"
                and r["profile_metrics"][side].get("maintenance") in ("window_sustained", "window_not_sustained")]
            zeros = [r["profile_metrics"][side]["tail4_direction_mean_mps"] for r in comparables if r.get("control_type") == "zero_output"]
            highs = [r for r in comparables if r.get("control_type") == "high_hold"]
            metric.update(zero_control_valid_n=len(zeros), high_control_valid_n=len(highs))
            if row.get("control_type") != "candidate":
                metric["inertia_conclusion"] = "control_observation"
            elif len(zeros) < 3 or len(highs) < 3:
                metric["inertia_conclusion"] = "control_insufficient"
            elif any(r["profile_metrics"][side]["maintenance"] != "window_sustained" for r in highs):
                metric["inertia_conclusion"] = "positive_control_failed"
            elif metric.get("maintenance") == "window_not_sustained":
                metric["inertia_conclusion"] = "not_sustained_in_window"
            elif metric.get("maintenance") == "window_sustained":
                mean = metric.get("tail4_direction_mean_mps")
                metric["inertia_conclusion"] = "exceeds_zero_control_observations" if mean is not None and mean > max(zeros) else "inertia_not_excluded"


def _raw_fields(frame):
    if len(frame) not in (64, 80) or frame[:2] != b"\xa5\x5a" or frame[2] != 1:
        raise ValueError("原始STATE长度/头无效")
    if binascii.crc_hqx(frame[2:-2], 0xffff) != int.from_bytes(frame[-2:], "little"):
        raise ValueError("原始STATE CRC无效")
    if ((frame[3], len(frame)) not in ((0x83, 64), (0x86, 80))
            or int.from_bytes(frame[4:6], "little") != len(frame) - 16):
        raise ValueError("原始STATE类型/长度无效")
    fields = struct.unpack("<IIiihhhhHhBBBBIIII", frame[14:62])
    mapping = dict(device_time_ms=fields[0], device_last_seq=fields[1], encoder_left=fields[2], encoder_right=fields[3],
        signed_left_speed_mps=fields[4] / 1000, signed_right_speed_mps=fields[5] / 1000,
        pwm_left=fields[6], pwm_right=fields[7], battery_mv=fields[8], control_mode=fields[10],
        armed=bool(fields[11]), stop_reason=fields[12], local_enable=bool(fields[13]),
        rx_dropped=fields[14], tx_dropped=fields[15], parameter_revision=fields[16],
        device_session=int.from_bytes(frame[6:10], "little"), frame_seq=int.from_bytes(frame[10:14], "little"))
    if fields[17] or fields[10] > 2 or fields[11] > 1 or fields[12] > 5 or fields[13] > 1:
        raise ValueError("STATE保留字段/枚举无效")
    if frame[3] == 0x86:
        seq, start, actual, index, status, reserved = struct.unpack("<IIIBBH", frame[62:78])
        if (reserved or status > 2 or index not in (0, 1, 255) or seq > fields[1]
            or (status == 0 and (seq or start or actual or index != 255))
            or (status != 0 and not seq) or (index != 1 and actual)
            or (status == 1 and (index == 255 or not fields[11] or fields[10] != 1))
            or (status == 2 and fields[11])
            or (status and (fields[0] - start) % 2**32 >= 2**31)
            or (index == 1 and ((actual - start) % 2**32 >= 2**31 or (fields[0] - actual) % 2**32 >= 2**31))):
            raise ValueError("PROFILE保留字段/执行身份无效")
        mapping.update(profile_seq=seq, profile_start_tick=start, segment1_actual_tick=actual,
                       segment_index=index, profile_status=status)
    return mapping


def verify_raw_recording(directory, trials):
    """Reassemble raw RX independently of the live parser and compare CSV fields."""
    directory = Path(directory)
    csv_path, log_path = directory / "telemetry.csv", directory / "communication.log"
    if not csv_path.exists() or not log_path.exists():
        return dict(verified=False, reason="缺少原始telemetry.csv或communication.log")
    stream, raw_states, errors, skipped = bytearray(), Counter(), [], 0
    with log_path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if "] RX |" not in line or " | HEX=" not in line:
                continue
            try:
                stream.extend(bytes.fromhex(line.split(" | HEX=", 1)[1]))
            except ValueError:
                errors.append("RX HEX无效")
                continue
            while len(stream) >= 2:
                start = stream.find(b"\xa5\x5a")
                if start < 0:
                    skipped += len(stream) - 1
                    del stream[:-1]
                    break
                if start:
                    skipped += start
                    del stream[:start]
                if len(stream) < 16:
                    break
                length = int.from_bytes(stream[4:6], "little")
                if stream[2] != 1 or length > 64:
                    errors.append("RX帧头/长度无效")
                    del stream[0]
                    continue
                if len(stream) < length + 16:
                    break
                frame = bytes(stream[:length + 16])
                if binascii.crc_hqx(frame[2:-2], 0xffff) != int.from_bytes(frame[-2:], "little"):
                    errors.append("RX CRC无效")
                    del stream[0]
                    continue
                del stream[:length + 16]
                if frame[3] in (0x83, 0x86):
                    raw_states[frame.hex()] += 1
    csv_states, field_errors = Counter(), 0
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                frame = bytes.fromhex(row["raw_frame_hex"])
                decoded = _raw_fields(frame)
                csv_states[frame.hex()] += 1
                for key, value in decoded.items():
                    text = row.get(key)
                    if text is None:
                        # Old CSV did not expose the wire frame sequence.
                        if key != "frame_seq":
                            field_errors += 1
                    elif isinstance(value, bool):
                        field_errors += text.lower() not in (("true", "1") if value else ("false", "0"))
                    elif abs(float(text) - value) > 1e-9:
                        field_errors += 1
            except (ValueError, KeyError, TypeError):
                field_errors += 1
    trial_errors = 0
    for trial in trials:
        for sample in trial.get("samples", []):
            try:
                decoded = _raw_fields(bytes.fromhex(sample["raw_frame"]))
                trial_errors += any(sample.get(k) != v for k, v in decoded.items() if k != "frame_seq" or sample.get(k) is not None)
            except (ValueError, KeyError, TypeError):
                trial_errors += 1
    missing = sum((raw_states - csv_states).values())
    extra = sum((csv_states - raw_states).values())
    return dict(verified=not (errors or skipped or len(stream) or missing or extra or field_errors or trial_errors),
        raw_state_count=sum(raw_states.values()), csv_state_count=sum(csv_states.values()),
        raw_missing_from_csv=missing, csv_missing_from_raw=extra, decoded_field_mismatches=field_errors,
        trial_field_mismatches=trial_errors, crc_header_errors=len(errors), skipped_bytes=skipped,
        trailing_bytes=len(stream), limit="只校核已保存RX、CSV和字段；不能证明硬件未丢数据或机械状态")


def _independent_profile(raw_rows, saved):
    """A second classifier from decoded wire fields, not summarize_profile()."""
    candidates = [r for r in raw_rows if r.get("profile_seq") == saved.get("profile_seq")
        and r["device_session"] == saved.get("session") and r["connection_id"] == saved.get("connection_id")]
    active = {}
    for row in candidates:
        if row.get("profile_status") == 1 and row["armed"] and row["control_mode"] == 1:
            active[(row["device_time_ms"], row["frame_seq"])] = row
    origin = candidates[0]["profile_start_tick"] if candidates else None
    ordered = sorted(active.values(), key=lambda r: (r["device_time_ms"] - origin) % 2**32) if origin is not None else []
    conflicting = (len({r["profile_start_tick"] for r in candidates}) > 1
        or len({r["segment1_actual_tick"] for r in candidates if r.get("segment_index") == 1}) > 1
        or any(a["segment_index"] == 1 and b["segment_index"] == 0 for a, b in zip(ordered, ordered[1:])))
    groups = [[r for r in ordered if r["segment_index"] == index] for index in (0, 1)]
    coverage = []
    for index, points in enumerate(groups):
        span = (points[-1]["device_time_ms"] - points[0]["device_time_ms"]) % 2**32 if points else 0
        largest_gap = max(((b["device_time_ms"] - a["device_time_ms"]) % 2**32 for a, b in zip(points, points[1:])), default=0)
        spec = saved["segments"][index]
        sufficient = len(points) >= 4 and span >= max(100, spec["duration_ms"] - 150) and largest_gap <= 100
        sufficient = sufficient and not conflicting and all(p["pwm_left"] == spec["left_pwm"] and p["pwm_right"] == spec["right_pwm"] for p in points)
        coverage.append(dict(sample_count=len(points), observed_span_ms=span, largest_gap_ms=largest_gap,
                             observation_sufficient=sufficient))
    clipped = origin is not None and type(saved.get("deadline_tick")) is int and (saved["deadline_tick"] - origin) % 2**32 < saved["duration_ms"]
    terminated = [r for r in candidates if r.get("profile_status") == 2]
    stop_reason = terminated[0].get("stop_reason") if terminated else None
    interrupted = (stop_reason is not None and stop_reason != 2) or saved.get("status") in ("aborted", "failed")
    metrics = {}
    for side in ("left", "right"):
        direction = saved["segments"][0][f"{side}_pwm"]
        if direction == 0:
            metrics[side] = dict(startup="not_tested", maintenance="not_tested", tail4_mean_mps=None)
            continue
        startup_tail = [p[f"signed_{side}_speed_mps"] for p in groups[0][-4:]]
        maintenance_tail = [p[f"signed_{side}_speed_mps"] for p in groups[1][-4:]]
        startup = "insufficient"
        if coverage[0]["observation_sufficient"]:
            startup = "established" if len([v for v in startup_tail if v * direction > 0]) >= 3 else "not_established"
        if startup == "not_established":
            maintenance = "startup_not_established"
        elif startup != "established":
            maintenance = "insufficient"
        elif not groups[1]:
            maintenance = "segment_missing"
        elif clipped:
            maintenance = "deadline_clipped"
        elif interrupted:
            maintenance = "stopped_early" if stop_reason is not None and stop_reason != 2 else "execution_aborted"
        elif not coverage[1]["observation_sufficient"]:
            maintenance = "insufficient"
        else:
            maintenance = "window_sustained" if sum(v * direction > 0 for v in maintenance_tail) >= 3 else "window_not_sustained"
        metrics[side] = dict(startup=startup, maintenance=maintenance,
                            tail4_mean_mps=sum(maintenance_tail) / len(maintenance_tail) if maintenance_tail else None)
    return metrics, coverage


def _independent_fixed(raw_rows, saved, next_arm):
    points, seen = [], set()
    for row in raw_rows:
        if (row["connection_id"] != saved.get("connection_id") or row["device_session"] != saved.get("session")
            or not row["armed"] or row["control_mode"] != 1 or type(saved.get("pwm_seq")) is not int
            or row["device_last_seq"] < saved["pwm_seq"]
            or type(saved.get("stop_seq")) is int and row["device_last_seq"] >= saved["stop_seq"]
            or type(next_arm) is int and row["device_last_seq"] >= next_arm
            or not (row["pwm_left"] or row["pwm_right"]) or row["device_time_ms"] in seen):
            continue
        seen.add(row["device_time_ms"])
        points.append(row)
    points.sort(key=lambda r: r["received_monotonic"])
    metrics = {}
    for side in ("left", "right"):
        target = saved.get(f"{side}_pwm", 0)
        values = [p[f"signed_{side}_speed_mps"] for p in points if p[f"pwm_{side}"] * target > 0][-4:]
        metrics[side] = ("not_tested" if not target else "insufficient" if len(values) < 4 else
                        "sustained" if sum(v * target > 0 for v in values) >= 3 else "not_sustained")
    return metrics


def recompute_run(directory, *, saved_rows=None, save=True):
    """Read-only originals; save a separate recompute.json with independent checks.

    ``saved_rows`` is supplied by Journal before publishing summary; public callers
    omit it to read summary.json/results.csv (v1 and v2). No device access.
    """
    directory = Path(directory).resolve()
    rows = saved_rows
    if rows is None:
        summary_path = directory / "summary.json"
        if summary_path.exists():
            if summary_path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("汇总超过8MiB复算上限")
            rows = json.loads(summary_path.read_text(encoding="utf-8"))["rows"]
        else:
            with (directory / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                for key in ("segments", "profile_metrics", "segment_observations"):
                    row[key] = json.loads(row[key]) if row.get(key) else None
                for key in ("session", "arm_seq", "pwm_seq", "stop_seq", "profile_seq", "deadline_tick", "duration_ms", "left_pwm", "right_pwm"):
                    row[key] = int(row[key]) if row.get(key) not in (None, "") else None
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError("复算试次超过128或格式无效")
    plan_path, context_path = directory / "plan.json", directory / "context.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
    context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.exists() else {}
    raw_rows, decoding_errors = [], 0
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as handle:
        for number, csv_row in enumerate(csv.DictReader(handle)):
            if number >= 1_000_000:
                raise ValueError("复算遥测超过100万行上限")
            try:
                # Quality filters are persisted facts; measurements decode from raw.
                if csv_row.get("quality"):
                    continue
                decoded = _raw_fields(bytes.fromhex(csv_row["raw_frame_hex"]))
                decoded.update(connection_id=csv_row.get("connection_id", ""),
                               received_monotonic=float(csv_row["received_monotonic"]))
                raw_rows.append(decoded)
            except (KeyError, ValueError, TypeError):
                decoding_errors += 1
    mismatches, recomputed = [], []
    mismatch_count = 0

    def check(trial_id, key, actual, expected):
        nonlocal mismatch_count
        equal = actual == expected
        if _number(actual) and _number(expected):
            equal = abs(actual - expected) <= 1e-9
        if not equal:
            mismatch_count += 1
            if len(mismatches) < 128:
                mismatches.append(dict(trial_id=trial_id, field=key, recomputed=actual, saved=expected))

    for index, row in enumerate(rows):
        trial_id = row["trial_id"]
        if row.get("segments"):
            metrics, coverage = _independent_profile(raw_rows, row)
            recomputed.append(dict(trial_id=trial_id, profile_metrics=metrics, segment_observations=coverage))
            for side in ("left", "right"):
                for key in ("startup", "maintenance", "tail4_mean_mps"):
                    check(trial_id, f"{side}.{key}", metrics[side][key], row["profile_metrics"][side].get(key))
            for segment_index, segment in enumerate(coverage):
                saved = row["segment_observations"][segment_index]
                for key, actual in segment.items():
                    check(trial_id, f"segment{segment_index}.{key}", actual, saved.get(key))
        else:
            following_arm = rows[index + 1].get("arm_seq") if index + 1 < len(rows) else None
            metrics = _independent_fixed(raw_rows, row, following_arm)
            recomputed.append(dict(trial_id=trial_id, **{f"{side}_feedback": value for side, value in metrics.items()}))
            for side, actual in metrics.items():
                check(trial_id, f"{side}_feedback", actual, row.get(f"{side}_feedback"))
    raw_check = verify_raw_recording(directory, [])
    result = dict(schema_version=2, source_schema_version=plan.get("schema_version", 1),
        run_id=context.get("run_id"), verified=raw_check["verified"] and not decoding_errors and not mismatch_count,
        raw_verification=raw_check, decoding_errors=decoding_errors, classification_mismatch_count=mismatch_count,
        mismatches=mismatches, rows=recomputed,
        method="独立CRC原始字段解码与第二实现的尾4/分段coverage分类；不调用在线分类器",
        limit="仅校核已保存记录；不证明未记录硬件数据、惯性因果、可靠性或机械停车")
    if save:
        temporary = directory / f"recompute.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(directory / "recompute.json")
        finally:
            temporary.unlink(missing_ok=True)
    return result
