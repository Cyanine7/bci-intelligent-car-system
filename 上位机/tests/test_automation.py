"""Software-only PROJECT_V1 automation validation; no hardware factories."""

import csv
from dataclasses import replace
import json
from pathlib import Path
import struct
import time

import pytest

from car_host.automation import AutomationService, _FailedJournal
from car_host.controller import HostController
from car_host.project_fake import FAKE_CONFIG, FAKE_SOURCE, ProjectFakeWorker, create_project_fake_worker
from car_host.project_protocol import ACK, ARM, CAPS, GET_PARAMS, HELLO, PARAMS, PROJECT_V1, SET_PWM, STOP, ProjectV1Adapter
from car_host.transport import ConnectionConfig


class FakeClock:
    def __init__(self):
        self.now = time.monotonic()

    def __call__(self):
        return self.now

    def advance(self, seconds=.05):
        self.now += seconds


@pytest.fixture
def fake_host(qapp, tmp_path, monkeypatch):
    clock = FakeClock()
    # Host freshness and request expiration use the same deterministic clock.
    monkeypatch.setattr(time, "monotonic", clock)
    host = HostController(tmp_path / "data", worker_factory=lambda config: ProjectFakeWorker(config, clock=clock))
    host.timer.stop()
    assert host.connect_device(FAKE_CONFIG)
    host.poll()
    host.poll()
    clock.advance()
    host.poll()
    assert host.capabilities.open_loop and host.project_state_fresh
    yield host, clock
    if host.automation_service is not None and host.automation_service.active:
        host.automation_service.stop("测试清理")
    deadline = time.perf_counter() + 2
    while not host.shutdown() and time.perf_counter() < deadline:
        clock.advance()
        qapp.processEvents()
        time.sleep(.001)
    assert host.shutdown()


def command_kinds(worker):
    return [payload[3] for payload, _ in worker.sent]


def result_rows(result):
    return json.loads(Path(result["artifacts"]["summary"]).read_text(encoding="utf-8"))["rows"]


def pump(host, clock, service=None, seconds=.05):
    clock.advance(seconds)
    host.poll()
    if service is not None:
        service.tick()


@pytest.fixture
def automation(fake_host, tmp_path):
    host, clock = fake_host
    service = AutomationService(host, tmp_path, clock=clock, demo=True)
    service.timer.stop()
    yield host, clock, service
    service.timer.stop()


def draft(*, repetitions=2, duration_ms=500, stop_after_ms=None):
    return {"name": "纯软件台架测试", "stages": [{"stage_id": "stage1", "name": "有限开环",
            "steps": [{"left_pwm": 1200, "right_pwm": -1500, "duration_ms": duration_ms,
                       "repetitions": repetitions, "stop_after_ms": stop_after_ms}]}]}


def preview_and_start(service, plan=None, *, client_id="test-client"):
    preview = service.dispatch("preview_plan", {"plan": plan or draft()}, client_id)
    assert "error" not in preview, preview
    params = {"plan_id": preview["plan_id"], "plan_digest": preview["digest"],
              "stage_id": "stage1", "approval_text": "我确认该版本；已架空、现场准备完成（软件测试）",
              "ready_confirmed": True}
    started = service.dispatch("start_stage", params, client_id)
    assert "run_id" in started, started
    return preview, params, started


def drive(automation, predicate, *, heartbeat=True, timeout=4):
    host, clock, service = automation
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return
        if heartbeat:
            service.dispatch("heartbeat", {}, "test-client")
        # File workers need wall time, rather than a rush through fake timeout.
        if service.status()["phase"] in ("journal_ready", "finalizing", "reporting"):
            host.poll()
            service.tick()
            time.sleep(.002)
        else:
            pump(host, clock, service)
    assert predicate(), service.status()


def test_project_fake_factory_only_accepts_software_sentinel():
    worker = create_project_fake_worker(FAKE_CONFIG)
    assert worker.source == FAKE_SOURCE
    for config in (ConnectionConfig(port="COM1", protocol=PROJECT_V1),
                   ConnectionConfig(kind="bluetooth_spp", bluetooth_address="2A:A2:19:07:1B:8B", protocol=PROJECT_V1),
                   replace(FAKE_CONFIG, protocol="LEGACY_APP")):
        with pytest.raises(ValueError, match="不会访问硬件"):
            create_project_fake_worker(config)


def test_fake_protocol_caps_and_control_tick_are_independent_of_state_rate(fake_host):
    host, clock = fake_host
    worker = host.worker
    assert host.source == FAKE_SOURCE and host.latest.source == FAKE_SOURCE
    assert command_kinds(worker) == [HELLO]
    assert host.latest.device_time_ms == 50 and not host.latest.armed
    arm = host.motion.arm_pwm()
    assert arm.success
    pump(host, clock)
    assert host.command_results[arm.request_id].status == "accepted"
    assert host.latest.armed and host.latest.device_last_seq == host.adapter.seq
    move = host.motion.set_pwm(1200, -1500, 130)
    assert move.success
    pump(host, clock)
    assert host.command_results[move.request_id].status == "accepted"
    assert host.latest.pwm_left == 1200 and host.latest.signed_right_speed_mps == -.15
    pump(host, clock, seconds=.08)
    assert not worker.armed and worker.tick_ms == 230  # 100 Hz finite expiry.
    pump(host, clock, seconds=.02)
    assert host.latest.device_time_ms == 250 and not host.latest.armed
    assert host.latest.stop_reason == 2 and host.latest.pwm_left == 0


def test_fake_parameter_readback_does_not_modify_ram(fake_host):
    host, clock = fake_host
    result = host.parameters.read_parameters()
    pump(host, clock)
    assert host.command_results[result.request_id].status == "accepted"
    assert host.actual_parameters["revision"] == 0
    assert host.actual_parameters["kp_left_q100"] == 0
    assert command_kinds(host.worker) == [HELLO, GET_PARAMS]


def test_fake_faults_do_not_claim_physical_feedback(fake_host):
    host, clock = fake_host
    worker = host.worker
    worker.set_fault("start_failure")
    assert host.motion.arm_pwm().success
    pump(host, clock)
    assert host.motion.set_pwm(1200, 1300, 500).success
    pump(host, clock)
    assert host.latest.pwm_left == 1200 and host.latest.signed_left_speed_mps == 0
    worker.set_fault("disable")
    pump(host, clock)
    assert not host.latest.local_enable and not host.latest.armed and host.latest.stop_reason == 3


def test_fake_disconnect_remains_terminal_and_never_reconnects(fake_host):
    host, clock = fake_host
    worker = host.worker
    worker.set_fault("disconnect")
    pump(host, clock)
    pump(host, clock)
    assert host.worker is None and not host.connected
    assert command_kinds(worker) == [HELLO]


def test_fake_storage_is_bounded_and_reports_event_loss():
    clock = FakeClock()
    worker = ProjectFakeWorker(FAKE_CONFIG, clock=clock)
    worker.start()
    adapter = ProjectV1Adapter(FAKE_SOURCE)
    assert worker.request_send(adapter.begin_session())
    for _ in range(20):
        clock.advance(10)
        worker.advance()
    assert len(worker._events) <= worker.EVENT_QUEUE_CAPACITY
    assert worker.take_overflow_count() > 0
    assert worker.take_overflow_count() == 0
    worker.request_stop()


def test_fake_reject_ack_and_missing_ack_are_injectable_separately(fake_host):
    host, clock = fake_host
    worker = host.worker
    worker.set_fault("reject", ARM)
    rejected = host.motion.arm_pwm()
    pump(host, clock)
    assert host.command_results[rejected.request_id].status == "rejected"
    assert not host.latest.armed
    worker.set_fault("reject", False)
    worker.set_fault("missingack", ARM)
    unknown = host.motion.arm_pwm()
    pump(host, clock)
    assert host.latest.armed and host.command_results[unknown.request_id].status == "queued"
    pump(host, clock, seconds=2.1)
    assert host.command_results[unknown.request_id].status == "unknown"
    assert command_kinds(worker).count(ARM) == 2  # No retries after unknown result.


def test_fake_stale_state_and_device_drop_faults_are_reported(fake_host):
    host, clock = fake_host
    worker = host.worker
    before = host.frames_received
    worker.set_fault("stale")
    pump(host, clock, seconds=.6)
    assert host.frames_received == before and not host.project_state_fresh
    worker.set_fault("stale", False)
    worker.set_fault("dropped", 1)
    pump(host, clock)
    assert host.latest.rx_dropped == 1
    assert any("缓存丢失" in entry.message for entry in host.logs)


def test_stage_batch_is_finite_recorded_and_traceable(automation):
    host, clock, service = automation
    worker = host.worker
    preview, _, started = preview_and_start(service)
    assert len(preview["stages"][0]["trials"]) == 2
    assert host.control_owner == started["run_id"] and host.recorder.is_active
    assert command_kinds(worker) == [HELLO, GET_PARAMS]  # No immediate ARM.
    drive(automation, lambda: not service.active)
    assert service.status()["phase"] == "completed"
    assert service.status()["completed_trials"] == 2 and host.control_owner is None
    assert command_kinds(worker) == [HELLO, GET_PARAMS, ARM, SET_PWM, ARM, SET_PWM]
    result = service.dispatch("get_results", {"run_id": started["run_id"]}, "test-client")
    assert result["finished"] and result["summary"]["integrity"]["dropped_records"] == 0
    directory = host.recorder.directory
    with (directory / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as handle:
        telemetry = list(csv.DictReader(handle))
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and {row["status"] for row in rows} == {"completed"}
    assert {row["left_feedback"] for row in rows} == {"sustained"}
    assert {row["right_feedback"] for row in rows} == {"sustained"}
    for row in rows:
        assert row["connection_id"] == host.connection_id
        assert int(row["session"]) == host.adapter.session
        assert any(int(t["device_last_seq"]) == int(row["pwm_seq"]) for t in telemetry)
        assert any(event["fields"].get("request_id") == row["pwm_request_id"] for event in events if "fields" in event)
    assert all(event["run_id"] == started["run_id"] for event in events)
    context = json.loads((directory / "context.json").read_text(encoding="utf-8"))
    assert context["software_demo"] is True and context["raw_handshake_covered"] is False
    assert context["actual_parameters"]["revision"] == 0 and context["caps_received_utc"]
    assert context["parameters_received_utc"]
    assert (directory / "speed_curve.svg").is_file()
    assert "编码器字段不累加为里程" in (directory / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("duration,expected", [(500, "not_sustained"), (50, "insufficient")])
def test_start_failure_is_valid_result_but_insufficient_observation_is_separate(automation, duration, expected):
    host, _, service = automation
    host.worker.set_fault("start_failure")
    _, _, started = preview_and_start(service, draft(repetitions=1, duration_ms=duration))
    drive(automation, lambda: not service.active)
    result = service.dispatch("get_results", {"run_id": started["run_id"]}, "test-client")
    row = result_rows(result)[0]
    assert row["status"] == "completed"
    assert row["left_feedback"] == expected and row["right_feedback"] == expected


def test_active_stop_is_sent_once_then_waits_before_next_trial(automation):
    host, _, service = automation
    worker = host.worker
    preview_and_start(service, draft(repetitions=2, duration_ms=800, stop_after_ms=300))
    drive(automation, lambda: not service.active)
    assert service.status()["phase"] == "completed"
    assert command_kinds(worker) == [HELLO, GET_PARAMS, ARM, SET_PWM, STOP, ARM, SET_PWM, STOP]
    assert all(row["stop_confirmed"] and row["ack_results"]["stop"] == "accepted"
               for row in result_rows(service.dispatch("get_results", {}, "test-client")))


def test_approval_version_digest_readiness_and_duplicate_start_are_enforced(automation):
    host, _, service = automation
    preview = service.dispatch("preview_plan", {"plan": draft()}, "test-client")
    base = {"plan_id": preview["plan_id"], "plan_digest": preview["digest"], "stage_id": "stage1",
            "approval_text": "已架空、现场准备完成", "ready_confirmed": True}
    for params in (base | {"approval_text": ""}, base | {"ready_confirmed": False},
                   base | {"plan_digest": "changed"}, base | {"stage_id": "absent"}):
        assert "error" in service.dispatch("start_stage", params, "test-client")
    assert command_kinds(host.worker) == [HELLO]
    newer = service.dispatch("preview_plan", {"plan": draft(repetitions=1)}, "test-client")
    assert newer["plan_id"] != preview["plan_id"]
    assert "error" in service.dispatch("start_stage", base, "test-client")
    params = base | {"plan_id": newer["plan_id"], "plan_digest": newer["digest"]}
    assert "run_id" in service.dispatch("start_stage", params, "test-client")
    assert "error" in service.dispatch("start_stage", params, "test-client")
    drive(automation, lambda: not service.active)
    assert "error" in service.dispatch("start_stage", params, "test-client")


def test_manual_commands_are_excluded_and_manual_stop_cancels_remaining_trials(automation):
    host, _, service = automation
    worker = host.worker
    preview_and_start(service, draft(repetitions=3))
    assert host.motion.arm_pwm().status == "busy"
    assert host.motion.set_pwm(2000, 0, 200).status == "busy"
    assert host.parameters.apply_parameters({"kp_left_q100": 1, "ki_left_q100": 0,
                                            "kp_right_q100": 0, "ki_right_q100": 0}).status == "busy"
    drive(automation, lambda: service.status()["phase"] == "moving")
    result = host.motion.stop()
    assert result.success
    assert service.status()["phase"] == "stopping"
    arm_pwm_count = sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker))
    drive(automation, lambda: not service.active)
    for _ in range(20):
        pump(host, automation[1], service)
    assert service.status()["phase"] == "aborted"
    assert sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker)) == arm_pwm_count
    assert command_kinds(worker).count(STOP) == 1


@pytest.mark.parametrize("fault,value,reason", [
    ("reject", ARM, "rejected"),
    ("missingack", ARM, "unknown"),
    ("ack_delay_ms", 2300, "unknown"),
    ("stale", True, "STATE"),
    ("disable", True, "本地禁止"),
    ("disconnect", True, "连接"),
    ("dropped", 1, "数据丢失"),
    ("session_change", True, "STATE"),
])
def test_faults_abort_stage_without_further_motion_or_retries(automation, fault, value, reason):
    host, clock, service = automation
    worker = host.worker
    preview_and_start(service, draft(repetitions=3))
    drive(automation, lambda: service.status()["phase"] == "initial_settle")
    worker.set_fault(fault, value)
    drive(automation, lambda: not service.active)
    status = service.status()
    assert status["phase"] in ("aborted", "failed")
    assert reason in status["reason"], status
    kinds = command_kinds(worker)
    assert kinds.count(ARM) <= 1 and kinds.count(SET_PWM) == 0
    before = len(kinds)
    for _ in range(60):
        service.dispatch("heartbeat", {}, "test-client")
        pump(host, clock, service)
    assert len(command_kinds(worker)) == before


def test_session_identity_change_aborts_without_using_the_old_session(automation):
    host, _, service = automation
    worker = host.worker
    preview_and_start(service)
    drive(automation, lambda: service.status()["phase"] == "initial_settle")
    host.adapter.session += 1
    drive(automation, lambda: not service.active)
    assert "会话变化" in service.status()["reason"]
    assert not any(kind in (ARM, SET_PWM) for kind in command_kinds(worker))


def test_settle_requires_one_second_zero_feedback_and_fails_after_five(automation):
    host, _, service = automation
    worker = host.worker
    worker.set_fault("residual_feedback")
    preview_and_start(service)
    drive(automation, lambda: not service.active)
    assert "5 秒内" in service.status()["reason"]
    assert not any(kind in (ARM, SET_PWM) for kind in command_kinds(worker))


@pytest.mark.parametrize("failure", ["notification", "queue_loss", "event_loss", "journal_failure"])
def test_recording_failures_abort_and_never_publish_complete_report(automation, failure):
    host, _, service = automation
    worker = host.worker
    preview_and_start(service)
    drive(automation, lambda: service.status()["phase"] == "initial_settle")
    if failure == "notification":
        host.recorder._error = host.recorder.failure_message = "注入磁盘写入失败"
    elif failure == "queue_loss":
        host.recorder._dropped = host.recorder.dropped_total = 1
    elif failure == "event_loss":
        worker._overflow += 1
    else:
        service._journal._fail("注入实验记录写入失败")
    drive(automation, lambda: not service.active)
    assert service.status()["phase"] in ("aborted", "failed")
    assert not any(kind in (ARM, SET_PWM) for kind in command_kinds(worker))
    result = service.dispatch("get_results", {}, "test-client")
    assert result["status"] != "completed" or result["failure"]


def test_recording_start_error_prevents_get_params_arm_and_pwm(automation, monkeypatch):
    host, _, service = automation
    monkeypatch.setattr(host.recorder, "start", lambda *_: (_ for _ in ()).throw(OSError("磁盘不可写")))
    preview = service.dispatch("preview_plan", {"plan": draft()}, "test-client")
    result = service.dispatch("start_stage", {"plan_id": preview["plan_id"], "plan_digest": preview["digest"],
                  "stage_id": "stage1", "approval_text": "已架空、现场准备完成", "ready_confirmed": True}, "test-client")
    assert "录制启动失败" in result["error"]
    assert not service.active and host.control_owner is None
    assert command_kinds(host.worker) == [HELLO]


@pytest.mark.parametrize("loss", ["heartbeat", "connection"])
def test_control_heartbeat_or_client_loss_cancels_stage(automation, loss):
    host, clock, service = automation
    worker = host.worker
    preview_and_start(service, draft(repetitions=3))
    drive(automation, lambda: service.status()["phase"] == "moving")
    before = sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker))
    if loss == "connection":
        service.client_lost("test-client")
        assert service.status()["phase"] == "stopping"
    else:
        pump(host, clock, service, seconds=3.1)
    drive(automation, lambda: not service.active, heartbeat=False)
    assert service.status()["phase"] == "aborted"
    assert ("心跳" if loss == "heartbeat" else "控制连接") in service.status()["reason"]
    assert sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker)) == before


@pytest.mark.parametrize("fault,value", [("reject", SET_PWM), ("missingack", SET_PWM), ("ack_delay_ms", 2300)])
def test_pwm_ack_failure_never_launches_another_trial(automation, fault, value):
    host, _, service = automation
    worker = host.worker
    preview_and_start(service, draft(repetitions=3))
    drive(automation, lambda: service.status()["phase"] == "arm_wait")
    worker.set_fault(fault, value)
    drive(automation, lambda: not service.active)
    assert service.status()["phase"] in ("aborted", "failed")
    assert command_kinds(worker).count(ARM) == 1
    assert command_kinds(worker).count(SET_PWM) == 1
    assert command_kinds(worker).count(STOP) == 1


def test_observations_keep_human_and_assistant_sources_and_trial_identity(automation):
    host, _, service = automation
    preview, _, started = preview_and_start(service, draft(repetitions=1))
    drive(automation, lambda: service.status()["phase"] == "moving")
    trial_id = preview["stages"][0]["trials"][0]["trial_id"]
    for source, text in (("human", "架空观察：左轮朝前；软件夹具，无物理验收"),
                         ("assistant", "收到有符号编码器反馈，仅为软件观测")):
        assert service.dispatch("add_observation", {"run_id": started["run_id"], "trial_id": trial_id,
                                "source": source, "text": text}, "test-client")["queued"]
    assert "error" in service.dispatch("add_observation", {"run_id": started["run_id"], "trial_id": "unknown",
                                                        "text": "invalid"}, "test-client")
    drive(automation, lambda: not service.active)
    result = service.dispatch("get_results", {}, "test-client")
    deadline = time.perf_counter() + 2
    while service._journal.is_running and time.perf_counter() < deadline:
        time.sleep(.002)
    notes = [json.loads(line) for line in Path(result["artifacts"]["observations"]).read_text(encoding="utf-8").splitlines()]
    assert {note["source"] for note in notes} == {"human", "assistant"}
    assert {note["trial_id"] for note in notes} == {trial_id}
    assert {note["run_id"] for note in notes} == {started["run_id"]}


def test_active_stop_cannot_succeed_when_pwm_ack_arrives_after_finite_expiry(automation):
    host, _, service = automation
    worker = host.worker
    preview_and_start(service, draft(repetitions=2, duration_ms=500, stop_after_ms=300))
    drive(automation, lambda: service.status()["phase"] == "arm_wait")
    # ARM ACK is already queued; only the following PWM ACK is delayed.
    worker.set_fault("ack_delay_ms", 650)
    drive(automation, lambda: not service.active)
    assert service.status()["phase"] == "aborted"
    assert "主动 STOP" in service.status()["reason"]
    assert "到期" in service.status()["reason"]
    assert command_kinds(worker).count(ARM) == 1 and command_kinds(worker).count(SET_PWM) == 1
    row = result_rows(service.dispatch("get_results", {}, "test-client"))[0]
    assert row["status"] != "completed"


def test_persistent_journal_constructor_error_finishes_failed_and_releases_control(automation):
    host, _, service = automation
    worker = host.worker
    calls = []

    def fail_journal(*args, **kwargs):
        calls.append(True)
        raise OSError("注入持久记录目录故障")

    service.journal_factory = fail_journal
    _, _, started = preview_and_start(service)
    drive(automation, lambda: not service.active)
    assert service.status()["phase"] == "failed" and host.control_owner is None
    assert len(calls) == 2
    assert not host.recorder.is_running
    assert not any(kind in (ARM, SET_PWM) for kind in command_kinds(worker))
    result = service.dispatch("get_results", {"run_id": started["run_id"]}, "test-client")
    assert result["finished"] and result["failure"]
    assert "report" not in result["artifacts"]
    assert Path(result["artifacts"]["telemetry"]).is_file()
    assert "INTEGRITY" in Path(result["artifacts"]["communication"]).read_text(encoding="utf-8")
    observation = service.dispatch("add_observation", {"run_id": started["run_id"], "text": "记录构造失败后的补充观察"}, "test-client")
    assert "error" in observation and "观察记录未接受" in observation["error"]
    assert host.shutdown()


@pytest.mark.parametrize("failed_event,phase,expected_motions", [
    ("trial_started", "initial_settle", 0),
    ("trial_completed", "trial_settle", 2),
])
def test_trial_event_queue_failure_prevents_arm_in_the_same_tick(automation, monkeypatch, failed_event, phase, expected_motions):
    host, _, service = automation
    worker = host.worker
    preview_and_start(service, draft(repetitions=3))
    drive(automation, lambda: service.status()["phase"] == phase)
    journal = service._journal
    original_event = journal.event
    failures = []

    def fail_event(kind, **fields):
        if kind == failed_event:
            failures.append(True)
            journal._fail("注入实验事件瞬时 queue full")
            return False
        return original_event(kind, **fields)

    monkeypatch.setattr(journal, "event", fail_event)
    drive(automation, lambda: not service.active)
    assert failures == [True]
    assert service.status()["phase"] == "failed"
    assert sum(kind in (ARM, SET_PWM) for kind in command_kinds(worker)) == expected_motions
    assert command_kinds(worker).count(STOP) == 1
    assert "INTEGRITY" in (host.recorder.directory / "communication.log").read_text(encoding="utf-8")


def test_failed_journal_history_is_bounded_and_evicted_results_are_unavailable(automation, tmp_path):
    _, _, service = automation
    for index in range(10):
        run_id = f"failed-{index}"
        service._run = {"run_id": run_id}
        service._register_journal(_FailedJournal(run_id, tmp_path, "注入持久构造失败"))
    assert len(service._history) == 8 and list(service._history) == [f"failed-{index}" for index in range(2, 10)]
    assert not service._retired_journals
    assert "error" in service.dispatch("get_results", {"run_id": "failed-0"}, "test-client")
    result = service.dispatch("get_results", {"run_id": "failed-9"}, "test-client")
    assert result["status"] == "failed" and result["finished"]
    assert "error" in service.dispatch("add_observation", {"run_id": "failed-9", "text": "补充观察"}, "test-client")
