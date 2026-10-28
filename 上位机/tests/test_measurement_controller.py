"""Controller capability negotiation, ordered receive pumping and trace evidence."""

from dataclasses import replace
import json
import time

import pytest

from car_host.controller import HostController
from car_host.project_protocol import (ARM, GET_PROFILE_CAPS, PROFILE_CAPS, PROFILE_CAPS_LAYOUT,
    PROFILE_COMMAND_LAYOUT, SET_PWM_PROFILE, STATE, STATE_LAYOUT, STOP, encode_frame)
from car_host.transport import ConnectionConfig
from test_application_extensions import Link, spin
from test_project_v1 import project_host, handshake, reply, ack, update_state, state_frame


def negotiate(host):
    result = host.motion.query_profile_caps()
    assert result.success
    reply(host, PROFILE_CAPS, PROFILE_CAPS_LAYOUT.pack(3, 1, 2, 6000, 1000, 100, 20))
    assert not host.capabilities.pwm_profile
    ack(host, GET_PROFILE_CAPS)
    assert host.capabilities.pwm_profile
    return result


def test_profile_is_explicit_and_both_caps_and_ack_are_required(project_host):
    host = project_host
    handshake(host)
    assert len(host.worker.sent) == 1  # Startup is still HELLO only.
    assert not host.capabilities.pwm_profile
    result = host.motion.query_profile_caps()
    ack(host, GET_PROFILE_CAPS)
    assert not host.capabilities.pwm_profile
    reply(host, PROFILE_CAPS, PROFILE_CAPS_LAYOUT.pack(3, 1, 2, 6000, 1000, 100, 20))
    assert host.capabilities.pwm_profile
    assert host.command_results[result.request_id].status == "accepted"


def test_old_firmware_keeps_fixed_pwm_when_query_is_unknown(project_host):
    host = project_host
    handshake(host)
    assert host.motion.query_profile_caps().success
    ack(host, GET_PROFILE_CAPS, status=9)
    assert not host.capabilities.pwm_profile and host.capabilities.open_loop
    before = len(host.worker.sent)
    segments = [dict(left_pwm=1400, right_pwm=0, duration_ms=400),
                dict(left_pwm=900, right_pwm=0, duration_ms=400)]
    assert not host.motion.set_pwm_profile(segments).success
    assert len(host.worker.sent) == before
    assert host.motion.arm_pwm().success


def test_profile_uses_actual_caps_and_cannot_renew_motion(project_host):
    host = project_host
    handshake(host)
    negotiate(host)
    assert host.motion.arm_pwm().success
    ack(host, ARM)
    update_state(host, tick=1050, mode=1, armed=1)
    segments = [dict(left_pwm=1400, right_pwm=0, duration_ms=400),
                dict(left_pwm=900, right_pwm=0, duration_ms=600)]
    before = len(host.worker.sent)
    assert not host.motion.set_pwm_profile(segments).success  # Base CAPS says 800ms.
    assert len(host.worker.sent) == before
    segments[1]["duration_ms"] = 300
    result = host.motion.set_pwm_profile(segments)
    assert result.success and host.project_motion_active
    wire = host.worker.sent[-1][0]
    assert wire[3] == SET_PWM_PROFILE
    fields = PROFILE_COMMAND_LAYOUT.unpack(wire[14:-2])
    assert fields[3:9] == (1400, 0, 900, 0, 400, 700)
    assert host.command_metadata[result.request_id]["values"]["deadline_tick"] == fields[-1]
    ack(host, SET_PWM_PROFILE)
    update_state(host, tick=1100, mode=1, armed=1)
    before = len(host.worker.sent)
    assert not host.motion.set_pwm_profile(segments).success
    assert not host.motion.set_pwm(1400, 0, 100).success
    assert len(host.worker.sent) == before


def test_receive_pump_reads_already_available_input_before_freshness(qapp, tmp_path):
    class AvailableLink(Link):
        def __init__(self, config):
            super().__init__(config)
            self.available = []
        def pull_available_input(self, max_bytes=65536):
            for event in self.available:
                self.events.append(event)
            self.available.clear()
            return dict(bytes_read=0, remaining_bytes=0, budget_exceeded=False)
    host = HostController(tmp_path, worker_factory=AvailableLink)
    host.timer.stop()
    assert host.connect_device(ConnectionConfig(port="NO_HARDWARE", protocol="PROJECT_V1"))
    host.poll()
    handshake(host)
    host.latest = replace(host.latest, received_monotonic=time.monotonic() - .6)
    assert not host.project_state_fresh
    link = host.worker
    link.emit("rx", state_frame(host.adapter, tick=1050))
    link.available.append(link.events.pop())
    host.pump_io()
    assert host.project_state_fresh and not host.io_backlog_exceeded
    # Processing cannot make an actually old next frame fresh.
    link.emit("rx", state_frame(host.adapter, tick=1100))
    link.events[-1] = replace(link.events[-1], monotonic=time.monotonic() - .6)
    host.pump_io()
    assert not host.project_state_fresh
    host.disconnect_device()
    spin(qapp, host.shutdown)


def test_backlog_is_bounded_and_trace_is_saved(project_host, qapp):
    host = project_host
    handshake(host)
    assert host.start_recording()
    for _ in range(5):
        host.worker.emit("status", message="bounded batch")
    batch = host.pump_io(max_events=2, time_budget_ms=100)
    assert batch["event_count"] == 2 and host.io_backlog_exceeded
    assert not host.motion.arm_pwm().success
    host.pump_io(max_events=256, time_budget_ms=100)
    assert not host.io_backlog_exceeded
    host.stop_recording()
    spin(qapp, lambda: not host.recorder.is_running)
    traces = [json.loads(line) for line in (host.recorder.directory / "receive_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(t.get("kind") == "controller_batch" and t["budget_exceeded"] for t in traces)
    assert all(t["schema_version"] == 1 for t in traces)


def stopped_frame(host, tick, *, last_seq=None):
    payload = STATE_LAYOUT.pack(tick, host.adapter.seq if last_seq is None else last_seq,
        0, 0, 0, 0, 0, 0, 12000, 90, 0, 0, 1, 1, 3, 4, 7, 0)
    return encode_frame(STATE, host.adapter.session, tick // 50, payload)


def test_time_budget_yield_drains_small_fresh_queue_without_abort(project_host, qapp, monkeypatch):
    host=project_host
    handshake(host)
    host.worker.emit("status", message="first")
    host.worker.emit("status", message="second")
    timer=[0.0]
    def elapsed():
        timer[0]+=.020
        return timer[0]
    monkeypatch.setattr("car_host.controller.perf_counter",elapsed)
    batch=host.pump_io()
    assert batch['event_count']==1 and batch['budget_yielded']
    assert not batch['budget_exceeded'] and not host.io_backlog_exceeded
    spin(qapp,lambda: not host._io_events and not host._rx_pump_scheduled)
    assert any(entry.message=='second' for entry in host.logs)


def test_aged_queue_is_still_a_backlog_error(project_host, monkeypatch):
    host=project_host
    handshake(host)
    host.worker.emit("status",message="fresh")
    host.worker.emit("status",message="old pending")
    host.worker.events[-1]=replace(host.worker.events[-1],monotonic=time.monotonic()-.6)
    timer=[0.0]
    def elapsed():
        timer[0]+=.020
        return timer[0]
    monkeypatch.setattr("car_host.controller.perf_counter",elapsed)
    batch=host.pump_io()
    assert batch['budget_yielded'] and batch['budget_exceeded']
    assert host.io_backlog_exceeded and batch['pending_age_ms']>=500


def test_superseded_stop_does_not_confirm_later_zero_feedback(project_host, monkeypatch):
    host = project_host
    handshake(host)
    now = [time.monotonic()]
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: now[0])
        result = host.motion.stop()
        ack(host, STOP)
        host.worker.emit("rx", stopped_frame(host, 1050))
        host.poll()
        observed = host._stop_observations[result.request_id]
        first_zero = observed["first_zero_monotonic"]
        assert "continuous_zero_monotonic" not in observed
        assert host.motion.arm_pwm().success
        assert observed["observation_status"] == "superseded"
        assert observed["superseded_by_command"] == "arm_pwm"
        # A later command's stopped states cannot finish the previous STOP window.
        for index in range(22):
            now[0] += .05
            host.worker.emit("rx", stopped_frame(host, 1100 + index * 50))
            host.poll()
        assert observed["first_zero_monotonic"] == first_zero
        assert "continuous_zero_monotonic" not in observed


def test_completed_stop_evidence_survives_next_arm(project_host, monkeypatch):
    host = project_host
    handshake(host)
    now = [time.monotonic()]
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: now[0])
        result = host.motion.stop()
        ack(host, STOP)
        for index in range(23):
            now[0] += .05
            host.worker.emit("rx", stopped_frame(host, 1050 + index * 50))
            host.poll()
        observed = host._stop_observations[result.request_id]
        confirmation = observed["continuous_zero_monotonic"]
        assert host.motion.arm_pwm().success
        assert observed["continuous_zero_monotonic"] == confirmation
        assert "superseded_by_seq" not in observed


def test_health_epoch_is_metadata_and_retired_latest_is_not_fresh(project_host):
    host = project_host
    handshake(host)
    before = host.communication_health()
    assert before["parser_epoch"] == host.adapter.diagnostics["parser_epoch"]
    assert "parser.parser_epoch" not in before["counter_current"]
    parsed = before["counter_current"]["parser.frames_parsed"]
    host.adapter.reset()
    host.latest = None
    retired = host.communication_health()
    assert retired["parser_epoch"] > before["parser_epoch"]
    assert retired["counter_current"]["parser.frames_parsed"] == parsed
    assert retired["latest_state_age_ms"] is None and not retired["state_fresh"]
    host.worker.emit("rx", stopped_frame(host, 1050))
    host.poll()
    recovered = host.communication_health()
    assert recovered["state_fresh"] and recovered["epoch"] == before["epoch"]
    assert "parser.parser_epoch" not in recovered["counter_delta"]


def test_byte_budget_limits_already_queued_rx_blocks(project_host):
    host = project_host
    handshake(host)
    initial_count = host.frames_received
    first = b"".join(stopped_frame(host, 1050 + index * 50) for index in range(64))
    second = b"".join(stopped_frame(host, 4250 + index * 50) for index in range(64))
    assert len(first) == len(second) == 4096
    host.worker.emit("rx", first)
    host.worker.emit("rx", second)
    batch = host.pump_io(max_bytes=4096, time_budget_ms=100)
    assert batch["processed_bytes"] == 4096 and batch["event_count"] == 1
    assert host.io_backlog_exceeded and host.frames_received == initial_count + 64
    batch = host.pump_io(max_bytes=4096, time_budget_ms=100)
    assert not host.io_backlog_exceeded and batch["processed_bytes"] == 4096
    assert host.frames_received == initial_count + 128


def test_oversized_rx_cannot_pin_budget_or_make_complete_recording(project_host, qapp):
    host = project_host
    handshake(host)
    assert host.start_recording()
    host.worker.emit("rx", b"x" * 70000)
    host.worker.emit("rx", stopped_frame(host, 1050))
    batch = host.pump_io(time_budget_ms=100)
    assert batch["event_count"] == 2 and batch["processed_bytes"] == 64
    assert not host.io_backlog_exceeded and host.latest.device_time_ms == 1050
    assert host.recording_incomplete
    health = host.communication_health()
    assert health["counter_current"]["controller_rx_rejected"] == 1
    host.stop_recording()
    spin(qapp, lambda: not host.recorder.is_running)
    traces = [json.loads(line) for line in (host.recorder.directory / "receive_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    rejected = next(row for row in traces if row.get("kind") == "rx_rejected")
    assert rejected["byte_count"] == 70000 and "payload_hex" not in rejected
    assert all(len(line.encode("utf-8")) < 16384 for line in
               (host.recorder.directory / "receive_trace.jsonl").read_text(encoding="utf-8").splitlines())


def test_retired_connection_payload_does_not_pin_current_byte_budget(project_host):
    host = project_host
    handshake(host)
    initial_count = host.frames_received
    host.worker.emit("rx", b"x" * 70000, connection_id="retired-connection")
    host.worker.emit("rx", stopped_frame(host, 1050))
    batch = host.pump_io(max_bytes=4096, time_budget_ms=100)
    assert batch["event_count"] == 2 and batch["processed_bytes"] == 64
    assert not host.io_backlog_exceeded and host.frames_received == initial_count + 1
    assert host._rx_events_rejected == 0


def test_requested_event_budget_cannot_expand_controller_cache(project_host):
    host = project_host
    handshake(host)
    for _ in range(300):
        host.worker.emit("status", message="bounded flood")
    batch = host.pump_io(max_events=10000, time_budget_ms=100)
    assert batch["event_count"] == 256 and host.io_backlog_exceeded
    assert len(host._io_events) <= 256


@pytest.mark.parametrize("arguments", [{"max_events": True}, {"max_events": 0},
                                        {"max_bytes": 0}, {"time_budget_ms": float("nan")},
                                        {"time_budget_ms": False}])
def test_invalid_budget_does_not_enter_receive_pump(project_host, arguments):
    with pytest.raises(ValueError):
        project_host.pump_io(**arguments)
    assert not project_host._pumping_io
