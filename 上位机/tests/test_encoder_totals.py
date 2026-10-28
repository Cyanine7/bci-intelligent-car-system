"""Full count semantics, coherent boundaries and old-firmware compatibility."""

import csv
from dataclasses import replace
import json
import time

import pytest

from car_host.controller import HostController
from car_host.encoder_panel import EncoderTotalsPanel
from car_host.encoder_totals import signed_delta64
from car_host.encoder_verification import verify_encoder_recording
from car_host.project_protocol import (
    ProjectV1Adapter, PROJECT_V1, CAPS, CAPS_LAYOUT, ACK, STATE, STATE_LAYOUT,
    ENCODER_CAPS, ENCODER_TOTALS, ENCODER_CAPS_LAYOUT, ENCODER_TOTALS_LAYOUT, encode_frame,
)
from car_host.transport import ConnectionConfig
from test_application_extensions import Link, spin


def total_frame(adapter, *, tick=1050, seq=1, samples=5, left=100, right=200, epoch=10, delayed=0):
    return encode_frame(ENCODER_TOTALS, adapter.session, seq,
        ENCODER_TOTALS_LAYOUT.pack(1, 1, epoch, tick, samples, left, right, 10, delayed))


@pytest.mark.parametrize("split", range(57))
def test_full_totals_decode_all_splits_without_becoming_state(split):
    p = ProjectV1Adapter("SERIAL"); p.begin_session()
    raw = total_frame(p, left=2**40+1, right=-(2**40)+3)
    assert p.feed(raw[:split], 1.0) + p.feed(raw[split:], 1.05) == []
    s, = p.pop_encoder_totals()
    assert (s.total_left, s.total_right, s.device_time_ms) == (2**40+1, -(2**40)+3, 1050)
    assert s.received_monotonic == (1.0 if split == len(raw) else 1.05)
    assert p._last_state_tick is None


def test_totals_replay_epoch_change_and_counter_wrap():
    p = ProjectV1Adapter("SERIAL"); p.begin_session()
    p.feed(total_frame(p, tick=0xfffffff0, seq=0xfffffffe, samples=0xfffffffe), 1)
    p.pop_encoder_totals()
    p.feed(total_frame(p, tick=10, seq=0, samples=0), 2)
    assert len(p.pop_encoder_totals()) == 1
    p.feed(total_frame(p, tick=10, seq=0, samples=0), 2.1)
    p.feed(total_frame(p, tick=20, seq=1, samples=1, epoch=11), 2.2)
    assert not p.pop_encoder_totals()
    assert p.diagnostics["encoder_replayed"] == 2
    assert signed_delta64(-(2**63)+2, 2**63-3) == 5
    assert signed_delta64(2**63-3, -(2**63)+2) == -5


def quiet_state(host, tick, seq=1):
    return encode_frame(STATE, host.adapter.session, seq, STATE_LAYOUT.pack(
        tick, host.adapter.seq, 0, 0, 0, 0, 0, 0, 11500, 57, 0, 0, 0, 1, 0, 0, 0, 0))


def connected_host(qapp, tmp_path):
    h = HostController(tmp_path, worker_factory=Link); h.timer.stop()
    assert h.connect_device(ConnectionConfig(port="FAKE", protocol=PROJECT_V1))
    h.poll()
    h.worker.emit("rx", encode_frame(CAPS, h.adapter.session, 1,
        CAPS_LAYOUT.pack(15, 6000, 1180, 1000, 100, 20, 1))
        + encode_frame(ACK, h.adapter.session, 1, b"\x01\x00")
        + quiet_state(h, 1000))
    h.poll()
    return h


def enable_totals(h, *, ack=True):
    result = h.encoder_totals.query()
    assert result.success
    seq = h.adapter.seq
    h.worker.emit("rx", encode_frame(ENCODER_CAPS, h.adapter.session, seq,
        ENCODER_CAPS_LAYOUT.pack(1, 64, 100, 20, 0, 1560, 66000, 160000)))
    if ack:
        h.worker.emit("rx", encode_frame(ACK, h.adapter.session, seq, b"\x0a\x00"))
    h.worker.emit("rx", total_frame(h.adapter))
    h.poll()
    return seq


def test_discovery_needs_matching_reply_and_ack_no_motion(qapp, tmp_path):
    h = connected_host(qapp, tmp_path)
    assert [raw[3] for raw, _ in h.worker.sent] == [1]
    seq = enable_totals(h, ack=False)
    assert not h.capabilities.encoder_totals and not h.encoder_totals.begin().success
    h.worker.emit("rx", encode_frame(ACK, h.adapter.session, seq, b"\x0a\x00")); h.poll()
    assert h.capabilities.encoder_totals and h.encoder_totals.fresh
    assert [raw[3] for raw, _ in h.worker.sent] == [1, 10]
    assert h.shutdown()


def test_old_firmware_rejection_does_not_fabricate_totals(qapp, tmp_path):
    h = connected_host(qapp, tmp_path); h.encoder_totals.query()
    h.worker.emit("rx", encode_frame(ACK, h.adapter.session, h.adapter.seq, b"\x0a\x09")); h.poll()
    assert not h.capabilities.encoder_totals
    assert "未支持" in h.encoder_status
    assert h.latest_encoder_totals is None and not h.encoder_totals.begin().success
    assert h.shutdown()


def test_manual_five_turns_full_difference_and_boundaries_saved(qapp, tmp_path):
    h = connected_host(qapp, tmp_path); enable_totals(h)
    panel = EncoderTotalsPanel(h)
    before = len(h.worker.sent)
    assert h.encoder_totals.begin().success
    assert not h.motion.arm_pwm().success
    # Lost intermediate radio snapshots do not lose the MCU's total counts.
    h.worker.emit("rx", total_frame(h.adapter, tick=1250, seq=5, samples=25, left=7900, right=-7600))
    h.poll()
    h.worker.emit("rx", quiet_state(h, 1450, seq=2) + total_frame(h.adapter,
        tick=1450, seq=9, samples=45, left=7900, right=-7600))
    h.poll()
    result = h.encoder_totals.finish()
    assert result.success and result.values["delta_left"] == 7800 and result.values["delta_right"] == -7800
    assert result.values["counts_per_left_turn"] == result.values["counts_per_right_turn"] == 1560
    assert len(h.worker.sent) == before
    spin(qapp, lambda: not h.recorder.is_running)
    path = h.recorder.directory
    rows = list(csv.DictReader((path/"encoder_totals.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 2 and int(rows[-1]["total_left"]) == 7900
    markers = [json.loads(x) for x in (path/"receive_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(x for x in markers if x.get("kind") == "encoder_calibration_start")["snapshot"]["total_left"] == 100
    assert next(x for x in markers if x.get("kind") == "encoder_calibration_end")["delta_right"] == -7800
    audit = verify_encoder_recording(path)
    assert audit["verified"] and audit["calibration_count"] == 1 and audit["raw_rows"] == 2
    # An otherwise well-formed forged result fails independent arithmetic.
    trace = path/"receive_trace.jsonl"
    text = trace.read_text(encoding="utf-8")
    trace.write_text(text.replace('"delta_right":-7800', '"delta_right":-7799'), encoding="utf-8")
    assert not verify_encoder_recording(path)["verified"]
    panel.close()
    assert h.shutdown()


def test_fake_device_manual_counts_are_accumulated_at_every_tick(qapp, tmp_path):
    from car_host.project_fake import FAKE_CONFIG, ProjectFakeWorker
    h = HostController(tmp_path, worker_factory=ProjectFakeWorker); h.timer.stop()
    assert h.connect_device(FAKE_CONFIG)
    spin(qapp, lambda: (h.poll() or True) and h.project_state_fresh)
    h.encoder_totals.query()
    spin(qapp, lambda: (h.poll() or True) and h.encoder_totals.fresh)
    assert h.encoder_totals.begin().success
    h.worker.faults["manual_left_counts"] = 13
    deadline = time.monotonic()+.1
    while time.monotonic() < deadline:
        h.poll(); time.sleep(.01)
    h.worker.faults["manual_left_counts"] = 0
    h.worker.faults["manual_right_counts"] = -17
    deadline = time.monotonic()+.1
    while time.monotonic() < deadline:
        h.poll(); time.sleep(.01)
    h.worker.faults["manual_right_counts"] = 0
    spin(qapp, lambda: (h.poll() or True) and h.encoder_totals.quiet)
    result = h.encoder_totals.finish(1, 1)
    assert result.success
    assert result.values["delta_left"] > 0 and result.values["delta_right"] < 0
    spin(qapp, lambda: not h.recorder.is_running)
    assert verify_encoder_recording(h.recorder.directory)["verified"]
    assert [raw[0][3] for raw in h.worker.sent] == [1,10]
    assert h.shutdown()


@pytest.mark.parametrize("fault", ["stale", "delayed", "recording", "epoch", "drop"])
def test_manual_result_rejects_bad_boundaries(qapp, tmp_path, fault):
    h = connected_host(qapp, tmp_path); enable_totals(h)
    assert h.encoder_totals.begin().success
    h.worker.emit("rx", total_frame(h.adapter, tick=1250, seq=5, samples=25, left=7900, right=8000,
        delayed=1 if fault == "delayed" else 0))
    h.poll()
    h.worker.emit("rx", quiet_state(h, 1450, seq=2) + total_frame(h.adapter,
        tick=1450, seq=9, samples=45, left=7900, right=8000, delayed=1 if fault == "delayed" else 0))
    h.poll()
    if fault == "stale":
        h.latest_encoder_totals = replace(h.latest_encoder_totals, received_monotonic=time.monotonic()-1)
    elif fault == "recording":
        h.stop_recording()
    elif fault == "epoch":
        h.latest_encoder_totals = replace(h.latest_encoder_totals, epoch_tick=999)
    elif fault == "drop":
        h._device_dropped = (1, 0)
    assert not h.encoder_totals.finish().success
    h.encoder_totals.cancel()
    spin(qapp, lambda: not h.recorder.is_running)
    assert h.shutdown()


def test_stop_cancels_manual_baseline_and_remains_available(qapp, tmp_path):
    h = connected_host(qapp, tmp_path); enable_totals(h)
    assert h.encoder_totals.begin().success
    assert h.motion.stop().success
    assert h.encoder_totals.baseline is None
    assert h.worker.sent[-1][0][3] == 4
    spin(qapp, lambda: not h.recorder.is_running)
    assert h.shutdown()
