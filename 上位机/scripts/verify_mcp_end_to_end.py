"""Real SDK -> stdio MCP -> TCP bridge -> entry point -> PROJECT_V1 fake.

Run from the existing SDK environment, without installing dependencies:
    .venv-mcp/Scripts/python.exe scripts/verify_mcp_end_to_end.py

All evidence stays under a unique .qa project/runtime. The production entry
point receives only an isolated artifact root and the strict fake factory.
This test checks child ownership and never uses the existing real bridge.
"""

import asyncio
import csv
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from mcp import Client, StdioServerParameters


EXPECTED_TOOLS = {
    "get_status", "connect_device", "disconnect_device", "preview_plan",
    "get_templates", "start_stage", "get_progress", "add_observation", "stop", "get_results",
    "start_plan", "get_communication_health", "suggest_refinement",
    "query_encoder_totals",
}


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


async def scenario(source_project, project, qa_directory, endpoint, gui, gui_log, stop_file):
    def verify_owned_endpoint():
        if gui.poll() is not None:
            raise AssertionError(f"假设备工作台已退出；诊断日志：{gui_log}")
        assert endpoint.is_file() and endpoint.stat().st_size <= 64 * 1024
        saved = json.loads(endpoint.read_text(encoding="utf-8"))
        owner = json.loads(stop_file.with_suffix(".owner.json").read_text(encoding="utf-8"))
        assert owner.get("run_marker") == stop_file.name
        assert saved.get("pid") == owner.get("pid"), "拒绝附加不属于本验证子进程的工作台端点"
        assert saved.get("host") == "127.0.0.1"
        assert Path(saved["project_directory"]).resolve() == project

    deadline = time.monotonic() + 10
    while not endpoint.exists():
        if gui.poll() is not None or time.monotonic() > deadline:
            raise AssertionError(f"假设备正式入口未建立控制桥；诊断日志：{gui_log}")
        await asyncio.sleep(.1)
    verify_owned_endpoint()
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(source_project / "car_debug_mcp" / "launch.py"), "--project-directory", str(project)],
        cwd=str(qa_directory))
    async with Client(parameters) as client:
        tool_list = await client.list_tools()
        assert {tool.name for tool in tool_list.tools} == EXPECTED_TOOLS

        async def call(name, arguments=None):
            verify_owned_endpoint()
            result = await client.call_tool(name, arguments or {})
            value = result.structured_content
            assert isinstance(value, dict) and "error" not in value, (name, value)
            return value

        state = await call("get_status")
        assert state["demo"] and not state["connected"] and not state["active"]
        assert (await call("connect_device"))["queued"]
        deadline = time.monotonic() + 5
        while True:
            state = await call("get_status")
            if state["connected"] and state["caps"] and state["state"]:
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(.1)
        assert state["state"]["source"] == "SIMULATION_PROJECT_V1"
        assert (await call("query_encoder_totals"))["success"]
        deadline = time.monotonic() + 5
        while True:
            state = await call("get_status")
            if state["capabilities"]["encoder_totals"] and state["encoder_totals"]:
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(.1)
        assert state["caps"]["wheel_diameter_um"] == 66000
        assert not state["state"]["armed"] and state["state"]["pwm_left"] == state["state"]["pwm_right"] == 0
        plan = await call("preview_plan", {"plan": {"name": "SDK 真实链路纯软件验收",
            "stages": [{"stage_id": "demo", "name": "到期与主动停止",
                "steps": [{"left_pwm": 1100, "right_pwm": -1200, "duration_ms": 500,
                           "repetitions": 2, "label": "仅软件夹具的到期试次"},
                          {"left_pwm": 1300, "right_pwm": 0, "duration_ms": 800,
                           "stop_after_ms": 300, "label": "仅软件夹具的主动 STOP"}]}]}})
        run = await call("start_stage", dict(plan_id=plan["plan_id"], plan_digest=plan["digest"],
            stage_id="demo", approval_text="开发验收软件夹具：仅假设备，未进行物理实验",
            ready_confirmed=True))
        # No tool calls for >3 seconds: heartbeat must run independently of calls.
        await asyncio.sleep(3.4)
        deadline = time.monotonic() + 15
        while True:
            state = await call("get_progress")
            if not state["active"]:
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(.15)
        assert state["phase"] == "completed" and state["completed_trials"] == 3, state
        results = await call("get_results", {"run_id": run["run_id"]})
        assert results["finished"] and not results["failure"]
        paths = results["artifacts"]
        for path in paths.values():
            artifact = Path(path).resolve()
            assert artifact.is_relative_to(qa_directory) and artifact.is_file(), path
        directory = Path(paths["report"]).parent
        report = Path(paths["report"]).read_text(encoding="utf-8")
        assert "编码器字段不累加为里程" in report
        rows = read_csv(paths["results"])
        assert len(rows) == 3 and all(row["status"] == "completed" for row in rows)
        telemetry = read_csv(directory / "telemetry.csv")
        assert len(telemetry) > 60 and all(row["source"] == "SIMULATION_PROJECT_V1" for row in telemetry)
        context = json.loads(Path(paths["context"]).read_text(encoding="utf-8"))
        assert context["software_demo"] and context["approval_source"] == "software_test_fixture"
        assert not context["raw_handshake_covered"]
        assert "dropped_records=0 failure=None" in (directory / "communication.log").read_text(encoding="utf-8")
        assert (await call("add_observation", dict(run_id=run["run_id"], trial_id="demo-t003",
            text="SDK 与假设备闭环通过；没有实物方向或机械停止验收。", source="assistant")))["queued"]
        deadline = time.monotonic() + 3
        while "SDK" not in Path(paths["observations"]).read_text(encoding="utf-8"):
            assert time.monotonic() < deadline
            await asyncio.sleep(.05)

        # The new v2 workflow deliberately prepares profile capability through
        # an explicit preview. Normal connect/legacy preview above used HELLO.
        assert not (await call("get_status"))["capabilities"]["pwm_profile"]
        suite_id = "sdk_profile_" + uuid4().hex
        profile_plan = await call("preview_plan", {"plan": {
            "schema_version": 2, "name": "SDK v2 两时间块及有限两段纯软件验收",
            "suite": {"suite_id": suite_id, "total_entered_budget": 2},
            "stages": [
                {"stage_id": "candidate", "name": "候选两段", "block_id": "B1",
                 "rest_before_ms": 0, "steps": [{"control_type": "candidate",
                    "segments": [{"left_pwm": 1400, "right_pwm": 0, "duration_ms": 400},
                                 {"left_pwm": 900, "right_pwm": 0, "duration_ms": 600}]}]},
                {"stage_id": "zero", "name": "输出归零对照", "block_id": "B2",
                 "rest_before_ms": 200, "steps": [{"control_type": "zero_output",
                    "segments": [{"left_pwm": 1400, "right_pwm": 0, "duration_ms": 400},
                                 {"left_pwm": 0, "right_pwm": 0, "duration_ms": 600}]}]}]}})
        deadline = time.monotonic() + 5
        while True:
            profile_status = await call("get_status")
            if profile_status["capabilities"]["pwm_profile"] and profile_status["state"].get("profile_status") == 0:
                break
            assert time.monotonic() < deadline, profile_status
            await asyncio.sleep(.1)
        profile_run = await call("start_plan", dict(plan_id=profile_plan["plan_id"],
            plan_digest=profile_plan["digest"],
            approval_text="开发验收软件夹具：仅假设备v2两段表，未进行物理实验", ready_confirmed=True))
        deadline = time.monotonic() + 18
        while True:
            progress = await call("get_progress")
            if not progress["active"]:
                break
            assert time.monotonic() < deadline, progress
            await asyncio.sleep(.15)
        assert progress["phase"] == "completed" and progress["completed_trials"] == 2, progress
        profile_results = await call("get_results", {"run_id": profile_run["run_id"]})
        assert profile_results["finished"] and not profile_results["failure"]
        profile_paths = profile_results["artifacts"]
        for path in profile_paths.values():
            assert Path(path).resolve().is_relative_to(qa_directory) and Path(path).is_file(), path
        summary = json.loads(Path(profile_paths["summary"]).read_text(encoding="utf-8"))
        assert summary["completed_count"] == 2 and summary["independent_verification"]["verified"], summary
        assert {row["block_id"] for row in summary["rows"]} == {"B1", "B2"}
        assert all(row["profile_segment_observed"] for row in summary["rows"])
        assert all(row["conclusion_window_ms"] == 600 for row in summary["rows"])
        segments = read_csv(profile_paths["segments"])
        assert len(segments) == 4 and {row["segment_index"] for row in segments} == {"0", "1"}
        profile_telemetry = read_csv(Path(profile_paths["report"]).parent / "telemetry.csv")
        assert {row["segment_index"] for row in profile_telemetry} >= {"0", "1"}
        assert all(row["source"] == "SIMULATION_PROJECT_V1" for row in profile_telemetry)
        suite = await call("get_results", {"suite_id": suite_id})
        assert suite["entered_count"] == suite["completed_count"] == 2 and suite["remaining_budget"] == 0
        health = await call("get_communication_health")
        suggestion = await call("suggest_refinement", {"side": "left", "direction": 1,
            "run_ids": [profile_run["run_id"]]})
        assert suggestion["status"] == "undetermined" and suggestion["draft"] is None
        await call("disconnect_device")
        return dict(sdk=importlib.metadata.version("mcp"),
                    tools=sorted(tool.name for tool in tool_list.tools), completed_trials=5,
                    legacy_completed_trials=3, profile_completed_trials=2,
                    telemetry_count=len(telemetry) + len(profile_telemetry), independent_heartbeat_seconds=3.4,
                    result=results, profile_result=profile_results, suite=suite,
                    profile_health=health, refinement_suggestion=suggestion,
                    isolated_project_directory=str(project), gui_log=str(gui_log), real_hardware_access=False)


def main() -> int:
    source_project = Path(__file__).resolve().parent.parent
    # Each invocation owns a separate endpoint, lock, preferences and artifacts.
    # An existing real workbench in the source project remains untouched.
    project = source_project / ".qa" / ("sdk_end_to_end_" + uuid4().hex)
    qa_directory = project / ".qa"
    endpoint = project / ".runtime" / "automation.json"
    if any((endpoint.parent / name).exists() for name in ("automation.json", "automation.lock")):
        raise SystemExit("已有工作台端点或实例锁；验证拒绝附加现有进程，以免访问实物。")
    qa_directory.mkdir(parents=True, exist_ok=True)
    host_python = source_project / ".venv" / "Scripts" / "python.exe"
    if not host_python.is_file():
        raise SystemExit("缺少既有上位机 .venv；本验证不安装依赖。")
    stop_file = qa_directory / f"mcp_qa_stop_{uuid4().hex}"
    gui_log = qa_directory / "mcp_end_to_end_gui.log"
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    with gui_log.open("w", encoding="utf-8") as log_handle:
        gui = subprocess.Popen([str(host_python), str(source_project / "scripts" / "run_automation_qa_host.py"),
                                str(stop_file), str(project)], cwd=source_project, env=env, stdout=log_handle, stderr=log_handle)
        try:
            evidence = asyncio.run(asyncio.wait_for(scenario(source_project, project, qa_directory, endpoint, gui, gui_log, stop_file), 60))
        finally:
            stop_file.write_text("QA complete", encoding="utf-8")
            try:
                gui.wait(timeout=8)
            except subprocess.TimeoutExpired:
                gui.terminate()
                try:
                    gui.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    gui.kill()
                    gui.wait(timeout=3)
                raise RuntimeError(f"假设备窗口未正常退出；诊断日志：{gui_log}")
            finally:
                stop_file.unlink(missing_ok=True)
                stop_file.with_suffix(".owner.json").unlink(missing_ok=True)
    assert gui.returncode == 0 and not endpoint.exists(), "正常退出必须清除本次控制桥端点"
    result_path = qa_directory / "mcp_end_to_end_result.json"
    evidence["normal_gui_exit"] = True
    result_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    (source_project / ".qa" / "mcp_end_to_end_result.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(sdk=evidence["sdk"], tools=len(evidence["tools"]), completed_trials=evidence["completed_trials"],
                         telemetry_count=evidence["telemetry_count"], directory=str(Path(evidence["result"]["artifacts"]["report"]).parent),
                         result_path=str(result_path), normal_gui_exit=True, real_hardware_access=False), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
