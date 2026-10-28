"""Validated, finite experiment plans; templates are unapproved drafts."""

from copy import deepcopy
import hashlib
import json
import re
import unicodedata
from .project_protocol import PWM_MAX_DURATION_MS


MAX_STAGES = 16
MAX_TRIALS = 128
MAX_REPETITIONS = 100
PWM_LIMIT = 10000
DURATION_LIMIT_MS = PWM_MAX_DURATION_MS
MAX_SUITE_TRIALS = 4096
MAX_REST_MS = 3_600_000
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")


def _integer(value, field, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{field} 必须是 {lower}..{upper} 内的整数（不能是 bool）")
    return value


def _text(value, field, maximum=80, empty=False):
    if not isinstance(value, str) or not (0 if empty else 1) <= len(value) <= maximum:
        raise ValueError(f"{field} 必须是长度不超过 {maximum} 的文字")
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError(f"{field} 不能包含控制或隐藏字符")
    if not empty and not value.strip():
        raise ValueError(f"{field} 不能为空白")
    return value


def _object(value, field, permitted):
    if not isinstance(value, dict) or any(k not in permitted for k in value):
        raise ValueError(f"{field} 格式或字段不受支持")
    return value


def _digest(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_plan(raw: dict) -> dict:
    """Expand a draft into a bounded JSON snapshot; never retain input references."""
    if isinstance(raw, dict) and raw.get("schema_version") == 2:
        return _build_v2(raw)
    raw = _object(raw, "计划", {"name", "stages"})
    name = _text(raw.get("name"), "计划名称")
    stages = raw.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
        raise ValueError(f"计划需要 1..{MAX_STAGES} 个阶段")
    expanded, used_ids, total = [], set(), 0
    for stage in stages:
        stage = _object(stage, "阶段", {"stage_id", "name", "steps"})
        stage_id = stage.get("stage_id")
        if not isinstance(stage_id, str) or not _TOKEN.fullmatch(stage_id):
            raise ValueError("stage_id 必须是 1..48 位字母、数字、下划线或短横线")
        if stage_id in used_ids:
            raise ValueError("stage_id 不能重复")
        used_ids.add(stage_id)
        stage_name = _text(stage.get("name"), "阶段名称")
        steps = stage.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_TRIALS:
            raise ValueError("阶段需要非空、有限的 steps 列表")
        trials = []
        for step in steps:
            step = _object(step, "步骤", {"left_pwm", "right_pwm", "duration_ms",
                                         "repetitions", "label", "stop_after_ms"})
            left = _integer(step.get("left_pwm"), "left_pwm", -PWM_LIMIT, PWM_LIMIT)
            right = _integer(step.get("right_pwm"), "right_pwm", -PWM_LIMIT, PWM_LIMIT)
            duration = _integer(step.get("duration_ms"), "duration_ms", 1, DURATION_LIMIT_MS)
            repetitions = _integer(step.get("repetitions", 1), "repetitions", 1, MAX_REPETITIONS)
            label = _text(step.get("label", ""), "label", 120, empty=True)
            stop_after = step.get("stop_after_ms")
            if stop_after is not None:
                stop_after = _integer(stop_after, "stop_after_ms", 1, duration - 1)
            if total + repetitions > MAX_TRIALS:
                raise ValueError(f"计划总试次不能超过 {MAX_TRIALS}")
            for _ in range(repetitions):
                trials.append(dict(trial_id=f"{stage_id}-t{len(trials) + 1:03d}",
                                   left_pwm=left, right_pwm=right, duration_ms=duration,
                                   stop_after_ms=stop_after, label=label))
            total += repetitions
        expanded.append(dict(stage_id=stage_id, name=stage_name, trials=trials))
    payload = dict(name=name, stages=expanded, trial_count=total)
    digest = _digest(payload)
    return deepcopy(dict(plan_id=digest[:24], digest=digest, **payload))


def validate_against_caps(plan: dict, limits: dict) -> None:
    """Revalidate the frozen snapshot and the actual CAPS limits before execution."""
    if not isinstance(plan, dict) or not isinstance(limits, dict):
        raise ValueError("计划与 CAPS 必须是对象")
    if plan.get("schema_version") == 2:
        _validate_v2(plan, limits)
        return
    pwm_limit = _integer(limits.get("pwm_limit"), "CAPS.pwm_limit", 1, PWM_LIMIT)
    duration_limit = _integer(limits.get("max_duration_ms"), "CAPS.max_duration_ms", 1,
                              DURATION_LIMIT_MS)
    try:
        payload = {key: plan[key] for key in ("name", "stages", "trial_count")}
        if plan["digest"] != _digest(payload):
            raise ValueError("计划内容与冻结摘要不一致")
        plan_id = plan.get("plan_id")
        if not isinstance(plan_id, str) or not _TOKEN.fullmatch(plan_id):
            raise ValueError("plan_id 格式无效")
        draft = dict(name=plan["name"], stages=[
            dict(stage_id=s["stage_id"], name=s["name"], steps=[
                {k: t[k] for k in ("left_pwm", "right_pwm", "duration_ms", "stop_after_ms", "label")}
                for t in s["trials"]]) for s in plan["stages"]])
        rebuilt = build_plan(draft)
        if any(rebuilt[key] != plan[key] for key in ("name", "stages", "trial_count", "digest")):
            raise ValueError("计划不是有效的冻结快照")
        for stage in plan["stages"]:
            for trial in stage["trials"]:
                if max(abs(trial["left_pwm"]), abs(trial["right_pwm"])) > pwm_limit:
                    raise ValueError("计划 PWM 超过实际 CAPS 上限")
                if trial["duration_ms"] > duration_limit:
                    raise ValueError("计划时长超过实际 CAPS 上限")
    except (KeyError, TypeError) as exc:
        raise ValueError("计划快照字段无效") from exc


def get_templates() -> list[dict]:
    """Six editable drafts. Null PWM deliberately prevents accidental execution."""
    definitions = [
        ("direction", "轮向核对", 500, 1, None,
         [(None, 0, "左轮正向"), (None, 0, "左轮反向"),
          (0, None, "右轮正向"), (0, None, "右轮反向")]),
        ("repeat_start", "重复启动", 500, 5, None,
         [(None, 0, "左轮启动重复"), (0, None, "右轮启动重复")]),
        ("long_pulse", "较长脉冲", 800, 1, None,
         [(None, 0, "左轮较长观察"), (0, None, "右轮较长观察")]),
        ("curve", "PWM—速度曲线", 800, 3, None,
         [(None, 0, "左轮第1档"), (None, 0, "左轮第2档"),
          (0, None, "右轮第1档"), (0, None, "右轮第2档")]),
        ("expiry_stop", "到期停止", 500, 1, None,
         [(None, None, "到期软件停止核对")]),
        ("active_stop", "主动 STOP", 800, 1, 400,
         [(None, None, "执行中请求 STOP")]),
    ]
    legacy = [dict(name=name, stages=[dict(stage_id=identifier, name=name, steps=[
        dict(left_pwm=left, right_pwm=right, duration_ms=duration,
             repetitions=repetitions, stop_after_ms=stop_after, label=label)
        for left, right, label in wheels])])
        for identifier, name, duration, repetitions, stop_after, wheels in definitions]
    profile_steps = []
    for repeat in range(3):
        for kind in ("candidate", "high_hold", "zero_output"):
            for side, direction in (("left", "正向"), ("right", "反向"), ("left", "反向"), ("right", "正向")):
                first = dict(left_pwm=0, right_pwm=0, duration_ms=400)
                second = dict(left_pwm=0, right_pwm=0, duration_ms=600)
                first[f"{side}_pwm"] = None
                if kind != "zero_output":
                    second[f"{side}_pwm"] = None
                profile_steps.append(dict(segments=[first, second], control_type=kind,
                    repetitions=1, label=f"{side} {direction} {kind} 第{repeat + 1}次；填写对应有符号PWM"))
    return legacy + [dict(schema_version=2, name="四条件三时间块维持与对照（108试次；须填PWM及预算）",
        suite=dict(suite_id="maintenance", total_entered_budget=None), stages=[
            dict(stage_id=f"block_{i + 1}", name=f"时间块{i + 1}", block_id=f"B{i + 1}",
                 rest_before_ms=0 if i == 0 else 60_000,
                 steps=deepcopy(profile_steps if i == 0 else list(reversed(profile_steps)) if i == 1 else profile_steps[12:] + profile_steps[:12]))
            for i in range(3)])]


def _build_v2(raw):
    raw = _object(raw, "计划", {"schema_version", "name", "suite", "stages", "refinement_basis"})
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 2:
        raise ValueError("不支持的计划 schema_version")
    name = _text(raw.get("name"), "计划名称")
    suite = _object(raw.get("suite"), "suite", {"suite_id", "total_entered_budget"})
    suite_id = suite.get("suite_id")
    if not isinstance(suite_id, str) or not _TOKEN.fullmatch(suite_id):
        raise ValueError("suite_id 必须是1..48位字母、数字、下划线或短横线")
    budget = _integer(suite.get("total_entered_budget"), "套件总进入预算", 1, MAX_SUITE_TRIALS)
    stages = raw.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
        raise ValueError(f"计划需要1..{MAX_STAGES}个阶段")
    expanded, ids, total = [], set(), 0
    for stage in stages:
        stage = _object(stage, "阶段", {"stage_id", "name", "block_id", "rest_before_ms", "steps"})
        identifier = stage.get("stage_id")
        if not isinstance(identifier, str) or not _TOKEN.fullmatch(identifier) or identifier in ids:
            raise ValueError("stage_id 必须有效且不能重复")
        ids.add(identifier)
        block = _text(stage.get("block_id", identifier), "block_id", 48)
        rest = _integer(stage.get("rest_before_ms", 0), "rest_before_ms", 0, MAX_REST_MS)
        stage_name = _text(stage.get("name"), "阶段名称")
        steps = stage.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_TRIALS:
            raise ValueError("阶段需要非空有限steps")
        trials = []
        for step in steps:
            step = _object(step, "步骤", {"left_pwm", "right_pwm", "duration_ms", "segments",
                "repetitions", "label", "stop_after_ms", "control_type", "origin_slot"})
            reps = _integer(step.get("repetitions", 1), "repetitions", 1, MAX_REPETITIONS)
            label = _text(step.get("label", ""), "label", 120, empty=True)
            control = step.get("control_type", "candidate")
            if control not in ("candidate", "high_hold", "zero_output", "fixed_pwm"):
                raise ValueError("control_type 无效")
            if "segments" in step:
                if any(key in step for key in ("left_pwm", "right_pwm", "duration_ms")):
                    raise ValueError("segments不能与固定PWM字段混用")
                segments = step["segments"]
                if not isinstance(segments, list) or len(segments) != 2:
                    raise ValueError("有限profile必须明确两段")
                clean = []
                for segment in segments:
                    segment = _object(segment, "段", {"left_pwm", "right_pwm", "duration_ms"})
                    clean.append(dict(left_pwm=_integer(segment.get("left_pwm"), "段left_pwm", -PWM_LIMIT, PWM_LIMIT),
                        right_pwm=_integer(segment.get("right_pwm"), "段right_pwm", -PWM_LIMIT, PWM_LIMIT),
                        duration_ms=_integer(segment.get("duration_ms"), "段duration_ms", 1, DURATION_LIMIT_MS)))
                duration = sum(s["duration_ms"] for s in clean)
                _integer(duration, "总时长", 2, DURATION_LIMIT_MS)
                for side in ("left_pwm", "right_pwm"):
                    if clean[0][side] * clean[1][side] < 0 or (clean[0][side] == 0 and clean[1][side] != 0):
                        raise ValueError("同轮维持段不得换向或从未激励轮开始输出")
                if not (clean[0]["left_pwm"] or clean[0]["right_pwm"]):
                    raise ValueError("启动段至少激励一轮")
                if control == "zero_output" and any(clean[1][k] for k in ("left_pwm", "right_pwm")):
                    raise ValueError("zero_output对照第二段必须两轮PWM0")
                if control == "high_hold" and any(clean[1][k] != clean[0][k] for k in ("left_pwm", "right_pwm")):
                    raise ValueError("high_hold对照必须保持启动PWM")
                base = dict(segments=clean, duration_ms=duration,
                            left_pwm=clean[0]["left_pwm"], right_pwm=clean[0]["right_pwm"])
            else:
                if control not in ("candidate", "fixed_pwm"):
                    raise ValueError("分段对照必须声明segments")
                duration = _integer(step.get("duration_ms"), "duration_ms", 1, DURATION_LIMIT_MS)
                base = dict(left_pwm=_integer(step.get("left_pwm"), "left_pwm", -PWM_LIMIT, PWM_LIMIT),
                            right_pwm=_integer(step.get("right_pwm"), "right_pwm", -PWM_LIMIT, PWM_LIMIT),
                            duration_ms=duration)
                control = "fixed_pwm"
            stop_after = step.get("stop_after_ms")
            if stop_after is not None:
                stop_after = _integer(stop_after, "stop_after_ms", 1, duration - 1)
            origin = _text(step.get("origin_slot", ""), "origin_slot", 160, empty=True)
            if total + reps > MAX_TRIALS or total + reps > budget:
                raise ValueError("计划超过128试次或套件总进入预算")
            for _ in range(reps):
                trial_id = f"{identifier}-t{len(trials) + 1:03d}"
                trials.append(dict(trial_id=trial_id, slot_id=trial_id, stage_id=identifier, block_id=block,
                    control_type=control, label=label, stop_after_ms=stop_after, origin_slot=origin, **deepcopy(base)))
            total += reps
        expanded.append(dict(stage_id=identifier, name=stage_name, block_id=block, rest_before_ms=rest, trials=trials))
    basis = raw.get("refinement_basis", "")
    _text(basis, "refinement_basis", 2000, empty=True)
    payload = dict(schema_version=2, name=name, suite=dict(suite_id=suite_id, total_entered_budget=budget),
                   stages=expanded, trial_count=total, refinement_basis=basis,
                   deadline_strategy="current_device_tick_plus_total_once")
    digest = _digest(payload)
    return deepcopy(dict(plan_id=digest[:24], digest=digest, **payload))


def plan_to_draft(plan):
    """Return an editable draft; expanded repetitions stay explicit and ordered."""
    result = dict(name=plan["name"], stages=[])
    v2 = plan.get("schema_version") == 2
    if v2:
        result.update(schema_version=2, suite=deepcopy(plan["suite"]),
                      refinement_basis=plan.get("refinement_basis", ""))
    for stage in plan["stages"]:
        converted = dict(stage_id=stage["stage_id"], name=stage["name"], steps=[])
        if v2:
            converted.update(block_id=stage["block_id"], rest_before_ms=stage["rest_before_ms"])
        for trial in stage["trials"]:
            step = {k: deepcopy(trial[k]) for k in ("label", "stop_after_ms")}
            if "segments" in trial:
                step["segments"] = deepcopy(trial["segments"])
            else:
                step.update({k: trial[k] for k in ("left_pwm", "right_pwm", "duration_ms")})
            if v2:
                step.update(control_type=trial["control_type"], origin_slot=trial["origin_slot"])
            converted["steps"].append(step)
        result["stages"].append(converted)
    return result


def _validate_v2(plan, limits):
    if not isinstance(plan.get("plan_id"), str) or not _TOKEN.fullmatch(plan["plan_id"]):
        raise ValueError("plan_id格式无效")
    rebuilt = build_plan(plan_to_draft(plan))
    if {k: v for k, v in plan.items() if k != "plan_id"} != {k: v for k, v in rebuilt.items() if k != "plan_id"}:
        raise ValueError("计划不是有效的冻结快照或内容摘要已改变")
    base_pwm = _integer(limits.get("pwm_limit"), "CAPS.pwm_limit", 1, PWM_LIMIT)
    base_duration = _integer(limits.get("max_duration_ms"), "CAPS.max_duration_ms", 1, DURATION_LIMIT_MS)
    for stage in plan["stages"]:
        for trial in stage["trials"]:
            pwm, duration = base_pwm, base_duration
            if "segments" in trial:
                if limits.get("profile_version") != 1:
                    raise ValueError("需要真实PROFILE_CAPS版本1")
                _integer(limits.get("profile_max_segments"), "CAPS.profile_max_segments", 2, 2)
                pwm = min(pwm, _integer(limits.get("profile_pwm_limit"), "CAPS.profile_pwm_limit", 1, PWM_LIMIT))
                duration = min(duration, _integer(limits.get("profile_max_total_ms"), "CAPS.profile_max_total_ms", 2, DURATION_LIMIT_MS))
                minimum = _integer(limits.get("profile_min_segment_ms", 1), "CAPS.profile_min_segment_ms", 1, DURATION_LIMIT_MS)
                if any(s["duration_ms"] < minimum for s in trial["segments"]):
                    raise ValueError("分段时长小于实际PROFILE_CAPS下限")
            segments = trial.get("segments", [trial])
            if any(max(abs(s["left_pwm"]), abs(s["right_pwm"])) > pwm for s in segments):
                raise ValueError("PWM超过实际CAPS上限")
            if trial["duration_ms"] > duration:
                raise ValueError("时长超过实际CAPS上限")
