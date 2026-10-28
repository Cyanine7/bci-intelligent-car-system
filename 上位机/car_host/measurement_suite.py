"""Bounded suite indexes and refinement drafts; no device or Qt imports."""

from collections import Counter, defaultdict
from copy import deepcopy
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from .experiment_models import build_plan, plan_to_draft

MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_RUNS = 256


def read_suite(directory):
    path = Path(directory) / "index.json"
    if not path.exists():
        return None
    if path.stat().st_size > MAX_INDEX_BYTES:
        raise ValueError("套件索引超过有界大小")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 2 or not isinstance(value.get("runs"), list):
        raise ValueError("套件索引格式无效")
    if len(value["runs"]) > MAX_RUNS:
        raise ValueError("套件运行索引达到上限")
    budget = value.get("total_entered_budget")
    if type(budget) is not int or not 1 <= budget <= 4096:
        raise ValueError("套件索引预算无效")
    identities, consumed = set(), 0
    for run in value["runs"]:
        if not isinstance(run, dict):
            raise ValueError("套件运行索引无效")
        identity = run.get("run_id")
        count, entered, finished = run.get("planned_count"), run.get("entered_count"), run.get("finished")
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", identity)
            or identity in identities or type(count) is not int or not 1 <= count <= 128
            or type(entered) is not int or not 0 <= entered <= count or type(finished) is not bool
            or not isinstance(run.get("slots"), list) or len(run["slots"]) != count
            or any(not isinstance(slot, dict) or not isinstance(slot.get("trial_id"), str) for slot in run["slots"])
            or len({s["trial_id"] for s in run["slots"]}) != count):
            raise ValueError("套件运行预算、身份或槽位无效")
        identities.add(identity)
        consumed += entered if finished else count
    if consumed > budget:
        raise ValueError("套件索引已超过总预算")
    return value


def _write_suite(directory, value):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    if len(payload.encode("utf-8")) > MAX_INDEX_BYTES:
        raise ValueError("套件索引超过有界大小")
    temporary = directory / f"index.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(directory / "index.json")
    finally:
        temporary.unlink(missing_ok=True)


def check_suite(directory, suite, planned_count):
    """Unfinished reservations consume budget; a crashed run never resumes."""
    saved = read_suite(directory)
    if saved is None:
        return
    if saved.get("suite_id") != suite["suite_id"] or saved.get("total_entered_budget") != suite["total_entered_budget"]:
        raise ValueError("已有套件预算不可改写；扩大边界必须使用新套件及真实授权")
    if len(saved["runs"]) >= MAX_RUNS:
        raise ValueError("套件运行索引达到256次上限")
    consumed = sum(r.get("entered_count", 0) if r.get("finished") else r["planned_count"] for r in saved["runs"])
    if consumed + planned_count > suite["total_entered_budget"]:
        raise ValueError("新冻结表超过套件剩余进入预算（含未终结运行的保留预算）")


def reserve_suite(directory, suite, run_id, plan, slots, artifacts):
    """Called by the journal thread before it publishes ready."""
    check_suite(directory, suite, len(slots))
    value = read_suite(directory) or dict(schema_version=2, **deepcopy(suite), runs=[])
    if any(run["run_id"] == run_id for run in value["runs"]):
        raise ValueError("套件运行身份不能重复保留预算")
    value["runs"].append(dict(run_id=run_id, plan_id=plan["plan_id"], digest=plan["digest"],
        planned_count=len(slots), slots=deepcopy(slots), entered_count=0, finished=False,
        status="recording", artifacts=deepcopy(artifacts)))
    _write_suite(directory, value)


def finish_suite(directory, run_id, trials, status):
    value = read_suite(directory)
    if value is None:
        raise ValueError("套件保留预算索引不存在")
    run = next((r for r in value["runs"] if r["run_id"] == run_id), None)
    if run is None:
        raise ValueError("套件运行身份不存在")
    if run.get("finished"):
        raise ValueError("套件终结运行不可再次改写")
    entered = {t["trial_id"] for t in trials}
    planned = {slot["trial_id"] for slot in run["slots"]}
    if len(entered) != len(trials) or not entered <= planned or any(t.get("status") not in ("completed", "aborted", "failed") for t in trials):
        raise ValueError("套件终结试次必须唯一、来自计划槽位且为终结状态")
    run.update(finished=True, status=status, entered_count=len(trials),
        completed_count=sum(t["status"] == "completed" for t in trials),
        aborted_count=sum(t["status"] == "aborted" for t in trials),
        failed_count=sum(t["status"] == "failed" for t in trials),
        cancelled_slots=[s for s in run["slots"] if s["trial_id"] not in entered])
    value["entered_count"] = sum(r.get("entered_count", 0) for r in value["runs"])
    value["completed_count"] = sum(r.get("completed_count", 0) for r in value["runs"])
    value["aborted_count"] = sum(r.get("aborted_count", 0) for r in value["runs"])
    value["cancelled_count"] = sum(len(r.get("cancelled_slots", [])) for r in value["runs"])
    value["reserved_count"] = sum(r["planned_count"] for r in value["runs"] if not r.get("finished"))
    value["remaining_budget"] = value["total_entered_budget"] - value["entered_count"] - value["reserved_count"]
    _write_suite(directory, value)


def suggest_refinement(rows, *, side, direction, suite, remaining_budget):
    """Return only a new draft. All screens and budgets remain explicit."""
    if side not in ("left", "right") or type(direction) is not int or direction not in (-1, 1):
        raise ValueError("side须为left/right，direction须为整数-1或1")
    selected = [r for r in rows if r.get("segments") and r.get("status") == "completed"
        and r.get("control_type") == "candidate" and r["segments"][0][f"{side}_pwm"] * direction > 0
        and r["segments"][0][f"{'right' if side == 'left' else 'left'}_pwm"] == 0]
    conditions = {(r["segments"][0][f"{side}_pwm"], r["segments"][0]["duration_ms"],
                   r["segments"][1]["duration_ms"]) for r in selected}
    if len(conditions) != 1:
        return dict(status="undetermined", reason="需要同侧、方向、启动PWM及段时长的可比结果", draft=None)
    start_pwm, startup_ms, hold_ms = next(iter(conditions))
    groups = defaultdict(list)
    for row in selected:
        metric = row.get("profile_metrics", {}).get(side, {})
        if (metric.get("startup") != "established"
            or metric.get("maintenance") not in ("window_sustained", "window_not_sustained")
            or metric.get("inertia_conclusion") not in ("exceeds_zero_control_observations", "not_sustained_in_window")):
            continue
        block = (row.get("block_id"), row.get("connection_id"), row.get("session"))
        groups[(block, abs(row["segments"][1][f"{side}_pwm"]))].append(metric["maintenance"])
    blocks = sorted({block for block, _ in groups})
    levels = sorted({level for _, level in groups})
    outcome = {}
    for level in levels:
        values = [groups.get((block, level), []) for block in blocks]
        if len(blocks) < 3 or any(len(v) < 3 for v in values):
            outcome[level] = "insufficient"
        elif all(all(x == "window_sustained" for x in v) for v in values):
            outcome[level] = "screen_success"
        elif all(all(x == "window_not_sustained" for x in v) for v in values):
            outcome[level] = "screen_failure"
        else:
            outcome[level] = "mixed"
    base = dict(outcomes=outcome, sample_rule="每档每块至少3次，至少3块；全同结果仅为有限样本筛选，不是可靠保证",
                draft=None, auto_execute=False)
    if not levels or any(v in ("mixed", "insufficient") for v in outcome.values()):
        return dict(**base, status="undetermined", reason="样本不足或跨块结果混合；建议保持原档复测")
    if all(v == "screen_success" for v in outcome.values()):
        return dict(**base, status="one_sided", reason=f"有限窗口边界不高于最低已测幅值{levels[0]}；不能给单点")
    if all(v == "screen_failure" for v in outcome.values()):
        return dict(**base, status="undetermined", reason="已测范围内未找到成功档，不自动升档")
    ordering = [outcome[x] for x in levels]
    if any(ordering[i] == "screen_success" and ordering[i + 1] == "screen_failure" for i in range(len(ordering) - 1)):
        return dict(**base, status="undetermined", reason="结果非单调；不压缩为一个阈值")
    boundary = next(((a, b) for a, b in zip(levels, levels[1:])
        if outcome[a] == "screen_failure" and outcome[b] == "screen_success"), None)
    if boundary is None:
        return dict(**base, status="undetermined", reason="未形成相邻可比失败/成功区间")
    low, high = boundary
    increment = 50 if high - low > 50 else 25
    candidates = sorted(set([low, high, *range(low + increment, high, increment)]))
    if len(candidates) < 3:
        return dict(**base, status="interval", reason="区间已不宽于25 PWM，保持区间而非单点", interval=[low, high])
    count = 3 * 3 * (len(candidates) + 2)
    if count > 128 or count > remaining_budget:
        return dict(**base, status="budget_limited", reason=f"含三块、端点锚点与两对照需要{count}次，剩余{remaining_budget}次", interval=[low, high])
    passive = "right" if side == "left" else "left"
    steps = []
    for level, kind in [(x, "candidate") for x in candidates] + [(abs(start_pwm), "high_hold"), (0, "zero_output")]:
        segments = [dict(left_pwm=0, right_pwm=0, duration_ms=startup_ms),
                    dict(left_pwm=0, right_pwm=0, duration_ms=hold_ms)]
        segments[0][f"{side}_pwm"] = start_pwm
        segments[1][f"{side}_pwm"] = direction * level
        assert segments[0][f"{passive}_pwm"] == segments[1][f"{passive}_pwm"] == 0
        steps.append(dict(segments=segments, control_type=kind, repetitions=3,
                          label=f"{side} {direction:+d} {kind} {level}"))
    draft = dict(schema_version=2, name=f"{side}{direction:+d}维持边界加密", suite=deepcopy(suite),
        refinement_basis=f"三个时间块有限样本筛选区间[{low},{high}]；{increment}PWM步长；须新预览冻结，未自动执行",
        stages=[dict(stage_id=f"refine_b{i + 1}", name=f"细扫时间块{i + 1}", block_id=f"refine_B{i + 1}",
                     rest_before_ms=60_000, steps=deepcopy(steps if i == 0 else list(reversed(steps)) if i == 1 else steps[1:] + steps[:1]))
                for i in range(3)])
    build_plan(draft)
    return dict(**{k: v for k, v in base.items() if k != "draft"}, status="draft", draft=draft,
                interval=[low, high], required_trials=count, step_pwm=increment,
                reason="新草稿包含三时间块、端点锚点、高PWM保持及PWM0对照；必须重新预览冻结")


def makeup_draft(plan, run_id, completed_trials, remaining_budget):
    """Create a whole new draft for unfinished slots, without resuming a run.

    Actual trial dictionaries exclude only ``status == completed``; a completed
    trial with zero feedback remains completed. A list of completed IDs is also
    accepted. The original plan and evidence are never changed.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", run_id):
        raise ValueError("补测来源run_id无效")
    if type(remaining_budget) is not int or not 0 <= remaining_budget <= 4096:
        raise ValueError("剩余进入预算须为0..4096的整数")
    if not isinstance(plan, dict):
        raise ValueError("补测需要冻结计划")
    try:
        rebuilt = build_plan(plan_to_draft(plan))
    except (KeyError, TypeError) as exc:
        raise ValueError("补测来源冻结计划字段无效") from exc
    if ({k: v for k, v in plan.items() if k != "plan_id"}
            != {k: v for k, v in rebuilt.items() if k != "plan_id"}):
        raise ValueError("补测来源计划的冻结摘要或内容无效")
    if not isinstance(completed_trials, list) or len(completed_trials) > 128:
        raise ValueError("补测来源试次列表须有界")
    planned_ids = {t["trial_id"] for stage in plan["stages"] for t in stage["trials"]}
    completed, seen = set(), set()
    for trial in completed_trials:
        if isinstance(trial, str):
            identifier, status = trial, "completed"
        elif isinstance(trial, dict):
            identifier, status = trial.get("trial_id"), trial.get("status")
            if status not in ("completed", "aborted", "failed"):
                raise ValueError("补测只能依据终结试次状态")
        else:
            raise ValueError("来源试次须为试次对象或已完成身份")
        if identifier not in planned_ids or identifier in seen:
            raise ValueError("来源试次身份必须唯一且来自冻结计划")
        seen.add(identifier)
        if status == "completed":
            completed.add(identifier)
    required = sum(t["trial_id"] not in completed for stage in plan["stages"] for t in stage["trials"])
    response = dict(draft=None, required_trials=required)
    if not required:
        return dict(**response, status="empty", reason="全部槽位已completed；充分失败样本不自动加入补测")
    if plan.get("schema_version") != 2 or not isinstance(plan.get("suite"), dict):
        return dict(**response, status="needs_suite", reason="旧v1记录须明确新suite_id及总进入预算，再预览v2补测草稿")
    if required > remaining_budget:
        return dict(**response, status="budget_limited", reason=f"全部补测需要{required}次，剩余{remaining_budget}次；不生成部分表")
    draft = dict(schema_version=2, name="补测：" + plan["name"][:72], suite=deepcopy(plan["suite"]),
        refinement_basis=f"来源run {run_id}；排除全部completed槽位（包括充分失败）；中止和未进入槽位使用新版本，不恢复或重发旧命令",
        stages=[])
    editable = plan_to_draft(plan)
    old_ids = {stage["stage_id"] for stage in plan["stages"]}
    old_blocks = {stage["block_id"] for stage in plan["stages"]}
    for index, (stage, editable_stage) in enumerate(zip(plan["stages"], editable["stages"]), 1):
        steps = []
        for trial, step in zip(stage["trials"], editable_stage["steps"]):
            if trial["trial_id"] not in completed:
                steps.append(deepcopy(step) | {"origin_slot": f"{run_id}:{trial['trial_id']}"})
        if not steps:
            continue
        base_identifier = f"makeup_{run_id[:12]}_{index:02d}"
        identifier, suffix = base_identifier, 0
        while identifier in old_ids or identifier in old_blocks:
            suffix += 1
            identifier = f"{base_identifier}_n{suffix:02d}"
        old_ids.add(identifier)
        old_blocks.add(identifier)
        draft["stages"].append(dict(stage_id=identifier, block_id=identifier,
            name="补测：" + stage["name"][:72], rest_before_ms=stage["rest_before_ms"], steps=steps))
    build_plan(draft)
    return dict(**{k: v for k, v in response.items() if k != "draft"}, status="draft", draft=draft,
        reason="完整补测草稿已生成；保持原顺序、静置与对照，需新预览冻结和实际授权核对")
