"""Validated, finite experiment plans; templates are unapproved drafts."""

from copy import deepcopy
import hashlib
import json
import re
import unicodedata


MAX_STAGES = 16
MAX_TRIALS = 128
MAX_REPETITIONS = 100
PWM_LIMIT = 6000
DURATION_LIMIT_MS = 1000
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
    return [dict(name=name, stages=[dict(stage_id=identifier, name=name, steps=[
        dict(left_pwm=left, right_pwm=right, duration_ms=duration,
             repetitions=repetitions, stop_after_ms=stop_after, label=label)
        for left, right, label in wheels])])
        for identifier, name, duration, repetitions, stop_after, wheels in definitions]
