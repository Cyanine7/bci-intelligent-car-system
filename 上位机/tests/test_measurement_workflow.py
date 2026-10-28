"""Real workflow + software device; never creates a hardware link."""

from copy import deepcopy
import csv
import json
from pathlib import Path
import time

import pytest

from car_host.automation import AutomationService
from car_host.experiment_models import build_plan, get_templates, plan_to_draft, validate_against_caps
from car_host.measurement_analysis import compare_controls, summarize_profile, verify_raw_recording, recompute_run
from car_host.measurement_suite import check_suite, finish_suite, read_suite, reserve_suite, suggest_refinement, makeup_draft
from car_host.experiment_recording import ExperimentJournal
from car_host.project_protocol import ARM, SET_PWM, SET_PWM_PROFILE, STOP
from test_automation import fake_host, command_kinds


def profile_draft(*, blocks=1, rest_ms=0, repetitions=1, budget=128, suite_id="workflow", controls=False):
    steps = [dict(segments=[dict(left_pwm=1400, right_pwm=0, duration_ms=400),
                           dict(left_pwm=900, right_pwm=0, duration_ms=600)],
                  control_type="candidate", repetitions=repetitions, label="600ms finite window")]
    if controls:
        for kind, pwm in (("high_hold", 1400), ("zero_output", 0)):
            step = deepcopy(steps[0])
            step["control_type"] = kind
            step["segments"][1]["left_pwm"] = pwm
            steps.append(step)
    return dict(schema_version=2, name="Finite profile software verification",
        suite=dict(suite_id=suite_id, total_entered_budget=budget), stages=[
        dict(stage_id=f"b{i + 1}", name=f"block {i + 1}", block_id=f"B{i + 1}",
             rest_before_ms=rest_ms if i else 0, steps=deepcopy(steps if i % 2 == 0 else list(reversed(steps))))
        for i in range(blocks)])


@pytest.fixture
def profile_service(fake_host, tmp_path):
    host, clock = fake_host
    result = host.send_command("query_profile_caps")
    assert result.success
    for _ in range(4):
        clock.advance()
        host.poll()
    assert host.capabilities.pwm_profile
    service = AutomationService(host, tmp_path, clock=clock, demo=True)
    service.timer.stop()
    return host, clock, service


def launch(service, raw, *, local=True):
    dispatch = service.dispatch_local if local else lambda m, p: service.dispatch(m, p, "test-client")
    preview = dispatch("preview_plan", {"plan": raw})
    assert "error" not in preview, preview
    params = dict(plan_id=preview["plan_id"], plan_digest=preview["digest"],
                  approval_text="真实授权原话的纯软件夹具；已经架空就绪", ready_confirmed=True)
    result = dispatch("start_plan", params)
    assert "run_id" in result, result
    return preview, params, result


def drive(service_tuple, predicate=lambda s: not s.active, timeout=8):
    host, clock, service = service_tuple
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate(service):
            return
        if service.status().get("owner_kind") == "mcp":
            service.dispatch("heartbeat", {}, "test-client")
        if service.status()["phase"] in ("journal_ready", "finalizing", "reporting"):
            host.poll()
            service.tick()
            time.sleep(.002)
        else:
            clock.advance(.2 if service.status()["phase"] == "resting" else .05)
            host.poll()
            service.tick()
            time.sleep(.0005)
    assert predicate(service), service.status()


def get_saved(service):
    result = service.dispatch_local("get_results", {})
    return result, json.loads(Path(result["artifacts"]["summary"]).read_text(encoding="utf-8"))


def test_v2_three_blocks_rest_profiles_zero_control_raw_verification(profile_service):
    host, clock, service = profile_service
    preview, _, started = launch(service, profile_draft(blocks=3, rest_ms=60_000,
        repetitions=3, budget=40, controls=True))
    drive(profile_service)
    result, summary = get_saved(service)
    assert result["status"] == "completed", result
    assert summary["independent_verification"]["verified"]
    assert summary["planned_count"] == summary["entered_count"] == summary["completed_count"] == 27
    assert not summary["cancelled_slots"]
    rows = summary["rows"]
    assert {r["block_id"] for r in rows} == {"B1", "B2", "B3"}
    assert all(r["profile_segment_observed"] for r in rows)
    assert all(r["segment_observations"][1]["sample_count"] >= 4 for r in rows)
    zero_rows = [r for r in rows if r["control_type"] == "zero_output"]
    assert len(zero_rows) == 9
    assert all(r["profile_metrics"]["left"]["maintenance"] == "window_not_sustained" for r in zero_rows)
    assert all(r["profile_metrics"]["right"]["maintenance"] == "not_tested" for r in rows)
    events = [json.loads(line) for line in Path(result["artifacts"]["events"]).read_text(encoding="utf-8").splitlines()]
    rests = [e["fields"] for e in events if e["kind"] == "rest_completed"]
    assert len(rests) == 2 and all(r["actual_ms"] >= 60_000 for r in rests)
    assert command_kinds(host.worker).count(SET_PWM_PROFILE) == 27
    assert command_kinds(host.worker).count(ARM) == 27
    assert command_kinds(host.worker).count(SET_PWM) == 0
    ledger = read_suite(host.data_directory / "suites" / "workflow")
    assert ledger["entered_count"] == 27 and ledger["remaining_budget"] == 13
    assert Path(result["artifacts"]["segments"]).exists()
    assert preview["digest"] == summary["digest"] and started["run_id"] == summary["run_id"]
    svg = Path(result["artifacts"]["curve"]).read_text(encoding="utf-8")
    assert "device tick" in svg and "stroke-dasharray" in svg


def test_stop_during_rest_cancels_remaining_and_new_run_preserves_aborted_index(profile_service):
    host, clock, service = profile_service
    launch(service, profile_draft(blocks=3, rest_ms=60_000, budget=6))
    drive(profile_service, lambda s: s.status()["phase"] == "resting")
    before = command_kinds(host.worker).count(ARM)
    clock.advance(.2)
    host.poll()
    service.stop("human STOP during rest")
    drive(profile_service)
    result, summary = get_saved(service)
    assert result["status"] == "aborted"
    assert summary["entered_count"] == summary["completed_count"] == 1
    assert len(summary["cancelled_slots"]) == 2
    assert command_kinds(host.worker).count(ARM) == before == 1
    old_run = summary["run_id"]
    second = profile_draft(budget=6)
    second["stages"][0]["steps"][0]["origin_slot"] = old_run + "/b2-t001"
    launch(service, second)
    drive(profile_service)
    ledger = read_suite(host.data_directory / "suites" / "workflow")
    assert len(ledger["runs"]) == 2 and ledger["runs"][0]["status"] == "aborted"
    assert ledger["entered_count"] == ledger["completed_count"] == 2
    assert ledger["cancelled_count"] == 2 and ledger["remaining_budget"] == 4
    _, second_summary = get_saved(service)
    assert second_summary["rows"][0]["origin_slot"] == old_run + "/b2-t001"


def test_stop_in_profile_preserves_entered_aborted_and_invalidates_old_start(profile_service):
    host, clock, service = profile_service
    _, params, _ = launch(service, profile_draft(repetitions=3, budget=3))
    drive(profile_service, lambda s: s.status()["phase"] == "moving")
    service.stop("active STOP")
    drive(profile_service)
    _, summary = get_saved(service)
    assert summary["entered_count"] == summary["aborted_count"] == 1
    assert summary["completed_count"] == 0 and len(summary["cancelled_slots"]) == 2
    before = len(host.worker.sent)
    assert "error" in service.dispatch_local("start_plan", params)
    assert len(host.worker.sent) == before
    ledger = read_suite(host.data_directory / "suites" / "workflow")
    assert ledger["entered_count"] == 1 and ledger["remaining_budget"] == 2


def test_local_owner_has_no_mcp_heartbeat_and_bystander_disconnect_is_harmless(profile_service):
    host, clock, service = profile_service
    launch(service, profile_draft(blocks=2, rest_ms=4000))
    drive(profile_service, lambda s: s.status()["phase"] == "resting")
    service.dispatch("get_status", {}, "bystander")
    service.client_lost("bystander")
    assert service.active and service.status()["owner_kind"] == "local_ui"
    assert not host.send_command("arm_pwm").success
    drive(profile_service)
    assert service.status()["phase"] == "completed"


def test_old_caps_reject_profile_without_recording_or_motion(fake_host, tmp_path):
    host, clock = fake_host
    service = AutomationService(host, tmp_path, clock=clock, demo=True)
    service.timer.stop()
    host.worker.set_fault("profile_unsupported", True)
    preview = service.dispatch_local("preview_plan", {"plan": profile_draft()})
    for _ in range(4):
        clock.advance()
        host.poll()
    before = len(host.worker.sent)
    result = service.dispatch_local("start_plan", dict(plan_id=preview["plan_id"], plan_digest=preview["digest"],
        approval_text="software fixture", ready_confirmed=True))
    assert "error" in result and "PROFILE_CAPS" in result["error"]
    assert len(host.worker.sent) == before and not host.recorder.is_running


@pytest.mark.parametrize("mutation", ["bool_pwm", "reverse", "passive_second", "mixed", "too_long", "unknown", "budget", "digest"])
def test_v2_validation_and_freeze_rejects_ambiguous_or_unsafe_tables(mutation):
    raw = profile_draft()
    step = raw["stages"][0]["steps"][0]
    if mutation == "bool_pwm":
        step["segments"][0]["left_pwm"] = True
    elif mutation == "reverse":
        step["segments"][1]["left_pwm"] = -10
    elif mutation == "passive_second":
        step["segments"][1]["right_pwm"] = 10
    elif mutation == "mixed":
        step["left_pwm"] = 100
    elif mutation == "too_long":
        step["segments"][1]["duration_ms"] = 1601
    elif mutation == "unknown":
        step["segments"][0]["timeout"] = 1000
    elif mutation == "budget":
        raw["suite"]["total_entered_budget"] = None
    if mutation == "digest":
        plan = build_plan(raw)
        plan["stages"][0]["trials"][0]["segments"][1]["left_pwm"] = 1000
        with pytest.raises(ValueError):
            validate_against_caps(plan, dict(pwm_limit=6000, max_duration_ms=1000,
                profile_version=1, profile_max_segments=2, profile_pwm_limit=6000, profile_max_total_ms=1000))
    else:
        with pytest.raises(ValueError):
            build_plan(raw)


def test_v2_round_trip_v1_compat_and_caps_revalidation():
    raw = profile_draft(blocks=3, repetitions=3)
    frozen = build_plan(raw)
    assert build_plan(plan_to_draft(frozen))["digest"] == frozen["digest"]
    raw["stages"][0]["steps"][0]["segments"][1]["left_pwm"] = 1234
    assert frozen["stages"][0]["trials"][0]["segments"][1]["left_pwm"] == 900
    caps = dict(pwm_limit=6000, max_duration_ms=1000, profile_version=1,
                profile_max_segments=2, profile_pwm_limit=1500, profile_max_total_ms=1000)
    validate_against_caps(frozen, caps)
    with pytest.raises(ValueError):
        validate_against_caps(frozen, {**caps, "profile_pwm_limit": 1300})
    legacy = dict(name="legacy", stages=[dict(stage_id="a", name="a",
        steps=[dict(left_pwm=1000, right_pwm=0, duration_ms=500)])])
    old = build_plan(legacy)
    assert "schema_version" not in old and build_plan(plan_to_draft(old)) == old


def test_108_trial_template_is_unapproved_and_has_each_side_direction_controls():
    template = get_templates()[-1]
    assert template["suite"]["total_entered_budget"] is None
    assert len(template["stages"]) == 3
    assert [s["rest_before_ms"] for s in template["stages"]] == [0, 60_000, 60_000]
    with pytest.raises(ValueError):
        build_plan(template)
    template["suite"]["total_entered_budget"] = 108
    for stage in template["stages"]:
        for step in stage["steps"]:
            side = "left" if step["segments"][0]["left_pwm"] is None else "right"
            direction = -1 if "反向" in step["label"] else 1
            startup = 1400 if side == "left" else 1600
            step["segments"][0][f"{side}_pwm"] = direction * startup
            if step["control_type"] != "zero_output":
                step["segments"][1][f"{side}_pwm"] = direction * (startup if step["control_type"] == "high_hold" else 900)
    plan = build_plan(template)
    assert plan["trial_count"] == 108
    for stage in plan["stages"]:
        assert len(stage["trials"]) == 36
        for side in ("left", "right"):
            for direction in (-1, 1):
                for kind in ("candidate", "high_hold", "zero_output"):
                    assert sum(t["segments"][0][f"{side}_pwm"] * direction > 0 and t["control_type"] == kind for t in stage["trials"]) == 3
    assert get_templates()[-1]["suite"]["total_entered_budget"] is None


def test_profile_caps_limit_does_not_shrink_unrelated_fixed_pwm_contract():
    raw = profile_draft()
    raw["stages"][0]["steps"].append(dict(left_pwm=2000, right_pwm=0, duration_ms=1000))
    caps = dict(pwm_limit=6000, max_duration_ms=1000, profile_version=1,
        profile_max_segments=2, profile_pwm_limit=1500, profile_max_total_ms=1000)
    validate_against_caps(build_plan(raw), caps)
    for kind in ("high_hold", "zero_output"):
        altered = profile_draft()
        altered["stages"][0]["steps"][0]["control_type"] = kind
        with pytest.raises(ValueError):
            build_plan(altered)


def test_suite_crashed_reservation_and_budget_change_are_not_auto_resumed(tmp_path):
    plan = build_plan(profile_draft(budget=3))
    suite = plan["suite"]
    slots = [dict(trial_id="a"), dict(trial_id="b")]
    reserve_suite(tmp_path, suite, "old", plan, slots, {})
    with pytest.raises(ValueError, match="剩余"):
        check_suite(tmp_path, suite, 2)
    with pytest.raises(ValueError, match="预算不可改写"):
        check_suite(tmp_path, {**suite, "total_entered_budget": 4}, 1)
    finish_suite(tmp_path, "old", [dict(trial_id="a", status="aborted")], "aborted")
    check_suite(tmp_path, suite, 2)
    saved = read_suite(tmp_path)
    assert saved["entered_count"] == saved["aborted_count"] == 1
    assert saved["cancelled_count"] == 1 and saved["reserved_count"] == 0


def synthetic_trial(*, startup=True, second_speed=.1, missing=False, clipped=False,
                    short=False, control="candidate", hold_pwm=900, start_tick=1000):
    trial = dict(trial_id="t", status="completed", connection_id="c", session=7, profile_seq=3,
        duration_ms=1000, deadline_tick=(start_tick + (800 if clipped else 1000)) & 0xffffffff,
        block_id="B1", control_type=control,
        segments=[dict(left_pwm=1400, right_pwm=0, duration_ms=400),
                  dict(left_pwm=hold_pwm, right_pwm=0, duration_ms=600)], samples=[])
    for offset in range(50, 1000, 50):
        segment = int(offset >= 400)
        if segment and (missing or short and offset < 800):
            continue
        speed = .12 if startup else 0
        if segment:
            speed = second_speed
        trial["samples"].append(dict(connection_id="c", device_session=7, profile_seq=3,
            device_time_ms=(start_tick + offset) & 0xffffffff, frame_seq=offset // 50,
            profile_start_tick=start_tick, segment1_actual_tick=(start_tick + 400) & 0xffffffff if segment else 0,
            segment_index=segment, profile_status=1, armed=True, control_mode=1,
            pwm_left=1400 if not segment else hold_pwm, pwm_right=0,
            signed_left_speed_mps=speed, signed_right_speed_mps=0, quality=()))
    return trial


@pytest.mark.parametrize("options,expected", [
    ({"startup": False}, "startup_not_established"),
    ({"second_speed": 0}, "window_not_sustained"),
    ({"second_speed": .1}, "window_sustained"),
    ({"missing": True}, "segment_missing"),
    ({"clipped": True}, "deadline_clipped"),
    ({"short": True}, "insufficient"),
])
def test_profile_classification_distinguishes_no_start_zero_short_clipped(options, expected):
    trial = synthetic_trial(**options)
    summary = summarize_profile(trial)
    assert summary["profile_metrics"]["left"]["maintenance"] == expected
    assert summary["profile_metrics"]["right"]["maintenance"] == "not_tested"


def test_same_pwm_segments_wrap_and_zero_output_remain_distinguishable():
    hold = summarize_profile(synthetic_trial(hold_pwm=1400, control="high_hold", start_tick=0xfffffe00))
    assert hold["profile_segment_observed"] and hold["profile_metrics"]["left"]["maintenance"] == "window_sustained"
    assert hold["segment_observations"][1]["actual_start_offset_ms"] == 400
    zero = summarize_profile(synthetic_trial(hold_pwm=0, control="zero_output", second_speed=0))
    assert zero["segment_observations"][1]["sample_count"] == 12
    assert zero["profile_metrics"]["left"]["maintenance"] == "window_not_sustained"


def test_late_active_stop_is_interruption_even_when_tail_and_coverage_pass():
    trial = synthetic_trial()
    trial["status"] = "aborted"
    terminal = {**trial["samples"][-1], "profile_status": 2, "armed": False,
                "pwm_left": 0, "device_time_ms": 1970, "stop_reason": 1}
    trial["samples"].append(terminal)
    summary = summarize_profile(trial)
    assert summary["segment_observations"][1]["observation_sufficient"]
    assert summary["profile_metrics"]["left"]["tail4_compatibility"] == "sustained"
    assert summary["profile_metrics"]["left"]["maintenance"] == "stopped_early"
    assert summary["profile_stop_truncated"] and summary["profile_termination_reason"] == 1


def test_conflicting_segment_tick_evidence_cannot_publish_maintenance_success():
    trial = synthetic_trial()
    trial["samples"][-1]["segment1_actual_tick"] += 10
    summary = summarize_profile(trial)
    assert summary["profile_evidence_conflict"]
    assert summary["profile_metrics"]["left"]["maintenance"] == "insufficient"


def test_control_comparison_does_not_call_inertia_maintenance():
    def row(kind, hold, speed):
        trial = synthetic_trial(control=kind, hold_pwm=hold, second_speed=speed)
        return {**trial, **summarize_profile(trial)}
    candidate = row("candidate", 900, .1)
    rows = [candidate, *[row("high_hold", 1400, .12) for _ in range(3)],
            *[row("zero_output", 0, .11) for _ in range(3)]]
    compare_controls(rows)
    assert candidate["profile_metrics"]["left"]["inertia_conclusion"] == "inertia_not_excluded"
    rows = [row("candidate", 900, .1), *[row("high_hold", 1400, .12) for _ in range(3)],
            *[row("zero_output", 0, 0) for _ in range(3)]]
    compare_controls(rows)
    assert rows[0]["profile_metrics"]["left"]["inertia_conclusion"] == "exceeds_zero_control_observations"
    rows.pop()
    compare_controls(rows)
    assert rows[0]["profile_metrics"]["left"]["inertia_conclusion"] == "control_insufficient"


def refinement_rows(levels=(800, 900), mixed=False):
    rows = []
    for block in range(3):
        for level in levels:
            for repeat in range(3):
                trial = synthetic_trial(hold_pwm=level, second_speed=.1 if level >= 900 else 0)
                trial["block_id"] = f"B{block + 1}"
                trial["trial_id"] = f"b{block}p{level}r{repeat}"
                row = {**trial, **summarize_profile(trial)}
                row["profile_metrics"]["left"]["inertia_conclusion"] = "exceeds_zero_control_observations" if level >= 900 else "not_sustained_in_window"
                if mixed and block == 1 and level == 900 and repeat == 0:
                    row["profile_metrics"]["left"]["maintenance"] = "window_not_sustained"
                rows.append(row)
    return rows


def test_refinement_is_new_bounded_draft_with_controls_anchors_and_no_execution():
    suite = dict(suite_id="suite", total_entered_budget=200)
    suggested = suggest_refinement(refinement_rows(), side="left", direction=1, suite=suite, remaining_budget=128)
    assert suggested["status"] == "draft" and suggested["step_pwm"] == 50
    assert suggested["auto_execute"] is False and suggested["interval"] == [800, 900]
    plan = build_plan(suggested["draft"])
    assert plan["trial_count"] == suggested["required_trials"] == 45
    assert all(s["rest_before_ms"] == 60_000 for s in plan["stages"])
    assert {t["control_type"] for s in plan["stages"] for t in s["trials"]} == {"candidate", "high_hold", "zero_output"}
    limited = suggest_refinement(refinement_rows(), side="left", direction=1, suite=suite, remaining_budget=44)
    assert limited["status"] == "budget_limited" and limited["draft"] is None
    mixed = suggest_refinement(refinement_rows(mixed=True), side="left", direction=1, suite=suite, remaining_budget=128)
    assert mixed["status"] == "undetermined" and mixed["draft"] is None
    narrowed = suggest_refinement(refinement_rows((850, 900)), side="left", direction=1, suite=suite, remaining_budget=128)
    assert narrowed["step_pwm"] == 25


def test_refinement_all_success_or_all_failure_does_not_invent_single_threshold():
    suite = dict(suite_id="suite", total_entered_budget=200)
    success = suggest_refinement(refinement_rows((900, 1000)), side="left", direction=1, suite=suite, remaining_budget=128)
    assert success["status"] == "one_sided" and success["draft"] is None
    failure = suggest_refinement(refinement_rows((700, 800)), side="left", direction=1, suite=suite, remaining_budget=128)
    assert failure["status"] == "undetermined" and failure["draft"] is None


def test_refinement_no_inertia_control_or_split_sessions_does_not_make_n3():
    suite = dict(suite_id="suite", total_entered_budget=200)
    rows = refinement_rows()
    for row in rows:
        row["profile_metrics"]["left"]["inertia_conclusion"] = "inertia_not_excluded"
    unresolved = suggest_refinement(rows, side="left", direction=1, suite=suite, remaining_budget=128)
    assert unresolved["draft"] is None and unresolved["status"] == "undetermined"


def test_refinement_nonmonotonic_or_25_interval_stays_an_interval():
    suite = dict(suite_id="suite", total_entered_budget=200)
    rows = refinement_rows((800, 900, 1000))
    for row in rows:
        if row["segments"][1]["left_pwm"] == 1000:
            row["profile_metrics"]["left"].update(maintenance="window_not_sustained", inertia_conclusion="not_sustained_in_window")
    unresolved = suggest_refinement(rows, side="left", direction=1, suite=suite, remaining_budget=128)
    assert unresolved["status"] == "undetermined" and unresolved["draft"] is None
    interval = suggest_refinement(refinement_rows((875, 900)), side="left", direction=1, suite=suite, remaining_budget=128)
    assert interval["status"] == "interval" and interval["interval"] == [875, 900]
    rows = refinement_rows()
    for index, row in enumerate(rows):
        row["session"] = 100 + index % 3
    unresolved = suggest_refinement(rows, side="left", direction=1, suite=suite, remaining_budget=128)
    assert unresolved["draft"] is None and unresolved["status"] == "undetermined"


def test_independent_reconstruction_detects_csv_corruption_without_touching_original(profile_service, tmp_path):
    host, clock, service = profile_service
    launch(service, profile_draft())
    drive(profile_service)
    result, summary = get_saved(service)
    directory = Path(result["artifacts"]["summary"]).parent
    assert verify_raw_recording(directory, service._run["trials"])["verified"]
    import shutil
    copy = tmp_path / "derived_corrupt_fixture"
    copy.mkdir()
    shutil.copyfile(directory / "communication.log", copy / "communication.log")
    with (directory / "telemetry.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    rows[0]["pwm_left"] = "5999"
    with (copy / "telemetry.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    check = verify_raw_recording(copy, [])
    assert not check["verified"] and check["decoded_field_mismatches"] == 1
    assert summary["independent_verification"]["verified"]
    independent = recompute_run(directory)
    assert independent["verified"] and independent["classification_mismatch_count"] == 0
    assert Path(result["artifacts"]["recompute"]).exists()
    bad_saved = deepcopy(summary["rows"])
    bad_saved[0]["profile_metrics"]["left"]["maintenance"] = "window_not_sustained" if bad_saved[0]["profile_metrics"]["left"]["maintenance"] != "window_not_sustained" else "window_sustained"
    mismatch = recompute_run(directory, saved_rows=bad_saved, save=False)
    assert not mismatch["verified"] and mismatch["raw_verification"]["verified"]
    assert mismatch["classification_mismatch_count"] == 1


def test_raw_recording_corruption_prevents_completed_publication(profile_service):
    host, clock, service = profile_service

    class CorruptingJournal(ExperimentJournal):
        def _write_report(self, payload):
            # Recorder has closed: reproduce disk corruption in a software fixture.
            path = self.directory / "telemetry.csv"
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fields, rows = reader.fieldnames, list(reader)
            rows[0]["pwm_left"] = "5999"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            super()._write_report(payload)

    service.journal_factory = CorruptingJournal
    launch(service, profile_draft())
    drive(profile_service)
    result, summary = get_saved(service)
    assert service.status()["phase"] == result["status"] == summary["status"] == "failed"
    assert result["failure"] and not summary["independent_verification"]["verified"]
    assert host.control_owner is None
    ledger = read_suite(host.data_directory / "suites" / "workflow")
    assert ledger["runs"][0]["status"] == "failed" and ledger["entered_count"] == 1


def test_suite_index_tampering_and_terminal_rewrite_do_not_reopen_budget(tmp_path):
    plan = build_plan(profile_draft(budget=3))
    reserve_suite(tmp_path, plan["suite"], "old", plan, [dict(trial_id="a")], {})
    with pytest.raises(ValueError, match="不能重复"):
        reserve_suite(tmp_path, plan["suite"], "old", plan, [dict(trial_id="a")], {})
    finish_suite(tmp_path, "old", [dict(trial_id="a", status="aborted")], "aborted")
    with pytest.raises(ValueError, match="不可再次改写"):
        finish_suite(tmp_path, "old", [], "completed")
    saved = read_suite(tmp_path)
    saved["runs"][0]["entered_count"] = -1
    (tmp_path / "index.json").write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="预算、身份或槽位"):
        check_suite(tmp_path, plan["suite"], 3)


def test_makeup_draft_keeps_aborted_cancelled_order_rest_controls_and_origin():
    plan = build_plan(profile_draft(blocks=3, rest_ms=60_000, repetitions=1, controls=True, budget=20))
    original = deepcopy(plan)
    complete = plan["stages"][0]["trials"][0]
    aborted = plan["stages"][0]["trials"][1]
    failed = plan["stages"][1]["trials"][0]
    evidence = [dict(trial_id=complete["trial_id"], status="completed", left_feedback="not_sustained"),
                dict(trial_id=aborted["trial_id"], status="aborted"),
                dict(trial_id=failed["trial_id"], status="failed")]
    result = makeup_draft(plan, "source_run", evidence, remaining_budget=19)
    assert result["status"] == "draft" and result["required_trials"] == 8
    new_plan = build_plan(result["draft"])
    assert new_plan["digest"] != plan["digest"] and new_plan["suite"] == plan["suite"]
    assert [s["rest_before_ms"] for s in new_plan["stages"]] == [0, 60_000, 60_000]
    assert {s["stage_id"] for s in plan["stages"]}.isdisjoint(s["stage_id"] for s in new_plan["stages"])
    assert {s["block_id"] for s in plan["stages"]}.isdisjoint(s["block_id"] for s in new_plan["stages"])
    expected = [t for s in plan["stages"] for t in s["trials"] if t["trial_id"] != complete["trial_id"]]
    actual = [t for s in new_plan["stages"] for t in s["trials"]]
    assert [t["origin_slot"] for t in actual] == ["source_run:" + t["trial_id"] for t in expected]
    assert [t["segments"] for t in actual] == [t["segments"] for t in expected]
    assert [t["control_type"] for t in actual] == [t["control_type"] for t in expected]
    assert plan == original and evidence[0]["left_feedback"] == "not_sustained"


def test_makeup_draft_budget_all_or_none_empty_and_v1_needs_explicit_suite():
    plan = build_plan(profile_draft(blocks=3, controls=True, budget=20))
    completed_ids = [t["trial_id"] for s in plan["stages"] for t in s["trials"]]
    limited = makeup_draft(plan, "source_run", completed_ids[:1], remaining_budget=7)
    assert limited == dict(status="budget_limited", reason="全部补测需要8次，剩余7次；不生成部分表", draft=None, required_trials=8)
    empty = makeup_draft(plan, "source_run", completed_ids, remaining_budget=0)
    assert empty["status"] == "empty" and empty["draft"] is None and empty["required_trials"] == 0
    legacy = build_plan(dict(name="legacy", stages=[dict(stage_id="a", name="a",
        steps=[dict(left_pwm=1000, right_pwm=0, duration_ms=500)])]))
    missing = makeup_draft(legacy, "source_run", [], remaining_budget=128)
    assert missing["status"] == "needs_suite" and missing["draft"] is None and missing["required_trials"] == 1


def test_makeup_draft_rejects_unknown_evidence_and_tampered_frozen_plan():
    plan = build_plan(profile_draft())
    with pytest.raises(ValueError, match="来源试次身份"):
        makeup_draft(plan, "source_run", ["other-t001"], 128)
    with pytest.raises(ValueError, match="终结"):
        makeup_draft(plan, "source_run", [dict(trial_id="b1-t001", status="running")], 128)
    with pytest.raises(ValueError, match="整数"):
        makeup_draft(plan, "source_run", [], True)
    plan["stages"][0]["trials"][0]["segments"][0]["left_pwm"] = 1500
    with pytest.raises(ValueError, match="冻结摘要"):
        makeup_draft(plan, "source_run", [], 128)
