"""Finite two-segment wire and fake-device contracts; no vehicle access."""

import struct

import pytest

from car_host.device import DeviceCapabilities, DeviceResult, MotionService
from car_host.project_fake import FAKE_CONFIG, ProjectFakeWorker
from car_host.project_protocol import (
    ACK, ARM, GET_PROFILE_CAPS, HELLO, PROFILE_CAPS, PROFILE_CAPS_LAYOUT,
    PROFILE_COMMAND_LAYOUT, PROFILE_STATE_LAYOUT, SET_PWM, SET_PWM_PROFILE,
    STATE, STATE_LAYOUT, STATE_PROFILE, STOP, ProjectV1Adapter, encode_frame,
    validate_pwm_segments,
)


SEGMENTS = [dict(left_pwm=1400, right_pwm=0, duration_ms=400),
            dict(left_pwm=900, right_pwm=0, duration_ms=600)]


def adapter():
    result = ProjectV1Adapter("SOFTWARE_ONLY")
    result.session, result.seq = 1234, 4
    return result


def profile_state(*, tick=600, seq=4, index=1, status=1, actual=500,
                  anchor=100, profile_seq=4, reserved=0):
    body = STATE_LAYOUT.pack(tick, seq, 1, 0, 17, 0, 900 if status == 1 else 0,
        0, 12000, 80, 1 if status == 1 else 0, int(status == 1), 0, 1, 0, 0, 0, 0)
    body += PROFILE_STATE_LAYOUT.pack(profile_seq, anchor, actual, index, status, reserved)
    return encode_frame(STATE_PROFILE, 1234, 7, body)


def test_frozen_profile_golden_vectors():
    assert PROFILE_COMMAND_LAYOUT.size == 20
    assert PROFILE_CAPS_LAYOUT.size == 12 and PROFILE_STATE_LAYOUT.size == 16
    body = PROFILE_COMMAND_LAYOUT.pack(1, 2, 0, 1400, 0, 900, 0, 400, 1000, 1100)
    assert encode_frame(SET_PWM_PROFILE, 1234, 4, body).hex() == (
        "a55a01081400d2040000040000000102000078050000840300009001e8034c040000e22f")
    assert encode_frame(GET_PROFILE_CAPS, 1234, 2).hex() == "a55a01090000d204000002000000a668"
    assert encode_frame(PROFILE_CAPS, 1234, 2, PROFILE_CAPS_LAYOUT.pack(3, 1, 2, 6000, 1000, 100, 20)).hex() == (
        "a55a01850c00d204000002000000030001027017e803640014009b8a")
    assert profile_state().hex() == (
        "a55a01864000d204000007000000580200000400000001000000000000001100000084030000"
        "e02e500001010001000000000000000000000000000000000400000064000000f4010000010100003331")


@pytest.mark.parametrize("split", range(81))
def test_complete_profile_state_split_and_repeated_metadata(split):
    parser = adapter()
    raw = profile_state()
    samples = parser.feed(raw[:split], 1.0) + parser.feed(raw[split:], 1.03)
    assert len(samples) == 1
    sample = samples[0]
    assert (sample.profile_seq, sample.profile_start_tick, sample.segment1_actual_tick,
            sample.segment_index, sample.profile_status) == (4, 100, 500, 1, 1)
    assert sample.pwm_left == 900 and sample.frame_seq == 7
    ended = parser.feed(profile_state(tick=1100, status=2), 1.05)[0]
    assert ended.segment_index == 1 and ended.segment1_actual_tick == 500
    assert not ended.armed and ended.pwm_left == 0


@pytest.mark.parametrize("changes", [dict(reserved=1), dict(index=2), dict(status=3),
    dict(profile_seq=5), dict(index=255), dict(actual=601), dict(actual=99),
    dict(index=0, actual=500), dict(status=0), dict(profile_seq=0)])
def test_invalid_profile_evidence_never_updates_state(changes):
    parser = adapter()
    assert parser.feed(profile_state(**changes)) == []
    assert parser.diagnostics["state_invalid"] == 1


def test_profile_wrap_and_accepted_but_never_executed_stop():
    parser = adapter()
    sample = parser.feed(profile_state(tick=50, anchor=0xFFFFFFF0, actual=0, index=1), 1.0)[0]
    assert sample.segment1_actual_tick == 0  # A valid wrap tick, not missing evidence.
    parser.reset()
    sample = parser.feed(profile_state(status=2, index=255, actual=0), 1.03)[0]
    assert sample.profile_seq == 4 and sample.segment_index == 255


@pytest.mark.parametrize("segments", [None, [], SEGMENTS[:1], SEGMENTS * 2,
    [{**SEGMENTS[0], "left_pwm": True}, SEGMENTS[1]],
    [{**SEGMENTS[0], "left_pwm": 10001}, SEGMENTS[1]],
    [SEGMENTS[0], {**SEGMENTS[1], "left_pwm": -1}],
    [SEGMENTS[0], {**SEGMENTS[1], "duration_ms": 3601}],
    [{**SEGMENTS[0], "duration_ms": 0}, SEGMENTS[1]],
    [{**SEGMENTS[0], "extra": 1}, SEGMENTS[1]]])
def test_profile_invalid_tables_do_not_consume_seq(segments):
    parser = adapter()
    with pytest.raises(ValueError):
        parser.encode_command("set_pwm_profile", dict(segments=segments, deadline_tick=1100))
    assert parser.seq == 4


def test_zero_second_segment_and_identical_pwm_segments_are_valid():
    for second in (0, 1400):
        table = [SEGMENTS[0], {**SEGMENTS[1], "left_pwm": second}]
        result = validate_pwm_segments(table)
        assert result == table and result[0] is not table[0]
        parser = adapter()
        raw = parser.encode_command("set_pwm_profile", dict(segments=table, deadline_tick=1100))
        assert PROFILE_COMMAND_LAYOUT.unpack(raw[14:-2])[5] == second


def test_profile_services_use_only_gateway():
    class Gateway:
        capabilities = DeviceCapabilities(pwm_profile=True)
        def __init__(self):
            self.calls = []
        def send_command(self, command, values=None):
            self.calls.append((command, values))
            return DeviceResult(True, "queued", "fixture")
    gateway = Gateway()
    service = MotionService(gateway)
    assert service.query_profile_caps().success and service.set_pwm_profile(SEGMENTS).success
    assert gateway.calls == [("query_profile_caps", None), ("set_pwm_profile", dict(segments=SEGMENTS))]
    assert gateway.capabilities.supports("pwm_profile")


class Clock:
    value = 0.0
    def __call__(self):
        return self.value


def fake(*, faults=None):
    clock = Clock()
    worker = ProjectFakeWorker(FAKE_CONFIG, clock=clock, faults=faults)
    worker.start()
    worker.drain_events()
    worker.request_send(encode_frame(HELLO, 1234, 1))
    worker.drain_events()
    return worker, clock


def reply_status(worker, kind, seq, body=b""):
    assert worker.request_send(encode_frame(kind, 1234, seq, body))
    acknowledgements = [event.payload[14:16] for event in worker.drain_events()
                        if event.kind == "rx" and event.payload[3] == ACK]
    assert acknowledgements and acknowledgements[-1][0] == kind
    return acknowledgements[-1][1]


def test_fake_keeps_old_state_until_explicit_query_and_old_firmware_is_supported():
    worker, clock = fake()
    clock.value = .05
    states = [event.payload for event in worker.drain_events() if event.kind == "rx"]
    assert states[0][3] == STATE and len(states[0]) == 64
    assert reply_status(worker, GET_PROFILE_CAPS, 2) == 0
    clock.value = .10
    states = [event.payload for event in worker.drain_events() if event.kind == "rx"]
    assert states[0][3] == STATE_PROFILE and len(states[0]) == 80
    unsupported, _ = fake(faults={"profile_unsupported": True})
    assert reply_status(unsupported, GET_PROFILE_CAPS, 2) == 9
    assert not unsupported._profile_enabled


def test_fake_switches_identical_pwm_segments_and_preserves_terminal_evidence():
    worker, clock = fake()
    assert reply_status(worker, GET_PROFILE_CAPS, 2) == 0
    assert reply_status(worker, ARM, 3, b"\x01") == 0
    raw = PROFILE_COMMAND_LAYOUT.pack(1, 2, 0, 1400, 0, 1400, 0, 400, 1000, 1000)
    assert reply_status(worker, SET_PWM_PROFILE, 4, raw) == 0
    assert reply_status(worker, SET_PWM_PROFILE, 5, raw) == 4
    assert reply_status(worker, SET_PWM, 6, struct.pack("<hhHI", 900, 0, 1000, 1000)) == 4
    clock.value = .40
    worker.advance()
    assert worker.segment_index == 1 and worker.segment1_actual_tick == 400
    assert worker.pwm_left == 1400 and worker._deadline == 1000
    clock.value = 1.0
    worker.advance()
    assert not worker.armed and worker.pwm_left == 0
    assert worker.profile_status == 2 and worker.segment_index == 1
    assert reply_status(worker, ARM, 7, b"\x01") == 0
    assert worker.profile_status == 0 and worker.segment_index == 255


def test_fake_early_deadline_and_stop_cancel_second_segment():
    worker, clock = fake()
    reply_status(worker, GET_PROFILE_CAPS, 2)
    reply_status(worker, ARM, 3, b"\x01")
    raw = PROFILE_COMMAND_LAYOUT.pack(1, 2, 0, 1400, 0, 900, 0, 400, 1000, 300)
    assert reply_status(worker, SET_PWM_PROFILE, 4, raw) == 0
    clock.value = .3
    worker.advance()
    assert worker.profile_status == 2 and worker.segment_index == 0 and worker.segment1_actual_tick == 0
    clock.value = .5
    worker.advance()
    assert worker.pwm_left == 0
    assert reply_status(worker, ARM, 5, b"\x01") == 0
    assert reply_status(worker, SET_PWM, 6, struct.pack("<hhHI", 1000, 0, 100, 1501)) == 8
    # Any identity STOP remains accepted and stops, matching the actual core.
    worker.request_send(encode_frame(STOP, 999, 1))
    assert not worker.armed


def test_rx_provenance_candidates_and_counters_survive_reset():
    parser = adapter()
    raw = profile_state()
    assert parser.feed(raw[:20], 1.0, rx_metadata=dict(rx_block_id=10)) == []
    sample = parser.feed(raw[20:], 1.03, rx_metadata=dict(rx_block_id=11,
        parent_read_monotonic=1.04, worker_enqueued_monotonic=1.05,
        controller_started_monotonic=1.06))[0]
    assert (sample.rx_first_block_id, sample.rx_last_block_id, sample.rx_first_monotonic) == (10, 11, 1.0)
    assert sample.received_monotonic == 1.03 and sample.controller_started_monotonic == 1.06
    assert {entry["reason"] for entry in parser.pop_diagnostics()} >= {"candidate", "append", "accepted"}
    assert parser.feed(raw[:20], 2.0, rx_metadata=dict(rx_block_id=12)) == []
    assert parser.feed(b"", 9.0) == []  # A processing timer does not discard sampled bytes.
    assert parser.feed(raw[20:], 2.108, rx_metadata=dict(rx_block_id=13)) == []
    assert parser.diagnostics["partial_timeouts"] == 1
    trace = next(entry for entry in parser.pop_diagnostics() if entry["reason"] == "partial_timeout")
    assert trace["rx_first_block_id"] == 12 and trace["session"] == 1234 and trace["seq"] == 7
    before = parser.diagnostics
    parser.reset()
    assert parser.diagnostics["partial_timeouts"] == 1
    assert parser.diagnostics["parser_epoch"] == before["parser_epoch"] + 1


@pytest.mark.parametrize("fractional_age", [.001, .019, .021, .029, .031, .051])
def test_host_maximum_profile_on_fractional_control_clock(qapp, tmp_path, monkeypatch, fractional_age):
    """An immediate link must not overestimate the 100 Hz device by 1..9 ms."""
    import car_host.controller as controller_module
    from car_host.controller import HostController

    clock = Clock()
    monkeypatch.setattr(controller_module.time, "monotonic", clock)
    host = HostController(tmp_path, worker_factory=lambda config: ProjectFakeWorker(config, clock=clock))
    host.timer.stop()
    def drain():
        # Controller intentionally processes bounded batches. The advanced
        # clock generated 27 STATE packets, so one poll need not consume all.
        for _ in range(30):
            host.poll()
    try:
        assert host.connect_device(FAKE_CONFIG)
        drain()
        clock.value = 1.35
        drain()
        assert host.latest.device_time_ms == 1350
        assert host.motion.query_profile_caps().success
        drain()
        assert host.capabilities.pwm_profile
        assert host.motion.arm_pwm().success
        drain()
        clock.value = 1.4
        drain()
        assert host.latest.device_time_ms == 1400 and host.latest.armed
        clock.value += fractional_age
        result = host.motion.set_pwm_profile(SEGMENTS)
        assert result.success
        drain()
        assert host.command_results[result.request_id].status == "accepted"
        assert 990 <= ((host.worker._deadline - host.worker.tick_ms) & 0xFFFFFFFF) <= 1000
    finally:
        host.disconnect_device();host.poll()
        assert host.shutdown()
