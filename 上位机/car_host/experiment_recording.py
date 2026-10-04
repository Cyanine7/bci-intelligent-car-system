"""Bounded asynchronous experiment evidence and reports, without device I/O."""

import csv
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import queue
import statistics
import threading
import time


_MAX_TRIALS = 128
_MAX_SAMPLES = 256
_METRIC_NAMES = {"sustained": "持续同号反馈", "not_sustained": "未满足持续反馈",
                 "insufficient": "观测不足", "not_tested": "未测试"}
_RESULT_FIELDS = (
    "trial_id", "label", "left_pwm", "right_pwm", "duration_ms", "stop_after_ms",
    "connection_id", "session", "arm_request_id", "arm_seq", "pwm_request_id", "pwm_seq",
    "stop_request_id", "stop_seq", "started_utc", "started_monotonic", "pwm_started_monotonic", "status", "reason",
    "stop_confirmed", "ack_results", "left_feedback", "right_feedback",
    "left_tail_median_mps", "right_tail_median_mps", "left_observation_delay_ms",
    "right_observation_delay_ms", "left_tail_mean_mps", "right_tail_mean_mps",
    "left_tail_std_mps", "right_tail_std_mps", "battery_min_mv", "battery_max_mv",
    "software_output_observed", "eligible_sample_count", "observations",
)


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, bytes):
        return value.hex(" ").upper()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("记录对象的 key 必须是文字")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("记录包含不可序列化或非有限值")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _eligible_samples(trial):
    pwm_seq = trial.get("pwm_seq")
    if type(pwm_seq) is not int:
        return []
    stop_seq = trial.get("stop_seq")
    result, seen_ticks = [], set()
    for sample in trial.get("samples", []):
        if (not isinstance(sample, dict) or sample.get("armed") is not True
                or sample.get("control_mode") != 1
                or sample.get("connection_id") != trial.get("connection_id")
                or sample.get("device_session") != trial.get("session")
                or type(sample.get("device_last_seq")) is not int
                or sample["device_last_seq"] < pwm_seq
                or (type(stop_seq) is int and sample["device_last_seq"] >= stop_seq)
                or not _number(sample.get("received_monotonic"))
                or sample.get("quality")):
            continue
        left, right = sample.get("pwm_left"), sample.get("pwm_right")
        if not any(type(value) is int and value != 0 for value in (left, right)):
            continue
        tick = sample.get("device_time_ms")
        if type(tick) is int:
            if tick in seen_ticks:
                continue
            seen_ticks.add(tick)
        result.append(sample)
    return sorted(result, key=lambda sample: sample["received_monotonic"])


def _wheel_metric(trial, eligible, side):
    target = trial.get(f"{side}_pwm", 0)
    if type(target) is not int or target == 0:
        return dict(feedback="not_tested", tail_median_mps=None, tail_mean_mps=None,
                    tail_std_mps=None, observation_delay_ms=None)
    pwm_key = f"pwm_{side}"
    speed_key = f"signed_{side}_speed_mps"
    # Zero speed is a valid observation, so a stationary wheel is a failure,
    # rather than being hidden by filtering only nonzero measurements.
    valid = [sample for sample in eligible
             if type(sample.get(pwm_key)) is int and sample[pwm_key] * target > 0
             and _number(sample.get(speed_key))]
    tail = valid[-4:]
    feedback = "insufficient" if len(tail) < 4 else (
        "sustained" if sum(sample[speed_key] * target > 0 for sample in tail) >= 3
        else "not_sustained")
    start = trial.get("pwm_started_monotonic")
    if not _number(start):
        start = trial.get("started_monotonic")
    first = next((sample for sample in valid if sample[speed_key] * target > 0), None)
    delay = ((first["received_monotonic"] - start) * 1000
             if first is not None and _number(start) and first["received_monotonic"] >= start else None)
    return dict(feedback=feedback,
                tail_median_mps=statistics.median(sample[speed_key] for sample in tail) if tail else None,
                tail_mean_mps=statistics.mean(sample[speed_key] for sample in tail) if tail else None,
                tail_std_mps=statistics.pstdev(sample[speed_key] for sample in tail) if tail else None,
                observation_delay_ms=delay)


def summarize_trial(trial: dict) -> dict:
    """Use final four valid observations, never infer displacement or physical stop."""
    eligible = _eligible_samples(trial)
    row = {field: trial.get(field) for field in _RESULT_FIELDS}
    for side in ("left", "right"):
        row.update({f"{side}_{key}": value
                    for key, value in _wheel_metric(trial, eligible, side).items()})
    batteries = [sample["battery_mv"] for sample in eligible
                 if type(sample.get("battery_mv")) is int]
    row["battery_min_mv"] = min(batteries) if batteries else None
    row["battery_max_mv"] = max(batteries) if batteries else None
    row["software_output_observed"] = bool(eligible)
    row["eligible_sample_count"] = len(eligible)
    return row


def _aggregate(rows):
    metrics = {}
    for side in ("left", "right"):
        feedback_counts = {key: sum(row[f"{side}_feedback"] == key for row in rows)
                           for key in _METRIC_NAMES}
        success = sum(row["status"] == "completed" and row[f"{side}_feedback"] == "sustained"
                      for row in rows)
        failure = sum(row["status"] == "completed" and row[f"{side}_feedback"] == "not_sustained"
                      for row in rows)
        metrics[side] = dict(feedback_counts=feedback_counts, success_count=success,
                             failure_count=failure, valid_trial_count=success + failure,
                             insufficient_count=feedback_counts["insufficient"],
                             not_tested_count=feedback_counts["not_tested"],
                             incomplete_count=sum(row["status"] != "completed"
                                                  and row[f"{side}_feedback"] != "not_tested" for row in rows),
                             success_rate=success / (success + failure) if success + failure else None)
    return metrics


class ExperimentJournal:
    """File writes/report rendering happen off the Qt thread. No join is needed."""

    def __init__(self, directory, plan, run_id, stage_id, context, *, queue_capacity=2048):
        if type(queue_capacity) is not int or not 1 <= queue_capacity <= 2048:
            raise ValueError("记录队列上限必须在 1..2048")
        self.directory = Path(directory).resolve()
        self.plan = _json_value(plan)
        self.run_id, self.stage_id = str(run_id), str(stage_id)
        self.context = _json_value(context)
        if not isinstance(self.plan, dict) or not isinstance(self.context, dict):
            raise ValueError("计划与上下文必须是对象")
        self._queue = queue.Queue(maxsize=queue_capacity)
        self._observation_queue = queue.Queue(maxsize=128)
        self._lock = threading.RLock()
        self._failure = None
        self._ready = False
        self._finished = False
        self._finish_payload = None
        self._finish_event = threading.Event()
        self._observation_thread = None
        self._observation_count = 0
        self._observations = []
        self._summary = None
        self._status, self._reason = "recording", ""
        self._artifacts = {key: str(self.directory / filename) for key, filename in (
            ("plan", "plan.json"), ("context", "context.json"), ("events", "events.jsonl"),
            ("observations", "observations.jsonl"), ("results", "results.csv"),
            ("report", "report.md"), ("curve", "speed_curve.svg"), ("summary", "summary.json"))}
        self._thread = threading.Thread(target=self._run, name="car-experiment-journal", daemon=True)
        self._thread.start()

    @property
    def failure(self):
        with self._lock:
            return self._failure

    @property
    def ready(self):
        with self._lock:
            return self._ready and self._failure is None

    @property
    def is_running(self):
        with self._lock:
            observation_running = self._observation_thread is not None
        return self._thread.is_alive() or observation_running

    def snapshot(self):
        with self._lock:
            return _json_value(dict(run_id=self.run_id, stage_id=self.stage_id, status=self._status,
                                    reason=self._reason, ready=self._ready and self._failure is None,
                                    finished=self._finished, failure=self._failure,
                                    artifacts=self._artifacts, summary=self._summary,
                                    observation_count=self._observation_count))

    def _fail(self, message):
        with self._lock:
            if self._failure is None:
                self._failure = str(message)

    def event(self, kind, **fields):
        try:
            if not isinstance(kind, str) or not 1 <= len(kind) <= 80 or not kind.isprintable():
                raise ValueError("事件 kind 无效")
            value = _json_value(dict(fields=fields, kind=kind, utc=_utc(),
                                     monotonic=time.monotonic(), run_id=self.run_id,
                                     stage_id=self.stage_id))
            if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 65536:
                raise ValueError("单个实验事件超过 64 KiB")
            with self._lock:
                if self._finish_event.is_set() or self._finished or self._failure is not None:
                    return False
                self._queue.put_nowait(value)
            return True
        except (queue.Full, ValueError, TypeError) as exc:
            self._fail("实验事件缓冲溢出" if isinstance(exc, queue.Full) else f"实验事件无效：{exc}")
            return False

    def observation(self, trial_id, text, source):
        """Allow later human notes without rewriting the report produced at finish."""
        if (source not in ("human", "assistant") or not isinstance(text, str)
                or not 1 <= len(text) <= 2000 or not text.strip()
                or not isinstance(trial_id, str) or len(trial_id) > 80
                or any(ord(char) < 32 and char not in "\n\t" for char in text)):
            return False
        with self._lock:
            if self._observation_count >= 128:
                return False
            value = dict(kind="observation", run_id=self.run_id, stage_id=self.stage_id,
                         trial_id=trial_id, text=text, source=source, utc=_utc(),
                         monotonic=time.monotonic(), after_report=self._finished)
            try:
                self._observation_queue.put_nowait(value)
            except queue.Full:
                self._fail("人工观察缓冲溢出")
                return False
            self._observation_count += 1
            self._observations.append(_json_value(value))
            if self._observation_thread is None:
                self._observation_thread = threading.Thread(target=self._write_observations,
                                                            name="car-experiment-observations", daemon=True)
                self._observation_thread.start()
        self.event("observation", trial_id=trial_id, text=text, source=source)
        return True

    def finish(self, status, reason, trials, integrity):
        """Request a final report; returns immediately with a pending snapshot."""
        try:
            trials = _json_value(trials)
            if not isinstance(trials, list) or len(trials) > _MAX_TRIALS:
                raise ValueError("最终试次列表超过上限或格式错误")
            if any(not isinstance(t, dict) or not isinstance(t.get("samples", []), list)
                   or len(t.get("samples", [])) > _MAX_SAMPLES for t in trials):
                raise ValueError("单试次样本列表超过 256 或格式错误")
            payload = dict(status=str(status), reason=str(reason), trials=trials,
                           integrity=_json_value(integrity))
        except (ValueError, TypeError) as exc:
            self._fail(f"实验收尾数据无效：{exc}")
            payload = dict(status="failed", reason=str(exc), trials=[], integrity={"complete": False})
        with self._lock:
            if not self._finish_event.is_set() and not self._finished:
                self._finish_payload = payload
                self._status, self._reason = payload["status"], payload["reason"]
                self._finish_event.set()
        return self.snapshot()

    def _write_document(self, key, content):
        with Path(self._artifacts[key]).open("x", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")

    def _write_observations(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with Path(self._artifacts["observations"]).open("a", encoding="utf-8") as handle:
                while True:
                    with self._lock:
                        try:
                            value = self._observation_queue.get_nowait()
                        except queue.Empty:
                            self._observation_thread = None
                            return
                    handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
                    handle.flush()
        except Exception as exc:
            self._fail(f"人工观察写入失败：{exc}")
            with self._lock:
                self._observation_thread = None

    def _run(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._write_document("plan", self.plan)
            self._write_document("context", {**self.context, "run_id": self.run_id,
                                             "stage_id": self.stage_id, "captured_utc": _utc()})
            with Path(self._artifacts["observations"]).open("a", encoding="utf-8"):
                pass
            with Path(self._artifacts["events"]).open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(kind="journal_ready", run_id=self.run_id,
                                             stage_id=self.stage_id, utc=_utc()), ensure_ascii=False) + "\n")
                handle.flush()
                with self._lock:
                    self._ready = True
                while True:
                    try:
                        value = self._queue.get(timeout=0.05)
                    except queue.Empty:
                        if self._finish_event.is_set():
                            break
                        continue
                    handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
                    handle.flush()
                with self._lock:
                    payload = self._finish_payload
                handle.write(json.dumps(dict(kind="finish", run_id=self.run_id, utc=_utc(),
                                             status=payload["status"], reason=payload["reason"],
                                             failure=self.failure), ensure_ascii=False) + "\n")
            self._write_report(payload)
        except Exception as exc:
            self._fail(f"实验记录写入失败：{exc}")
            with self._lock:
                self._status = "failed"
        finally:
            with self._lock:
                self._finished = True

    def _write_report(self, payload):
        trials = payload["trials"]
        rows = [summarize_trial(trial) for trial in trials]
        with Path(self._artifacts["results"]).open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=_RESULT_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict))
                                 else value for key, value in row.items()})
        status_counts = {}
        for row in rows:
            status = str(row["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        summary = dict(run_id=self.run_id, stage_id=self.stage_id, plan_id=self.plan.get("plan_id"),
                       digest=self.plan.get("digest"), status=payload["status"], reason=payload["reason"],
                       trial_count=len(trials), integrity=payload["integrity"], failure=self.failure,
                       status_counts=status_counts, metrics=_aggregate(rows), generated_utc=_utc(),
                       metric="最后4条有效输出STATE中至少3条轮速与受测PWM同号；不足4条为观测不足",
                       success_rate_basis="仅已完成且观测充分的受测轮试次；中止/失败、观测不足和未测试排除",
                       observation_delay="优先相对pwm_started_monotonic，缺失则相对started_monotonic；使用接收时间",
                       later_observations="后补观察见 observations.jsonl，原报告不重写")
        self._write_document("summary", dict(summary, rows=rows))
        with self._lock:
            observations = _json_value(self._observations)
        source = _cell(self.context.get("source", "未声明"))
        if self.context.get("software_demo") is True:
            source_note = "纯软件假设备；轮速与停车均为测试反馈，不是实车证据。"
        elif self.context.get("software_demo") is False:
            source_note = "设备链路记录；实际轮向、机械停稳仍须现场观察。"
        else:
            source_note = "是否为假设备未声明，不能据此认定实车验证。"
        handshake = self.context.get("raw_handshake_covered")
        handshake_note = "已覆盖" if handshake is True else "未覆盖" if handshake is False else "未声明"
        report = ["# 有限开环实验报告", "", f"计划：{self.plan.get('name', '')}",
                  f"运行：{self.run_id}；阶段：{self.stage_id}",
                  f"结果：{payload['status']}；原因：{payload['reason']}", "",
                  f"计划摘要：`{self.plan.get('digest', '')}`", "",
                  "## 数据来源与证据边界", "",
                  f"来源：{source}。{source_note}",
                  f"原始 HELLO/CAPS 握手日志：{handshake_note}。",
                  _cell(self.context.get("handshake_note", "握手覆盖须以原始通信日志核对；能力快照不能替代原始握手。")), "",
                  "MCU 接受由身份匹配的 ACK 表示；软件输出、编码器轮速和人工观察分别列出。",
                  "accepted 不证明已产生输出或实际转动；结果 unknown 不能视为接受。", "",
                  "每轮取末4条有效输出 STATE，至少3条轮速与命令 PWM 同号才记为持续反馈。",
                  "零速度是有效失败观察；少于4条为观测不足。此口径不证明每个控制采样均在转动。",
                  "STATE 为 20 Hz，编码器仅最新 10 ms 增量；反馈分档不能直接解释为振荡。",
                  "成功率只统计已完成且观测充分的受测轮试次；中止、失败、观测不足与未测试从分母排除。",
                  "观测延迟优先相对 PWM 请求时刻，缺失则相对试次开始（含 ARM）；使用电脑接收单调时间，",
                  "包含排队、通信和采样，不是物理启动时间。末4条均值/总体标准差与输出期间电压范围见 results.csv。",
                  "软件输出归零与 STATE 失能不证明机械停稳；编码器字段不累加为里程。", "",
                  "|试次|PWM 左/右|时长 ms|计划结束方式|执行状态|软件输出|左编码器轮速|右编码器轮速|软件停止确认|",
                  "|---|---:|---:|---|---|---|---|---|---|",
                  *[f"|{_cell(row['trial_id'])}|{row['left_pwm']}/{row['right_pwm']}|"
                    f"{row['duration_ms']}|{_stop_mode(row)}|{_cell(row['status'])}|"
                    f"{'已观测非零 PWM' if row['software_output_observed'] else '未观测非零 PWM'}|"
                    f"{_METRIC_NAMES[row['left_feedback']]}|{_METRIC_NAMES[row['right_feedback']]}|"
                    f"{bool(row['stop_confirmed'])}|" for row in rows], "",
                  "## 命令接受与观察", "",
                  "以下观察只包含生成本报告时已受理的文字，并按来源标注；未记录不代表现场已确认。", "",
                  "阶段整体观察：", "",
                  *_observation_lines([note for note in observations if not note.get("trial_id")]), "",
                  "|试次|ARM ACK|PWM ACK|STOP ACK|",
                  "|---|---|---|---|",
                  *[f"|{_cell(row['trial_id'])}|{_ack(row, 'arm')}|{_ack(row, 'pwm')}|{_ack(row, 'stop')}|"
                    for row in rows], "",
                  *_trial_observation_lines(trials, observations),
                  "## 聚合指标", "", "```json", json.dumps(summary["metrics"], ensure_ascii=False, indent=2),
                  "```", "", "## 记录完整性", "", "```json", json.dumps(payload["integrity"], ensure_ascii=False, indent=2),
                  "```", f"实验记录错误：{self.failure or '无'}", "",
                  "原始遥测和通信以同目录 telemetry.csv、communication.log 为准。",
                  "CAPS、实际 RAM 参数和时间快照见 context.json；不由本地产物推定当前固件散列。",
                  "人工与助手观察分开标注来源，见 observations.jsonl；生成后的补充观察不回写本报告。", "",
                  "![有符号轮速观测](speed_curve.svg)", ""]
        with Path(self._artifacts["report"]).open("x", encoding="utf-8") as handle:
            handle.write("\n".join(report))
        with Path(self._artifacts["curve"]).open("x", encoding="utf-8") as handle:
            handle.write(_speed_svg(trials))
        with self._lock:
            self._summary = summary


def _cell(value):
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _stop_mode(row):
    after = row.get("stop_after_ms")
    return "到期失能" if after is None else f"主动 STOP，PWM 请求后 {after} ms"


def _ack(row, command):
    statuses = row.get("ack_results")
    status = statuses.get(command) if isinstance(statuses, dict) else None
    if status is not None:
        return _cell(status)
    if command == "stop" and row.get("stop_after_ms") is None and not row.get("stop_request_id"):
        return "未发送（计划到期）"
    return "未记录"


def _observation_lines(notes):
    result = []
    for source, label in (("human", "人工观察"), ("assistant", "助手观察")):
        values = [_cell(note.get("text", "")) for note in notes if note.get("source") == source]
        result.append(f"{label}：{'；'.join(values) if values else '未记录'}。")
    return result


def _trial_observation_lines(trials, observations):
    result = []
    for trial in trials:
        trial_id = trial.get("trial_id")
        notes = [note for note in observations if note.get("trial_id") == trial_id]
        embedded = trial.get("observations", [])
        notes += [note for note in embedded if isinstance(note, dict)
                  and note.get("source") in ("human", "assistant")]
        unique = {(_cell(note.get("source")), _cell(note.get("text"))): note for note in notes}
        result.extend([f"试次 {_cell(trial_id)}：", "", *_observation_lines(list(unique.values())), ""])
    return result


def _speed_svg(trials):
    """Separate finite trials into panels; gaps are never drawn as continuous motion."""
    width, panel_height = 900, 145
    height = max(1, len(trials)) * panel_height + 42
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
                '<rect width="100%" height="100%" fill="#fff"/>',
                '<text x="20" y="24" font-family="sans-serif" font-size="16">有符号轮速（mm/s）；蓝=左，橙=右；横轴=接收时间 ms</text>']
    if not trials:
        elements.append('<text x="20" y="55" font-family="sans-serif">没有试次样本</text>')
    for index, trial in enumerate(trials):
        top = 42 + index * panel_height
        samples = _eligible_samples(trial)
        origin = trial.get("started_monotonic")
        if not _number(origin):
            origin = samples[0]["received_monotonic"] if samples else 0
        end = max([float(trial.get("duration_ms") or 1),
                   *[(s["received_monotonic"] - origin) * 1000 for s in samples]])
        speeds = [abs(s[key] * 1000) for s in samples for key in
                  ("signed_left_speed_mps", "signed_right_speed_mps") if _number(s.get(key))]
        scale = max([50.0, *speeds])
        title = html.escape(f"{trial.get('trial_id', '')}  PWM {trial.get('left_pwm')}/{trial.get('right_pwm')}  {trial.get('status', '')}")
        elements.extend([f'<text x="20" y="{top + 17}" font-family="sans-serif" font-size="13">{title}</text>',
                         f'<path d="M 70 {top + 38} V {top + 123} H 870" fill="none" stroke="#999"/>',
                         f'<path d="M 70 {top + 80} H 870" stroke="#ddd"/>',
                         f'<text x="10" y="{top + 45}" font-family="sans-serif" font-size="11">{scale:.0f}</text>',
                         f'<text x="10" y="{top + 121}" font-family="sans-serif" font-size="11">-{scale:.0f}</text>',
                         f'<text x="780" y="{top + 138}" font-family="sans-serif" font-size="11">{end:.0f} ms</text>'])
        for key, color in (("signed_left_speed_mps", "#2563eb"), ("signed_right_speed_mps", "#d97706")):
            points = [(70 + 800 * max(0, (s["received_monotonic"] - origin) * 1000) / end,
                       top + 80 - 40 * s[key] * 1000 / scale) for s in samples if _number(s.get(key))]
            if points:
                coordinates = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
                elements.append(f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="2"/>')
                elements.extend(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" fill="{color}"/>' for x, y in points)
    elements.append("</svg>")
    return "\n".join(elements)
