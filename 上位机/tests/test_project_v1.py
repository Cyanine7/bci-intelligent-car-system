"""Frozen binary protocol and semantic control tests; no physical vehicle."""

import csv
from dataclasses import replace
import struct
import time

import pytest

from car_host.controller import HostController
from car_host.project_protocol import (ACK, ARM, CAPS, CAPS_LAYOUT, GET_PARAMS, HELLO,
    PARAMS, PARAMS_LAYOUT, PARAMETER_KEYS, PROJECT_V1, SET_PWM, SET_SPEED, STATE,
    STATE_LAYOUT, STOP, ProjectV1Adapter, crc16, encode_frame)
from car_host.project_panels import MotionPanel, ParameterPanel
from car_host.transport import ConnectionConfig
from car_host.ui import MainWindow
from test_application_extensions import Link, spin


def state_frame(adapter, *, tick=1000, last_seq=None, mode=0, armed=0, local=1,
                reason=0, reserved=0, session=None):
    payload = STATE_LAYOUT.pack(tick, adapter.seq if last_seq is None else last_seq,
        -456, 123, -120, 250, -2000, 3000, 12345, 91, mode, armed, reason, local,
        3, 4, 7, reserved)
    return encode_frame(STATE, adapter.session if session is None else session, 0, payload)


def test_crc_and_frozen_lengths():
    assert crc16(b"123456789") == 0x29B1
    assert CAPS_LAYOUT.size == 13 and PARAMS_LAYOUT.size == 20 and STATE_LAYOUT.size == 48
    # Literal golden vector shared with the MCU core.
    assert encode_frame(HELLO, 1234, 1).hex() == "a55a01010000d204000001000000f451"


@pytest.mark.parametrize("split", range(65))
def test_incremental_state_preserves_signed_fields_at_every_split(split):
    adapter = ProjectV1Adapter("BLUETOOTH_SPP")
    adapter.begin_session()
    frame = state_frame(adapter)
    samples = adapter.feed(frame[:split], 1.0) + adapter.feed(frame[split:], 1.05)
    assert len(samples) == 1
    sample = samples[0]
    assert sample.protocol == PROJECT_V1 and sample.device_time_ms == 1000
    assert sample.signed_left_speed_mps == -.12 and sample.signed_right_speed_mps == .25
    assert sample.encoder_left == -456 and sample.pwm_left == -2000
    assert sample.rx_dropped == 3 and sample.tx_dropped == 4
    assert not sample.armed and sample.local_enable


def test_crc_length_noise_half_frame_timeout_and_bounded_storage():
    adapter = ProjectV1Adapter("SERIAL")
    adapter.begin_session()
    good = state_frame(adapter)
    bad = bytearray(good)
    bad[25] ^= 1
    oversized = b"\xA5\x5A\x01\x83\xff\xff" + bytes(8)
    samples = adapter.feed(bytes(bad) + oversized + b"reset\xff" * 20000 + good, 1.0)
    assert len(samples) == 1
    assert adapter.diagnostics["buffered_bytes"] <= 143
    assert adapter.diagnostics["rejected_frames"] >= 2
    assert any("CRC" in issue for issue in adapter.pop_issues())
    newer = state_frame(adapter, tick=2000)
    assert adapter.feed(newer[:20], 2.0) == []
    assert adapter.feed(newer[20:] + newer, 2.101)[0].device_time_ms == 2000
    assert any("100 ms" in issue for issue in adapter.pop_issues())


def test_session_reserved_and_time_identity_are_validated():
    adapter = ProjectV1Adapter("SERIAL")
    adapter.begin_session()
    assert adapter.feed(state_frame(adapter, session=0)) == []
    assert adapter.feed(state_frame(adapter, reserved=1)) == []
    assert adapter.feed(state_frame(adapter, tick=0xFFFFFFFE))
    assert adapter.feed(state_frame(adapter, tick=2))  # Tick wrap is valid.
    assert adapter.feed(state_frame(adapter, tick=2)) == []  # A replay cannot refresh STATE age.
    assert adapter.feed(state_frame(adapter, tick=1)) == []
    assert adapter.feed(state_frame(adapter, tick=3, last_seq=2)) == []


@pytest.fixture
def project_host(qapp, tmp_path):
    host = HostController(tmp_path, worker_factory=Link)
    host.timer.stop()
    assert host.connect_device(ConnectionConfig(port="COM_TEST", protocol=PROJECT_V1))
    host.poll()
    yield host
    host.disconnect_device()
    spin(qapp, host.shutdown)


def reply(host, kind, payload=b"", seq=None, session=None):
    host.worker.emit("rx", encode_frame(kind, host.adapter.session if session is None else session,
        host.adapter.seq if seq is None else seq, payload))
    host.poll()


def ack(host, command, seq=None, status=0):
    reply(host, ACK, bytes((command, status)), seq)


def handshake(host):
    reply(host, CAPS, CAPS_LAYOUT.pack(15, 5000, 250, 800, 100, 20, 1), seq=1)
    ack(host, HELLO, seq=1)
    host.worker.emit("rx", state_frame(host.adapter))
    host.poll()


def update_state(host, **kwargs):
    host.worker.emit("rx", state_frame(host.adapter, **kwargs))
    host.poll()


def test_open_is_readonly_random_hello_and_caps_controls_capability(project_host):
    host = project_host
    payload = host.worker.sent[0][0]
    assert payload[3] == HELLO and host.adapter.session and host.adapter.seq == 1
    assert not host.capabilities.open_loop and not host.capabilities.parameter_write
    assert host.motion.arm_pwm().status == "unsupported"
    reply(host, CAPS, CAPS_LAYOUT.pack(15, 5000, 250, 800, 100, 20, 1), seq=2)
    assert not host.capabilities.open_loop
    reply(host, CAPS, CAPS_LAYOUT.pack(15, 5000, 250, 800, 100, 20, 1), session=0, seq=1)
    assert not host.capabilities.open_loop
    handshake(host)
    assert host.capabilities.open_loop and host.capabilities.motion
    assert not host.capabilities.raw_send
    assert not host.send_manual("reset", False)
    assert len(host.worker.sent) == 1


def test_explicit_arm_ack_and_state_then_one_finite_pwm(project_host):
    host = project_host
    handshake(host)
    assert not host.motion.set_pwm(100, -100, 100).success
    arm = host.motion.arm_pwm()
    assert arm.success and host.adapter.seq == 2
    assert not host.motion.set_pwm(100, -100, 100).success
    ack(host, ARM)
    assert host.command_results[arm.request_id].status == "accepted"
    assert not host.motion.set_pwm(100, -100, 100).success  # Previous STATE cannot confirm ARM.
    update_state(host, tick=1020, mode=1, armed=1)
    assert not host.motion.arm_pwm().success
    assert not host.motion.set_pwm(6000, 0, 100).success  # Device CAPS is stricter.
    result = host.motion.set_pwm(100, -100, 200)
    assert result.success
    left, right, duration, deadline = struct.unpack("<hhHI", host.worker.sent[-1][0][14:-2])
    assert (left, right, duration) == (100, -100, 200)
    assert 1220 <= deadline < 1230
    assert host.motion.set_pwm(100, 100, 200).status == "rejected"
    ack(host, SET_PWM)
    update_state(host, tick=1030, mode=1, armed=1)
    assert host.project_motion_active and not host.motion.set_pwm(0, 0, 1).success
    update_state(host, tick=1220, mode=0, armed=0, reason=2)
    assert not host.project_motion_active
    assert not host.motion.set_pwm(100, 100, 200).success  # Must re-arm explicitly.


def test_state_freshness_local_disable_speed_limits_and_stop_exception(project_host):
    host = project_host
    assert host.motion.stop().success  # CAPS has not arrived; STOP is allowed.
    ack(host, STOP)
    handshake(host)
    host.latest = replace(host.latest, received_monotonic=time.monotonic() - .501)
    assert not host.motion.arm_speed().success
    update_state(host, tick=1010, local=0)
    assert not host.motion.arm_speed().success
    update_state(host, tick=1020, local=1)
    assert host.motion.arm_speed().success
    ack(host, ARM)
    update_state(host, tick=1030, mode=2, armed=1)
    assert not host.motion.set_speed(251, 0, 100).success
    assert not host.motion.set_speed(200, 0, 801).success
    assert host.motion.set_speed(-100, 200, 800).success
    ack(host, SET_SPEED)
    assert host.motion.stop().success


def test_parameter_draft_validation_ack_identity_and_actual_ram_readback(project_host):
    host = project_host
    handshake(host)
    values = dict(zip(PARAMETER_KEYS, (100, 200, 300, 400)))
    assert not host.parameters.apply_parameters({"Kp": 1}).success
    result = host.parameters.apply_parameters(values)
    assert result.success and host.actual_parameters is None
    seq = host.adapter.seq
    reply(host, PARAMS, PARAMS_LAYOUT.pack(8, 100, 200, 300, 400), seq=1)
    assert host.actual_parameters is None
    ack(host, GET_PARAMS, seq=seq)  # ACK type must echo SET_PARAMS.
    assert host.command_results[result.request_id].status == "queued"
    reply(host, PARAMS, PARAMS_LAYOUT.pack(8, 100, 200, 300, 400), seq=seq)
    assert host.actual_parameters == dict(values, revision=8)
    ack(host, 7, seq=seq)
    assert "确认" in host.parameter_status
    update_state(host, tick=1010, mode=1, armed=1)
    assert not host.parameters.apply_parameters(values).success


def test_timeout_unknown_late_reply_ignored_and_reconnect_never_replays(project_host):
    host = project_host
    handshake(host)
    result = host.parameters.read_parameters()
    old_session, seq = host.adapter.session, host.adapter.seq
    host._project_pending[seq]["due"] = time.monotonic() - .1
    host.poll()
    assert host.command_results[result.request_id].status == "unknown"
    reply(host, PARAMS, PARAMS_LAYOUT.pack(1, 1, 2, 3, 4), seq=seq)
    assert host.actual_parameters is None
    old_count = len(host.worker.sent)
    host.poll()
    assert len(host.worker.sent) == old_count
    host.disconnect_device()
    host.poll()
    assert host.connect_device(ConnectionConfig(port="COM_TEST", protocol=PROJECT_V1))
    host.poll()
    assert host.adapter.session != old_session
    assert len(host.worker.sent) == 1 and host.worker.sent[0][0][3] == HELLO
    assert not host.capabilities.motion and host.actual_parameters is None


def test_stop_remains_available_at_pending_request_limit_and_does_not_repeat(project_host):
    host = project_host
    handshake(host)
    reads = [host.parameters.read_parameters() for _ in range(32)]
    assert all(result.success for result in reads)
    assert not host.parameters.read_parameters().success
    result = host.motion.stop()
    assert result.success and host.worker.sent[-1][0][3] == STOP
    assert len(host._project_pending) == 32
    assert host.command_results[reads[0].request_id].status == "unknown"
    count = len(host.worker.sent)
    assert host.motion.stop().request_id == result.request_id
    assert len(host.worker.sent) == count


def test_out_of_order_parameter_replies_cannot_overwrite_newer_revision(project_host):
    host = project_host
    handshake(host)
    first = host.parameters.read_parameters()
    old_seq = host.adapter.seq
    second = host.parameters.read_parameters()
    new_seq = host.adapter.seq
    reply(host, PARAMS, PARAMS_LAYOUT.pack(8, 100, 200, 300, 400), seq=new_seq)
    ack(host, GET_PARAMS, seq=new_seq)
    reply(host, PARAMS, PARAMS_LAYOUT.pack(7, 1, 2, 3, 4), seq=old_seq)
    ack(host, GET_PARAMS, seq=old_seq)
    assert host.actual_parameters["revision"] == 8
    assert host.actual_parameters["kp_left_q100"] == 100
    assert host.command_results[second.request_id].status == "accepted"
    assert host.command_results[first.request_id].status == "accepted"  # ACK differs from verified readback.


def test_new_panels_use_services_and_separate_actual_values_from_draft(project_host, qapp):
    host = project_host
    window = MainWindow(host)
    motion = MotionPanel(host, 1)
    parameters = ParameterPanel(host)
    try:
        assert not motion.arm_button.isEnabled() and not parameters.apply_button.isEnabled()
        assert not window.send_button.isEnabled() and window.stop_button.isEnabled()
        handshake(host)
        assert motion.arm_button.isEnabled() and not motion.run_button.isEnabled()
        assert parameters.apply_button.isEnabled() and window.pid_button.isEnabled()
        parameters.draft_boxes["kp_left_q100"].setValue(555)
        parameters.read_button.click()
        seq = host.adapter.seq
        reply(host, PARAMS, PARAMS_LAYOUT.pack(9, 100, 200, 300, 400), seq=seq)
        ack(host, GET_PARAMS, seq=seq)
        assert parameters.draft_boxes["kp_left_q100"].value() == 555
        assert "revision=9" in parameters.actual_label.text()
        parameters.copy_button.click()
        assert parameters.draft_boxes["kp_left_q100"].value() == 100
        motion.arm_button.click()
        ack(host, ARM)
        update_state(host, tick=1030, mode=1, armed=1)
        assert motion.run_button.isEnabled() and not parameters.apply_button.isEnabled()
        motion.run_button.click()
        assert not motion.run_button.isEnabled()
        assert "-456" in window.device_state_value.text()
        assert window.left_value.text().startswith("-")
    finally:
        motion.close()
        parameters.close()
        window.close()


def test_motion_panel_explains_local_enable_gate_without_sending(project_host):
    host = project_host
    handshake(host)
    panel = MotionPanel(host, 1)
    try:
        sent_count = len(host.worker.sent)
        update_state(host, tick=1010, local=0)
        assert not panel.arm_button.isEnabled()
        assert "电机使能开关" in panel.arm_hint.text()
        assert "PD3" in panel.arm_button.toolTip()
        panel.arm_button.click()
        assert len(host.worker.sent) == sent_count
        update_state(host, tick=1020, local=1)
        assert panel.arm_button.isEnabled()
        assert "不可用" not in panel.arm_hint.text()
        assert len(host.worker.sent) == sent_count
    finally:
        panel.close()


def test_csv_records_device_stop_and_signed_fields(project_host, qapp):
    host = project_host
    handshake(host)
    assert host.start_recording()
    update_state(host, tick=1010, reason=2)
    directory = host.recorder.directory
    host.stop_recording()
    spin(qapp, lambda: not host.recorder.is_running)
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as file:
        row = next(csv.DictReader(file))
    assert row["stop_reason"] == "2" and row["signed_left_speed_mps"] == "-0.12"
    assert row["encoder_left"] == "-456" and row["pwm_left"] == "-2000"


def test_project_simulator_rejected_without_link_or_guessing(qapp, tmp_path):
    host = HostController(tmp_path, worker_factory=Link)
    host.timer.stop()
    assert not host.connect_device(ConnectionConfig(kind="simulation", protocol=PROJECT_V1))
    assert host.worker is None
    assert host.shutdown()
