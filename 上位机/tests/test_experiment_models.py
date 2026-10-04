from copy import deepcopy

import pytest

from car_host.experiment_models import build_plan, get_templates, validate_against_caps


def draft():
    return {"name": "批准的开环计划", "stages": [{"stage_id": "left", "name": "左轮",
            "steps": [{"left_pwm": 850, "right_pwm": 0, "duration_ms": 500,
                       "repetitions": 2}]}]}


def test_snapshot_expansion_digest_and_detachment():
    raw = draft()
    plan = build_plan(raw)
    assert plan["trial_count"] == 2
    assert [t["trial_id"] for t in plan["stages"][0]["trials"]] == ["left-t001", "left-t002"]
    assert len(plan["digest"]) == 64 and plan["plan_id"] == plan["digest"][:24]
    assert build_plan(deepcopy(raw)) == plan
    raw["stages"][0]["steps"][0]["left_pwm"] = 900
    assert plan["stages"][0]["trials"][0]["left_pwm"] == 850
    assert build_plan(raw)["digest"] != plan["digest"]


@pytest.mark.parametrize("field,value", [("left_pwm", True), ("right_pwm", 6001),
    ("left_pwm", -6001), ("left_pwm", None), ("duration_ms", False),
    ("duration_ms", 0), ("duration_ms", 1001), ("repetitions", 101),
    ("repetitions", 0), ("repetitions", True), ("stop_after_ms", 500),
    ("stop_after_ms", 0), ("stop_after_ms", True), ("label", "换行\n文字")])
def test_rejects_invalid_values(field, value):
    raw = draft()
    raw["stages"][0]["steps"][0][field] = value
    with pytest.raises(ValueError):
        build_plan(raw)


def test_rejects_oversized_plan_duplicate_stages_and_unrecognized_actions():
    raw = draft()
    raw["stages"][0]["steps"] *= 65
    with pytest.raises(ValueError, match="总试次"):
        build_plan(raw)
    raw = draft()
    raw["stages"] *= 2
    with pytest.raises(ValueError, match="不能重复"):
        build_plan(raw)
    raw = draft()
    raw["stages"][0]["steps"][0]["set_speed"] = 100
    with pytest.raises(ValueError, match="不受支持"):
        build_plan(raw)
    raw = draft()
    raw["stages"] = [dict(stage_id=f"s{i}", name="阶段", steps=deepcopy(raw["stages"][0]["steps"]))
                     for i in range(17)]
    with pytest.raises(ValueError):
        build_plan(raw)


def test_caps_validation_and_snapshot_tampering():
    plan = build_plan(draft())
    validate_against_caps(plan, {"pwm_limit": 6000, "max_duration_ms": 1000})
    plan["plan_id"] = "7c5b8fb6d39e448392806760314c7f6d"
    validate_against_caps(plan, {"pwm_limit": 6000, "max_duration_ms": 1000})
    for limits in ({"pwm_limit": 800, "max_duration_ms": 1000},
                   {"pwm_limit": 6000, "max_duration_ms": 400},
                   {"pwm_limit": True, "max_duration_ms": 1000}, {}):
        with pytest.raises(ValueError):
            validate_against_caps(plan, limits)
    plan["stages"][0]["trials"][0]["left_pwm"] = 900
    with pytest.raises(ValueError, match="摘要"):
        validate_against_caps(plan, {"pwm_limit": 6000, "max_duration_ms": 1000})


def test_templates_are_detached_unapproved_drafts():
    templates = get_templates()
    assert len(templates) == 6
    for template in templates:
        with pytest.raises(ValueError):
            build_plan(template)
    templates[0]["stages"][0]["steps"][0]["left_pwm"] = 6000
    assert get_templates()[0]["stages"][0]["steps"][0]["left_pwm"] is None
