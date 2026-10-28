"""本机分阶段实验；Qt 主线程执行语义动作，MCP 只提交冻结计划。"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import time
import json
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, QTimer, Signal

from .device import DeviceResult
from .experiment_models import build_plan, get_templates, validate_against_caps
from .experiment_recording import ExperimentJournal
from .measurement_suite import check_suite, read_suite, suggest_refinement
from .preferences import PreferencesStore
from .project_protocol import PROJECT_V1, ProjectV1Adapter
from .transport import ConnectionConfig


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _FailedJournal:
    """构造失败仍可查询原始记录位置，不虚报存在的实验报告。"""
    is_running = False
    ready = False

    def __init__(self, run_id, directory, reason):
        self.failure = reason
        self._snapshot = dict(run_id=run_id, status="failed", reason=reason,
                              finished=True, ready=False, failure=reason, summary=None,
                              artifacts=dict(telemetry=str(directory / "telemetry.csv"),
                                             communication=str(directory / "communication.log")))

    def snapshot(self):
        return deepcopy(self._snapshot)

    def observation(self, *args):
        return False


class AutomationQuitGuard(QObject):
    """直接 Quit 也先异步停止与保存；aboutToQuit 的返回值无法延迟退出。"""

    def __init__(self, app, host):
        super().__init__(app)
        self.app, self.host = app, host
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(app.quit)
        app.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.app and event.type() == QEvent.Type.Quit:
            if not self.host.shutdown():
                self.timer.start()
                return True
            self.timer.stop()
        return False


class AutomationService(QObject):
    """一次只有一个阶段；停止是不可恢复的终结操作。"""

    changed = Signal()
    ACK_TIMEOUT = 2.0
    SETTLE_TIMEOUT = 5.0
    QUIET_SECONDS = 1.0
    HEARTBEAT_TIMEOUT = 3.0
    SAMPLE_LIMIT = 256

    def __init__(self, host, project_directory, parent=None, *, clock=time.monotonic,
                 demo=False, journal_factory=ExperimentJournal):
        super().__init__(parent)
        self.host = host
        self.project_directory = Path(project_directory)
        self.clock = clock
        self.demo = demo
        self.journal_factory = journal_factory
        self._plan = None
        self._plan_used = False
        self._run = None
        self._phase = "idle"
        self._deadline = 0.0
        self._quiet_since = None
        self._quiet_last = None
        self._heartbeats = OrderedDict()
        self._history = OrderedDict()
        self._retired_journals = []
        self._trial = None
        self._trial_index = 0
        self._journal = None
        self._read_request = ""
        self._stop_result = None
        self._terminal_status = ""
        self._terminal_reason = ""
        self._closing = False
        self._suite_directories = OrderedDict()
        self._pump_active = False
        host.automation_service = self
        host.sample_received.connect(self._sample)
        host.request_updated.connect(self._request_updated)
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    @property
    def active(self):
        return self._phase not in ("idle", "completed", "aborted", "failed")

    def _touch(self, client_id):
        if not isinstance(client_id, str) or not client_id or len(client_id) > 128:
            raise ValueError("控制连接身份无效")
        self._heartbeats[client_id] = self.clock()
        self._heartbeats.move_to_end(client_id)
        while len(self._heartbeats) > 8:
            self._heartbeats.popitem(last=False)

    def status(self):
        h = self.host
        return dict(active=self.active, phase=self._phase, demo=self.demo,
                    connected=h.connected, connection_status=h.connection_status,
                    connection_id=h.connection_id, protocol=h.capabilities.protocol,
                    capabilities=asdict(h.capabilities), caps=deepcopy(h.project_limits),
                    state=asdict(h.latest) | {"raw_frame": h.latest.raw_frame.hex()}
                    if h.latest else None,
                    encoder_totals=asdict(h.latest_encoder_totals) | {"raw_frame": h.latest_encoder_totals.raw_frame.hex()}
                    if h.latest_encoder_totals else None,
                    encoder_status=h.encoder_status,
                    encoder_calibration=dict(active=h.encoder_totals.baseline is not None,
                        result=deepcopy(h.encoder_totals.result), reason=h.encoder_totals.reason),
                    control_owner=h.control_owner, recording_status=h.recording_status,
                    owner_kind=self._run.get("owner_kind") if self._run else None,
                    frozen_plan=deepcopy(self._plan),
                    run_id=self._run["run_id"] if self._run else None,
                    reason=self._terminal_reason,
                    completed_trials=sum(t["status"] == "completed" for t in self._run["trials"]) if self._run else 0,
                    entered_trials=len(self._run["trials"]) + bool(self._trial) if self._run else 0,
                    trial_count=len(self._run["stage"]["trials"]) if self._run else 0,
                    current_trial_id=self._trial["trial_id"] if self._trial else None)

    def dispatch_local(self, method, params):
        """Native UI uses the same semantics, without borrowing an MCP heartbeat."""
        return self.dispatch(method, params, "local_ui", owner_kind="local_ui")

    def invalidate_plan(self, reason="草稿已编辑"):
        if self.active:
            return False
        self._plan = None
        self._plan_used = False
        self.changed.emit()
        return True

    def dispatch(self, method, params, client_id, *, owner_kind="mcp"):
        """桥的唯一入口；全部在 GUI 线程调用，不返回令牌或任意文件内容。"""
        try:
            if not isinstance(params, dict):
                raise ValueError("参数必须为 JSON 对象")
            if owner_kind == "mcp":
                self._touch(client_id)
            if method == "heartbeat":
                return {"ok": True, "active": self.active}
            if method == "query_encoder_totals":
                return asdict(self.host.encoder_totals.query())
            if method in ("get_status", "get_progress"):
                return self.status()
            if method == "get_communication_health":
                return self.host.communication_health() if hasattr(self.host, "communication_health") else {"available": False}
            if method == "get_templates":
                return {"templates": get_templates(), "draft_only": True}
            if method == "get_results":
                if params.get("suite_id"):
                    directory = self._suite_directory(params["suite_id"])
                    saved = read_suite(directory)
                    if saved is None:
                        raise ValueError("没有该套件索引")
                    return {k: deepcopy(v) for k, v in saved.items() if k != "runs"} | {
                        "runs": [{k: deepcopy(v) for k, v in run.items() if k not in ("slots", "cancelled_slots")}
                                 | {"cancelled_count": len(run.get("cancelled_slots", []))}
                                 for run in saved["runs"][-8:]],
                        "run_count": len(saved["runs"]), "index_path": str(directory / "index.json")}
                run_id = params.get("run_id") or (self._run or {}).get("run_id")
                entry = self._history.get(run_id)
                if not entry:
                    raise ValueError("没有该运行的结果；仅保留最近 8 次运行索引")
                snapshot = entry.snapshot()
                # Entire artifacts remain on disk. A high-cardinality v2 block
                # summary must not make a valid run unqueryable over the bridge.
                if len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 60000:
                    summary = snapshot.get("summary") or {}
                    snapshot["summary"] = {k: deepcopy(summary[k]) for k in (
                        "schema_version", "status", "trial_count", "planned_count", "entered_count",
                        "completed_count", "aborted_count", "status_counts", "metrics", "failure",
                        "independent_verification", "conclusion") if k in summary}
                    snapshot["detail_note"] = "完整时间块、健康及取消槽位汇总见artifacts.summary文件"
                return snapshot
            if method == "suggest_refinement":
                return self._refinement(params)
            if method == "add_observation":
                return self._observation(params)
            if method == "stop":
                return asdict(self.stop(params.get("reason", "MCP STOP")))
            if self._closing:
                raise ValueError("上位机正在关闭")
            if method == "disconnect_device":
                self.host.disconnect_device()
                return {"queued": True, "message": "已请求断开；在途请求可能结果未知"}
            if method == "connect_device":
                if self.demo:
                    config = ConnectionConfig(port="PROJECT_V1_FAKE", protocol=PROJECT_V1)
                else:
                    loaded = PreferencesStore(self.project_directory / "config" / "preferences.json").load()
                    if loaded.warning:
                        raise ValueError(loaded.warning)
                    if loaded.preferences.profile.protocol != PROJECT_V1:
                        raise ValueError("自动化仅支持保存配置中的 PROJECT_V1")
                    config = loaded.preferences.profile.to_connection_config()
                return {"queued": self.host.connect_device(config), "message": "连接只执行 HELLO，不使能"}
            if method == "preview_plan":
                if self.active:
                    raise ValueError("阶段运行期间不能更换计划；请先停止")
                plan = build_plan(params.get("plan"))
                if len(json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 60000:
                    raise ValueError("展开的冻结表超过60KiB；请精简说明文字或拆分实验表后预览")
                # 每次预览创建新批准版本；即使相同内容也不能重放旧 start。
                plan["plan_id"] = uuid4().hex
                self._plan, self._plan_used = deepcopy(plan), False
                h = self.host
                # Explicit profile preparation is the sole MCP capability-query
                # path. Ordinary connection and fixed-PWM previews keep HELLO only.
                preparation = None
                if (any("segments" in t for s in plan["stages"] for t in s["trials"])
                        and not h.capabilities.pwm_profile):
                    preparation = asdict(h.send_command("query_profile_caps"))
                self.changed.emit()
                return deepcopy(plan) | ({"profile_preparation": preparation} if preparation else {})
            if method in ("start_stage", "start_plan"):
                return self._start(params, client_id, owner_kind=owner_kind, whole_plan=method == "start_plan")
            raise ValueError("不支持的自动化方法")
        except (ValueError, TypeError, KeyError) as exc:
            return {"error": str(exc)}

    def _suite_directory(self, suite_id):
        import re
        if not isinstance(suite_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", suite_id):
            raise ValueError("suite_id无效")
        return self.host.data_directory / "suites" / suite_id

    def _refinement(self, params):
        ids = params.get("run_ids") or [(self._run or {}).get("run_id")]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or len(set(ids)) != len(ids):
            raise ValueError("run_ids须为1..8个不同运行身份")
        rows, plan = [], None
        for run_id in ids:
            journal = self._history.get(run_id)
            if journal is None or not journal.snapshot().get("finished") or journal.failure:
                raise ValueError("加密建议只能读取已终结、校核成功的运行")
            path = Path(journal.snapshot()["artifacts"]["summary"])
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("结果超过有界大小")
            saved = json.loads(path.read_text(encoding="utf-8"))
            rows.extend(saved.get("rows", []))
            if plan is not None and plan.get("suite") != journal.plan.get("suite"):
                raise ValueError("不能混用不同套件预算")
            plan = journal.plan
        if not plan or plan.get("schema_version") != 2:
            raise ValueError("旧固定PWM记录不能作为维持边界细扫数据")
        ledger = read_suite(self._suite_directory(plan["suite"]["suite_id"]))
        remaining = ledger["remaining_budget"] if ledger else plan["suite"]["total_entered_budget"]
        return suggest_refinement(rows, side=params.get("side"), direction=params.get("direction"),
                                  suite=plan["suite"], remaining_budget=remaining)

    def _start(self, params, client_id, *, owner_kind="mcp", whole_plan=False):
        h = self.host
        self._retired_journals = [j for j in self._retired_journals if j.is_running]
        if len(self._retired_journals) >= 8:
            raise ValueError("后台观察仍在保存；请等待后再开始新阶段")
        if self.active or h.control_owner or h.encoder_totals.baseline is not None:
            raise ValueError("已有阶段运行；不能重复开始")
        if not self._plan or params.get("plan_id") != self._plan["plan_id"] or params.get("plan_digest") != self._plan["digest"]:
            raise ValueError("计划版本已失效；请重新预览并核对实际任务授权")
        if self._plan_used:
            raise ValueError("该冻结版本已使用；请重新预览")
        approval = params.get("approval_text")
        if params.get("ready_confirmed") is not True or not isinstance(approval, str) or not approval.strip() or len(approval) > 2000:
            raise ValueError("必须先由人确认该实验表、车轮架空与现场准备完成，并记录原话")
        if owner_kind not in ("mcp", "local_ui"):
            raise ValueError("控制来源无效")
        stage = (dict(stage_id="whole_plan", name=self._plan["name"], trials=[
            deepcopy(t) for s in self._plan["stages"] for t in s["trials"]]) if whole_plan else
            next((s for s in self._plan["stages"] if s["stage_id"] == params.get("stage_id")), None))
        if stage is None:
            raise ValueError("阶段不存在")
        if not isinstance(h.adapter, ProjectV1Adapter) or not h.capabilities.open_loop or not h.capabilities.parameter_read:
            raise ValueError("需要 PROJECT_V1 实际 CAPS 声明开环与参数读取能力")
        if not h.project_state_fresh or not h.latest.local_enable or h.latest.armed or h.project_request_pending or h.project_motion_active:
            raise ValueError("需要新鲜 STATE、本地允许、失能且无在途请求或运动")
        if h.recorder.is_running:
            raise ValueError("请先结束现有录制；每个阶段拥有独立录制目录")
        validate_against_caps(self._plan, h.project_limits)
        if any("segments" in t for t in stage["trials"]) and not getattr(h.capabilities, "pwm_profile", False):
            raise ValueError("需要当前会话真实CAPS及PROFILE_CAPS声明有限profile能力")
        suite_directory = None
        if self._plan.get("schema_version") == 2:
            suite_directory = self._suite_directory(self._plan["suite"]["suite_id"])
            check_suite(suite_directory, self._plan["suite"], len(stage["trials"]))
        if not h.start_recording():
            raise ValueError("录制启动失败；没有发送运动命令")
        run_id = uuid4().hex
        self._plan_used = True
        self._run = dict(run_id=run_id, client_id=client_id, stage=deepcopy(stage),
                         owner_kind=owner_kind, whole_plan=whole_plan,
                         suite_directory=str(suite_directory) if suite_directory else None,
                         plan=deepcopy(self._plan), connection_id=h.connection_id,
                         session=h.adapter.session, baseline_dropped=(h.latest.rx_dropped, h.latest.tx_dropped),
                         approval=approval, started_utc=utc_now(), trials=[])
        self._phase = "reading_parameters"
        self._trial = None
        self._trial_index = 0
        self._journal = None
        self._terminal_reason = self._terminal_status = ""
        self._stop_result = None
        h.control_owner = run_id
        result = h.send_command("read_parameters", owner=run_id)
        self._read_request = result.request_id
        self._deadline = self.clock() + self.ACK_TIMEOUT
        if not result.success:
            self.stop("实际 RAM 参数读取未排队：" + result.message)
        self.changed.emit()
        return dict(run_id=run_id, phase=self._phase, directory=str(h.recorder.directory),
                    message="已开始整张有限冻结表" if whole_plan else "已开始本机阶段；下一阶段须使用新冻结版本")

    def _event(self, kind, **fields):
        if self._journal is not None:
            return self._journal.event(kind, utc=utc_now(), monotonic=self.clock(), **fields)
        return False

    def _make_journal(self):
        h, r = self.host, self._run
        context = dict(run_id=r["run_id"], source=h.source, software_demo=self.demo,
                       connection_id=r["connection_id"], session=r["session"],
                       config=asdict(h.config), caps=deepcopy(h.project_limits),
                       capabilities=asdict(h.capabilities), caps_received_utc=h.caps_received_utc,
                       actual_parameters=deepcopy(h.actual_parameters), parameters_received_utc=h.parameters_received_utc,
                       approval_text=r["approval"], approval_source="software_test_fixture" if self.demo else r["owner_kind"] + "_human_claim",
                       approval_scope=dict(plan_id=r["plan"]["plan_id"], digest=r["plan"]["digest"],
                                           stage_ids=[s["stage_id"] for s in r["plan"]["stages"]] if r["whole_plan"] else [r["stage"]["stage_id"]],
                                           planned_trials=len(r["stage"]["trials"]), suite=deepcopy(r["plan"].get("suite"))),
                       schema_version=r["plan"].get("schema_version", 1), owner_kind=r["owner_kind"],
                       suite_directory=r.get("suite_directory"),
                       planned_slots=[{k: t.get(k) for k in ("trial_id", "slot_id", "stage_id", "block_id", "origin_slot")}
                                      for t in r["stage"]["trials"]],
                       communication_health=h.communication_health() if hasattr(h, "communication_health") else {},
                       started_utc=r["started_utc"],
                       raw_handshake_covered=False,
                       handshake_note="录制开始于既有连接之后；CAPS 为有时间标记的快照，不能补造原始 HELLO/CAPS 日志。",
                       physical_observation_note="零反馈持续 1 秒仅为软件继续条件；物理方向与停稳由现场人员观察。")
        self._journal = self.journal_factory(h.recorder.directory, r["plan"], r["run_id"], r["stage"]["stage_id"], context)
        self._register_journal(self._journal)
        self._event("stage_started", stage_id=r["stage"]["stage_id"], approval_text=r["approval"])

    def _register_journal(self, journal):
        self._history[self._run["run_id"]] = journal
        while len(self._history) > 8:
            _, retired = self._history.popitem(last=False)
            if retired.is_running:
                self._retired_journals.append(retired)

    def _sample(self, sample):
        if not self.active or not self._run or self._phase in ("finalizing", "reporting"):
            return
        if self._trial is not None:
            if len(self._trial["samples"]) >= self.SAMPLE_LIMIT:
                self.stop("单次实验样本缓存达到上限，观测不完整")
                return
            row = asdict(sample)
            row["raw_frame"] = sample.raw_frame.hex()
            self._trial["samples"].append(row)
            if ("segments" in self._trial and getattr(sample, "profile_seq", None) == self._trial.get("profile_seq")
                    and getattr(sample, "profile_status", None) == 1):
                segment = getattr(sample, "segment_index", None)
                seen = self._trial.setdefault("observed_segments", [])
                if segment in (0, 1) and segment not in seen:
                    seen.append(segment)
                    if not self._event("segment_observed", trial_id=self._trial["trial_id"],
                        profile_seq=sample.profile_seq, segment_index=segment,
                        planned_start_offset_ms=0 if segment == 0 else self._trial["segments"][0]["duration_ms"],
                        actual_start_tick=sample.profile_start_tick if segment == 0 else sample.segment1_actual_tick,
                        current_device_tick=sample.device_time_ms, pwm_left=sample.pwm_left, pwm_right=sample.pwm_right):
                        self.stop("分段执行证据事件未保存")
                        return
                if not self._trial.get("first_nonzero_output_utc") and (sample.pwm_left or sample.pwm_right):
                    self._trial["first_nonzero_output_utc"] = sample.received_utc
                    self._trial["first_nonzero_output_tick"] = sample.device_time_ms
        if self._phase in ("initial_settle", "trial_settle", "resting"):
            quiet = (sample.connection_id == self._run["connection_id"]
                     and sample.device_session == self._run["session"]
                     and not sample.armed and sample.pwm_left == 0 and sample.pwm_right == 0
                     and sample.signed_left_speed_mps == 0 and sample.signed_right_speed_mps == 0)
            if self._quiet_last is not None and sample.received_monotonic - self._quiet_last >= .5:
                self._quiet_since = None
            if quiet:
                if self._quiet_since is None:
                    self._quiet_since = sample.received_monotonic
            else:
                self._quiet_since = None
            self._quiet_last = sample.received_monotonic

    def _request_updated(self, result):
        if not self.active or not self._trial:
            return
        for key in ("arm", "pwm", "profile", "stop"):
            if result.request_id and result.request_id == self._trial.get(key + "_request_id"):
                self._trial["ack_results"][key] = result.status
                self._event("request_updated", trial_id=self._trial["trial_id"],
                            command=key, request_id=result.request_id, status=result.status, message=result.message)

    def _health_problem(self):
        h, r = self.host, self._run
        if not h.connected or h.connection_id != r["connection_id"]:
            return "连接已关闭或身份变化"
        if not isinstance(h.adapter, ProjectV1Adapter) or h.adapter.session != r["session"]:
            return "设备会话变化"
        s = h.latest
        if s is None or s.device_session != r["session"] or not 0 <= time.monotonic() - s.received_monotonic < .5:
            return "STATE 过期或当前会话无有效数据"
        if not s.local_enable:
            return "设备本地禁止"
        if (s.rx_dropped, s.tx_dropped) != r["baseline_dropped"]:
            return "MCU 数据丢失累计变化"
        if h.recording_incomplete or not h.recorder.is_active:
            return "原始录制失败、溢出或被停止；STATE/通信证据不完整"
        if self._journal is not None and self._journal.failure:
            return "实验记录失败：" + self._journal.failure
        if getattr(h, "io_backlog_exceeded", False):
            return "接收积压超过有界协调预算"
        if r["owner_kind"] == "mcp" and self.clock() - self._heartbeats.get(r["client_id"], -100000) > self.HEARTBEAT_TIMEOUT:
            return "控制心跳超过 3 秒"
        return ""

    def _check_health(self):
        problem = self._health_problem()
        h, sample = self.host, self.host.latest
        now = time.monotonic()
        trace = dict(event="health_check", monotonic=now, run_id=self._run["run_id"],
                     phase=self._phase, problem=problem, state_age_ms=(now - sample.received_monotonic) * 1000 if sample else None,
                     connection_id=h.connection_id, session=sample.device_session if sample else None,
                     frame_seq=sample.frame_seq if sample else None, tick=sample.device_time_ms if sample else None,
                     rx_last_block_id=sample.rx_last_block_id if sample else None,
                     last_batch=deepcopy(h._last_io_batch), recording_incomplete=h.recording_incomplete)
        if problem:
            trace["snapshot"] = {k: v for k, v in h.communication_health().items()
                                 if k not in ("stops", "epoch_history", "counter_reset_history")}
        h.recorder.record_trace(trace)
        return problem

    def _request_state(self, request_id):
        result = self.host.command_results.get(request_id)
        if result and result.status in ("rejected", "unknown"):
            raise ValueError(result.status + "：" + result.message)
        return result and result.status == "accepted"

    def _covered(self, prefix):
        s, t = self.host.latest, self._trial
        return bool(s and t and s.device_session == self._run["session"]
                    and s.device_last_seq >= t.get(prefix + "_seq", 0))

    def _quiet_ready(self):
        return (getattr(self.host, "_recording_rx_started", True)
                and self._quiet_since is not None and self._quiet_last is not None
                and self._quiet_last - self._quiet_since >= self.QUIET_SECONDS)

    def _settle(self, phase):
        self._phase = phase
        self._quiet_since = self._quiet_last = None
        self._deadline = self.clock() + self.SETTLE_TIMEOUT

    def _send(self, command, prefix, values=None):
        if command in ("arm_pwm", "set_pwm", "set_pwm_profile"):
            if (self._journal is None or not self._journal.ready or self._journal.failure
                    or self.host.recording_incomplete or not self.host.recorder.is_active
                    or self.host.recorder.dropped_total or self.host.recorder.failure_message):
                raise ValueError("运动前记录完整性检查失败")
        result = self.host.send_command(command, values, owner=self._run["run_id"])
        if self._trial is not None:
            metadata = self.host.command_metadata.get(result.request_id, {})
            self._trial[prefix + "_request_id"] = result.request_id
            self._trial[prefix + "_seq"] = metadata.get("seq")
            self._trial["ack_results"][prefix] = result.status
            if command == "set_pwm_profile":
                self._trial["deadline_tick"] = metadata.get("values", {}).get("deadline_tick")
            self._event("command_queued", trial_id=self._trial["trial_id"], command=command,
                        request_id=result.request_id, seq=metadata.get("seq"),
                        session=metadata.get("session"), status=result.status, values=deepcopy(metadata.get("values", values)))
        if not result.success:
            raise ValueError(result.message)
        return result

    def _begin_trial(self):
        spec = self._run["stage"]["trials"][self._trial_index]
        self._trial = deepcopy(spec) | dict(connection_id=self._run["connection_id"],
                         session=self._run["session"], started_utc=utc_now(),
                         started_monotonic=self.clock(), status="running", reason="",
                         samples=[], ack_results={}, stop_confirmed=False, observations=[])
        if not self._event("trial_started", trial_id=spec["trial_id"], spec=spec):
            raise ValueError("试次开始事件未保存，不发送 ARM")
        self._phase = "arm_wait"
        self._deadline = self.clock() + self.ACK_TIMEOUT
        self._send("arm_pwm", "arm")

    def _next_trial(self):
        spec = self._run["stage"]["trials"][self._trial_index]
        if self._run["plan"].get("schema_version") == 2:
            stage = next(s for s in self._run["plan"]["stages"] if s["stage_id"] == spec["stage_id"])
            if spec["trial_id"] == stage["trials"][0]["trial_id"] and stage["rest_before_ms"]:
                self._phase = "resting"
                self._quiet_since = self._quiet_last = None
                self._deadline = self.clock() + stage["rest_before_ms"] / 1000
                self._run["rest_started_monotonic"] = self.clock()
                if not self._event("rest_started", block_id=stage["block_id"],
                    planned_ms=stage["rest_before_ms"], device_tick=self.host.latest.device_time_ms):
                    raise ValueError("静置开始事件未保存")
                self.changed.emit()
                return
        self._begin_trial()

    def _end_trial(self):
        self._trial["status"] = "completed"
        self._trial["stop_confirmed"] = True
        self._trial["ended_utc"] = utc_now()
        if not self._event("trial_completed", trial_id=self._trial["trial_id"], stop_confirmed=True,
                           stop_reason=self.host.latest.stop_reason, software_quiet_seconds=self.QUIET_SECONDS):
            raise ValueError("试次完成事件未保存，不开始下一次")
        self._run["trials"].append(self._trial)
        self._trial = None
        self._trial_index += 1
        if self._trial_index >= len(self._run["stage"]["trials"]):
            self._finish_execution("completed", "冻结整表已结束" if self._run["whole_plan"] else "本阶段已结束；后续使用新冻结版本并核对任务授权")
        else:
            self._next_trial()

    def tick(self):
        if not self.active:
            return
        try:
            if hasattr(self.host, "pump_io") and not self._pump_active:
                self._pump_active = True
                try:
                    self.host.pump_io(max_events=256, max_bytes=65536, time_budget_ms=5)
                finally:
                    self._pump_active = False
            self._tick()
        except Exception as exc:
            # Qt 定时器异常不能逃逸后留下继续运行的阶段。
            self.stop("流程中止：" + str(exc))

    def _tick(self):
        h, now = self.host, self.clock()
        if self._phase == "finalizing":
            if h.recorder.is_running:
                return
            integrity = dict(samples=h.recorder.sample_count, logs=h.recorder.log_count,
                             dropped_records=h.recorder.dropped_total, failure=h.recorder.failure_message,
                             recording_incomplete=h.recording_incomplete)
            if hasattr(h, "communication_health"):
                integrity["communication_health"] = h.communication_health()
            if integrity["dropped_records"] or integrity["failure"] or integrity["recording_incomplete"]:
                self._terminal_status = "failed"
                self._terminal_reason += "；原始记录不完整"
            if self._journal is None:
                try:
                    self._make_journal()
                except Exception as exc:
                    self._terminal_status = self._phase = "failed"
                    self._terminal_reason += "；实验记录无法创建：" + str(exc)
                    self._journal = _FailedJournal(self._run["run_id"], h.recorder.directory,
                                                  self._terminal_reason)
                    self._register_journal(self._journal)
                    h.control_owner = None
                    h.add_log(self._terminal_reason, "ERROR", persist=False)
                    self.changed.emit()
                    return
            self._journal.finish(self._terminal_status, self._terminal_reason,
                                 self._run["trials"], integrity)
            self._phase = "reporting"
            return
        if self._phase == "reporting":
            if not self._journal.snapshot().get("finished") and self._journal.is_running:
                return
            snapshot = self._journal.snapshot()
            if snapshot.get("failure"):
                self._terminal_status = "failed"
                self._terminal_reason += "；实验报告保存失败"
            self._phase = self._terminal_status
            h.control_owner = None
            h.add_log(f"自动化阶段 {self._phase}：{self._terminal_reason}")
            self.changed.emit()
            h.changed.emit()
            return
        if self._phase == "stopping":
            s = h.latest
            meta = h.command_metadata.get(self._stop_result.request_id, {}) if self._stop_result else {}
            confirmed = bool(s and s.device_session == self._run["session"]
                             and not s.armed and s.pwm_left == 0 and s.pwm_right == 0
                             and s.device_last_seq >= meta.get("seq", 0)
                             and time.monotonic() - s.received_monotonic < .5)
            accepted = self._stop_result and self._stop_result.request_id in h.command_results and h.command_results[self._stop_result.request_id].status == "accepted"
            if confirmed and accepted or now >= self._deadline or not h.connected:
                self._event("abort_stop_result", software_stop_confirmed=bool(confirmed and accepted),
                            ack_status=h.command_results.get(self._stop_result.request_id).status
                            if self._stop_result and self._stop_result.request_id in h.command_results else "unknown")
                if self._trial:
                    self._trial["stop_confirmed"] = bool(confirmed and accepted)
                self._finish_execution(self._terminal_status, self._terminal_reason)
            return
        problem = self._check_health()
        if problem:
            self.stop(problem)
            return
        if self._phase == "reading_parameters":
            accepted = self._request_state(self._read_request)
            meta = h.command_metadata.get(self._read_request, {})
            if accepted and meta.get("seq") not in h._project_pending and h.actual_parameters is not None:
                self._make_journal()
                self._phase = "journal_ready"
                self._deadline = now + self.ACK_TIMEOUT
            elif now >= self._deadline:
                self.stop("实际 RAM 回读超时；没有发送运动")
        elif self._phase == "journal_ready":
            if self._journal.snapshot().get("ready"):
                self._settle("initial_settle")
            elif now >= self._deadline:
                self.stop("实验记录初始化超时；没有发送运动")
        elif self._phase in ("initial_settle", "trial_settle"):
            if self._quiet_ready():
                if self._phase == "initial_settle":
                    self._next_trial()
                else:
                    self._end_trial()
            elif now >= self._deadline:
                self.stop("5 秒内未满足失能、PWM 归零及连续 1 秒零反馈条件")
        elif self._phase == "resting":
            if h.latest.armed or h.latest.pwm_left or h.latest.pwm_right:
                self.stop("静置任务期间观察到活动输出")
            elif now >= self._deadline and self._quiet_ready():
                if not self._event("rest_completed", actual_ms=(now - self._run["rest_started_monotonic"]) * 1000,
                                   device_tick=h.latest.device_time_ms):
                    raise ValueError("静置完成事件未保存")
                self._begin_trial()
        elif self._phase == "arm_wait":
            if self._request_state(self._trial["arm_request_id"]) and self._covered("arm") and h.latest.armed and h.latest.control_mode == 1:
                self._phase = "pwm_wait"
                self._deadline = now + self.ACK_TIMEOUT
                self._trial["pwm_started_monotonic"] = now
                if "segments" in self._trial:
                    self._trial["profile_started_monotonic"] = now
                    deadline_tick = (h.latest.device_time_ms + self._trial["duration_ms"]) & 0xffffffff
                    self._trial["deadline_tick"] = deadline_tick
                    result = self._send("set_pwm_profile", "profile", dict(segments=deepcopy(self._trial["segments"]),
                                                                             deadline_tick=deadline_tick))
                    self._trial["pwm_request_id"] = result.request_id
                    self._trial["pwm_seq"] = self._trial["profile_seq"]
                else:
                    self._send("set_pwm", "pwm", dict(left=self._trial["left_pwm"],
                               right=self._trial["right_pwm"], duration_ms=self._trial["duration_ms"]))
            elif now >= self._deadline:
                self.stop("ARM 的 ACK/STATE 确认超时")
        elif self._phase == "pwm_wait":
            if self._request_state(self._trial["pwm_request_id"]) and self._covered("pwm"):
                self._phase = "moving"
                self._deadline = self._trial["pwm_started_monotonic"] + self._trial["duration_ms"] / 1000 + self.ACK_TIMEOUT
            elif now >= self._deadline:
                self.stop("PWM 的 ACK/STATE 确认超时")
        elif self._phase == "moving":
            after = self._trial.get("stop_after_ms")
            if self._covered("pwm") and not h.latest.armed and h.latest.pwm_left == 0 and h.latest.pwm_right == 0:
                if after is not None:
                    self.stop("主动 STOP 之前实验已到期，停止验证观测不足")
                elif h.latest.stop_reason != 2:
                    self.stop("实验未按到期原因停止")
                else:
                    self._settle("trial_settle")
            elif after is not None and now >= self._trial["pwm_started_monotonic"] + after / 1000:
                if (not h.latest.armed or h.latest.control_mode != 1
                        or ((self._trial["left_pwm"] or self._trial["right_pwm"])
                            and not (h.latest.pwm_left or h.latest.pwm_right))):
                    self.stop("主动 STOP 时未观察到活动 PWM，停止验证观测不足")
                else:
                    self._phase = "trial_stop_wait"
                    self._deadline = now + self.ACK_TIMEOUT
                    self._send("stop", "stop")
            elif now >= self._deadline:
                self.stop("运动结束 STATE 确认超时")
        elif self._phase == "trial_stop_wait":
            if self._request_state(self._trial["stop_request_id"]) and self._covered("stop") and not h.latest.armed and h.latest.pwm_left == 0 and h.latest.pwm_right == 0 and h.latest.stop_reason == 1:
                self._settle("trial_settle")
            elif now >= self._deadline:
                self.stop("主动 STOP 的 ACK/STATE 确认超时")

    def stop(self, reason="停止阶段"):
        reason = str(reason)[:2000]
        if not self.active:
            return self.host.send_command("stop", owner=self.host.control_owner)
        if self._phase in ("stopping", "finalizing", "reporting"):
            return self._stop_result or DeviceResult(True, "stopped", "已停止后续实验，正在保存记录")
        self._terminal_status, self._terminal_reason = "aborted", reason
        self._phase = "stopping"
        self._deadline = self.clock() + self.ACK_TIMEOUT
        self._event("stage_abort", reason=reason)
        result = self.host.send_command("stop", owner=self._run["run_id"])
        self._stop_result = result
        if self._trial:
            meta = self.host.command_metadata.get(result.request_id, {})
            self._trial["stop_request_id"] = result.request_id
            self._trial["stop_seq"] = meta.get("seq")
            self._trial["ack_results"]["stop"] = result.status
        self.changed.emit()
        self.host.changed.emit()
        return result

    def _finish_execution(self, status, reason):
        self._terminal_status, self._terminal_reason = status, reason
        if self._trial:
            self._trial["status"], self._trial["reason"] = status, reason
            self._trial["ended_utc"] = utc_now()
            self._run["trials"].append(self._trial)
            self._trial = None
        self._event("stage_execution_ended", status=status, reason=reason)
        self._phase = "finalizing"
        self.host.stop_recording(owner=self._run["run_id"])
        self.changed.emit()

    def _observation(self, params):
        run_id, trial_id = params.get("run_id"), params.get("trial_id", "")
        journal = self._history.get(run_id)
        if journal is None:
            raise ValueError("运行不存在或实验记录尚未初始化")
        plan = self._run["stage"] if self._run and self._run["run_id"] == run_id else None
        if plan is None and hasattr(journal, "plan"):
            plan = next((s for s in journal.plan["stages"] if s["stage_id"] == journal.stage_id), None)
            if journal.stage_id == "whole_plan":
                plan = {"trials": [t for s in journal.plan["stages"] for t in s["trials"]]}
        if plan is not None and trial_id and trial_id not in {t["trial_id"] for t in plan["trials"]}:
            raise ValueError("试次不存在")
        text, source = params.get("text"), params.get("source", "human")
        if not journal.observation(trial_id, text, source):
            raise ValueError("观察记录未接受；请检查长度、数量与记录状态")
        return {"queued": True, "message": "观察已排队；后补观察保存在 observations.jsonl"}

    def client_lost(self, client_id):
        self._heartbeats.pop(client_id, None)
        if self.active and self._run["owner_kind"] == "mcp" and self._run["client_id"] == client_id:
            self.stop("控制连接已消失")

    def shutdown(self):
        self._closing = True
        if self.active:
            self.stop("关闭上位机")
            self.tick()
            return False
        if any(j.is_running for j in (*self._history.values(), *self._retired_journals)):
            return False
        self.timer.stop()
        return True
