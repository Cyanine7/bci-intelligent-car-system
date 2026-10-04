import csv
from copy import deepcopy
import json
import threading
import time
import xml.etree.ElementTree as ET

import pytest

from car_host.experiment_models import build_plan
from car_host.experiment_recording import ExperimentJournal, summarize_trial


def plan():
    return build_plan({"name": "测试计划", "stages": [{"stage_id": "left", "name": "左轮",
                     "steps": [{"left_pwm": 850, "right_pwm": 0, "duration_ms": 500}]}]})


def trial(speeds=(0.0, 0.051, 0.051, 0.051)):
    return {"trial_id": "left-t001", "label": "左轮测试", "left_pwm": 850, "right_pwm": 0,
            "duration_ms": 500, "stop_after_ms": None, "connection_id": "connection-1",
            "session": 123, "arm_request_id": "arm-1", "arm_seq": 2,
            "pwm_request_id": "pwm-1", "pwm_seq": 3, "stop_request_id": None,
            "stop_seq": None, "started_utc": "2026-10-04T01:00:00Z", "started_monotonic": 100.0,
            "status": "completed", "reason": "到期", "ack_results": {"arm": "accepted", "pwm": "accepted"},
            "stop_confirmed": True, "observations": [], "samples": [
                dict(received_monotonic=100.1 + index * 0.05, received_utc="2026-10-04T01:00:00Z",
                     connection_id="connection-1", device_session=123, device_last_seq=3,
                     device_time_ms=1000 + index * 50, armed=True, control_mode=1, pwm_left=850,
                     pwm_right=0, signed_left_speed_mps=speed, signed_right_speed_mps=0.0,
                     quality=(), raw_frame=b"\xa5\x5a") for index, speed in enumerate(speeds)]}


def wait_for(predicate, timeout=3):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("异步记录未在期限内达到预期状态")


@pytest.mark.parametrize("speeds,expected", [
    ((0.0, 0.051, 0.051, 0.051), "sustained"),
    ((0.0, 0.0, 0.0, 0.0), "not_sustained"),
    ((0.0, 0.051, 0.0, 0.051), "not_sustained"),
    ((0.0, 0.051, 0.051), "insufficient"),
    ((-0.051, -0.051, -0.051, -0.051), "not_sustained"),
])
def test_valid_zero_failure_and_final_four_feedback(speeds, expected):
    row = summarize_trial(trial(speeds))
    assert row["left_feedback"] == expected
    assert row["right_feedback"] == "not_tested"
    assert row["eligible_sample_count"] == len(speeds)
    assert row["left_observation_delay_ms"] == (pytest.approx(150) if 0.051 in speeds else None)


def test_negative_direction_identity_sequence_and_stop_filters():
    entry = trial((-0.051,) * 4)
    entry["left_pwm"] = -850
    for sample in entry["samples"]:
        sample["pwm_left"] = -850
    row = summarize_trial(entry)
    assert row["left_feedback"] == "sustained"
    assert row["left_tail_median_mps"] == -0.051
    excluded = []
    for key, value in (("connection_id", "old"), ("device_session", 2),
                       ("device_last_seq", 2), ("device_last_seq", 4), ("armed", False),
                       ("pwm_left", 0), ("control_mode", 0), ("quality", ("bad",))):
        sample = deepcopy(entry["samples"][0])
        sample[key], sample["device_time_ms"] = value, 10000 + len(excluded) * 50
        excluded.append(sample)
    entry["stop_seq"] = 4
    entry["samples"].extend(excluded)
    assert summarize_trial(entry)["eligible_sample_count"] == 4


def test_pwm_time_base_tail_statistics_and_voltage_range():
    entry = trial()
    entry["pwm_started_monotonic"] = 100.05
    for sample, voltage in zip(entry["samples"], (11950, 11940, 11970, 11960)):
        sample["battery_mv"] = voltage
    row = summarize_trial(entry)
    assert row["left_observation_delay_ms"] == pytest.approx(100)
    assert row["left_tail_mean_mps"] == pytest.approx(0.03825)
    assert row["left_tail_std_mps"] == pytest.approx(0.0220836477965032)
    assert row["battery_min_mv"] == 11940 and row["battery_max_mv"] == 11970
    assert row["software_output_observed"] is True
    entry["samples"].append(deepcopy(entry["samples"][0]))
    assert summarize_trial(entry)["eligible_sample_count"] == 4


def test_writes_detached_evidence_reports_and_post_report_human_notes(tmp_path):
    original_plan = plan()
    context = {"caps": {"pwm_limit": 6000}, "actual_parameters": {"revision": 0},
               "source": "SIMULATION_PROJECT_V1", "software_demo": True,
               "raw_handshake_covered": False, "handshake_note": "开始于既有连接之后"}
    journal = ExperimentJournal(tmp_path, original_plan, "run-1", "left", context)
    original_plan["name"] = "之后被修改"
    context["caps"]["pwm_limit"] = 1
    wait_for(lambda: journal.ready)
    assert journal.event("trial_started", trial_id="left-t001", raw_frame=b"\xa5\x5a")
    assert journal.observation("left-t001", "目视轮向仍需核对", "assistant")
    entry = trial()
    pending = journal.finish("completed", "阶段完成", [entry], {"complete": True, "raw_dropped": 0})
    assert pending["status"] == "completed"
    entry["samples"][0]["signed_left_speed_mps"] = 999
    wait_for(lambda: journal.snapshot()["finished"] and not journal.is_running)
    result = journal.snapshot()
    assert result["failure"] is None
    assert "rows" not in result["summary"]
    assert result["summary"]["metrics"]["left"]["success_count"] == 1
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["rows"][0]["left_feedback"] == "sustained"
    assert json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))["name"] == "测试计划"
    assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))["caps"]["pwm_limit"] == 6000
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["kind"] == "journal_ready" and events[-1]["kind"] == "finish"
    assert any(event["fields"].get("raw_frame") == "A5 5A" for event in events if "fields" in event)
    with (tmp_path / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["pwm_request_id"] == "pwm-1" and row["pwm_seq"] == "3"
    report_before = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "不是物理启动时间" in report_before and "不累加为里程" in report_before
    assert "补充观察不回写本报告" in report_before
    assert "纯软件假设备" in report_before and "不是实车证据" in report_before
    assert "原始 HELLO/CAPS 握手日志：未覆盖" in report_before
    assert "开始于既有连接之后" in report_before
    assert "ARM ACK|PWM ACK|STOP ACK" in report_before
    assert "|left-t001|accepted|accepted|未发送（计划到期）|" in report_before
    assert "已观测非零 PWM" in report_before and "到期失能" in report_before
    assert "人工观察：未记录" in report_before
    assert "助手观察：目视轮向仍需核对" in report_before
    ET.parse(tmp_path / "speed_curve.svg")
    assert journal.observation("left-t001", "用户确认实际已停稳", "human")
    wait_for(lambda: not journal.is_running)
    notes = [json.loads(line) for line in (tmp_path / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [note["source"] for note in notes] == ["assistant", "human"]
    assert notes[-1]["after_report"] is True
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == report_before
    assert "用户确认实际已停稳" not in report_before
    assert not journal.event("late")


def test_report_distinguishes_ack_unknown_output_and_observation_sources(tmp_path):
    journal = ExperimentJournal(tmp_path, plan(), "report-evidence", "left", {
        "source": "BLUETOOTH_SPP", "software_demo": False, "raw_handshake_covered": True})
    wait_for(lambda: journal.ready)
    assert journal.observation("left-t001", "用户目视轮向尚未确认", "human")
    assert journal.observation("left-t001", "根据 STATE 观察到软件 PWM", "assistant")
    entry = trial()
    entry.update(stop_after_ms=250, stop_seq=4, stop_request_id="stop-1", status="aborted",
                 reason="STOP ACK 超时", stop_confirmed=False,
                 ack_results={"arm": "accepted", "pwm": "accepted", "stop": "unknown"})
    journal.finish("aborted", entry["reason"], [entry], {})
    wait_for(lambda: journal.snapshot()["finished"] and not journal.is_running)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "设备链路记录" in report and "原始 HELLO/CAPS 握手日志：已覆盖" in report
    assert "|left-t001|accepted|accepted|unknown|" in report
    assert "主动 STOP，PWM 请求后 250 ms" in report
    assert "人工观察：用户目视轮向尚未确认" in report
    assert "助手观察：根据 STATE 观察到软件 PWM" in report
    assert "accepted 不证明已产生输出或实际转动" in report


def test_finish_preserves_aborted_partial_trial(tmp_path):
    journal = ExperimentJournal(tmp_path, plan(), "run-2", "left", {})
    entry = trial((0.0,))
    entry.update(status="aborted", reason="连接已断开", stop_confirmed=False)
    journal.finish("aborted", "连接已断开", [entry], {"complete": False})
    wait_for(lambda: journal.snapshot()["finished"])
    row = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["rows"][0]
    assert row["status"] == "aborted" and row["left_feedback"] == "insufficient"
    assert row["stop_confirmed"] is False


def test_summary_success_denominator_excludes_insufficient_and_aborted(tmp_path):
    journal = ExperimentJournal(tmp_path, plan(), "counts", "left", {})
    entries = [trial(), trial((0.0,) * 4), trial((0.051,) * 3), trial()]
    entries[-1]["status"] = "aborted"
    journal.finish("aborted", "中止", entries, {})
    wait_for(lambda: journal.snapshot()["finished"])
    metrics = journal.snapshot()["summary"]["metrics"]
    assert metrics["left"]["success_count"] == 1
    assert metrics["left"]["failure_count"] == 1
    assert metrics["left"]["valid_trial_count"] == 2
    assert metrics["left"]["success_rate"] == 0.5
    assert metrics["left"]["incomplete_count"] == 1
    assert metrics["left"]["insufficient_count"] == 1
    assert metrics["right"]["valid_trial_count"] == 0
    assert metrics["right"]["success_rate"] is None


def test_bounded_overflow_is_visible_and_finish_never_blocks_writer(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = ExperimentJournal._write_document

    def slow_write(self, key, value):
        if key == "plan":
            entered.set()
            assert release.wait(3)
        return original(self, key, value)

    monkeypatch.setattr(ExperimentJournal, "_write_document", slow_write)
    journal = ExperimentJournal(tmp_path, plan(), "run-3", "left", {}, queue_capacity=1)
    assert entered.wait(1)
    assert not journal.ready
    assert journal.event("first")
    assert not journal.event("second")
    assert "溢出" in journal.failure
    started = time.monotonic()
    journal.finish("aborted", "日志溢出", [], {"complete": False})
    assert time.monotonic() - started < 0.2
    assert not journal.snapshot()["finished"]
    release.set()
    wait_for(lambda: journal.snapshot()["finished"])
    assert journal.snapshot()["summary"]["failure"] == journal.failure


def test_io_failure_and_oversized_samples_are_visible(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.write_text("existing", encoding="utf-8")
    journal = ExperimentJournal(occupied, plan(), "run-4", "left", {})
    wait_for(lambda: journal.snapshot()["finished"])
    assert journal.failure and not journal.ready
    assert journal.snapshot()["summary"] is None
    journal = ExperimentJournal(tmp_path / "large", plan(), "run-5", "left", {})
    entry = trial()
    entry["samples"] = [entry["samples"][0]] * 257
    journal.finish("completed", "", [entry], {})
    wait_for(lambda: journal.snapshot()["finished"])
    assert "256" in journal.failure
    assert journal.snapshot()["summary"]["status"] == "failed"


def test_observation_validation_and_limit(tmp_path):
    journal = ExperimentJournal(tmp_path, plan(), "run-6", "left", {})
    wait_for(lambda: journal.ready)
    for source, text in (("device", "观察"), ("human", ""), ("assistant", "x" * 2001)):
        assert not journal.observation("left-t001", text, source)
    for _ in range(128):
        assert journal.observation("left-t001", "人工观察", "human")
    assert not journal.observation("left-t001", "第129条", "human")
    journal.finish("completed", "", [], {})
    wait_for(lambda: journal.snapshot()["finished"] and not journal.is_running)
    assert len((tmp_path / "observations.jsonl").read_text(encoding="utf-8").splitlines()) == 128
