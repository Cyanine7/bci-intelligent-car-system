"""客户端用假 TCP 服务；SDK discovery 用真正官方 Client 与 stdio 子进程。"""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from car_debug_mcp.client import BridgeClient, BridgeUnavailable, MAX_MESSAGE_BYTES
from car_debug_mcp.server import TOOL_NAMES


def test_missing_endpoint_no_process_or_hardware(tmp_path):
    async def scenario():
        client = BridgeClient(tmp_path)
        with pytest.raises(BridgeUnavailable, match="未运行"):
            await client.request("get_status")
        await client.close()
        assert not (tmp_path / ".runtime").exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [{}, {"schema_version": 1, "host": "192.168.1.1", "port": 80,
                                    "token": "x" * 40}, {"schema_version": 1, "host": "127.0.0.1",
                                    "port": True, "token": "x" * 40}])
def test_invalid_or_remote_endpoint_refused(tmp_path, bad):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "automation.json").write_text(json.dumps(bad), encoding="utf-8")
    client = BridgeClient(tmp_path)
    with pytest.raises(BridgeUnavailable):
        client._endpoint()


def test_persistent_connection_ids_and_idle_heartbeat(tmp_path):
    async def scenario():
        received, connections = [], []
        async def handle(reader, writer):
            connections.append(writer)
            try:
                while raw := await reader.readline():
                    message = json.loads(raw)
                    received.append(message)
                    # 回复可乱序；客户端按 ID 关联。
                    writer.write((json.dumps({"id": message["id"], "result": {
                        "ok": True, "method": message["method"]}}) + "\n").encode())
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        runtime = tmp_path / ".runtime"
        runtime.mkdir()
        (runtime / "automation.json").write_text(json.dumps(dict(schema_version=1,
            host="127.0.0.1", port=server.sockets[0].getsockname()[1], token="a" * 40,
            project_directory=str(tmp_path.resolve()))), encoding="utf-8")
        client = BridgeClient(tmp_path)
        try:
            results = await asyncio.gather(client.request("get_status"), client.request("get_progress"))
            assert [r["method"] for r in results] == ["get_status", "get_progress"]
            await asyncio.sleep(1.2)
            assert sum(r["method"] == "heartbeat" for r in received) >= 2
            assert len(connections) == 1
            assert len({r["id"] for r in received}) == len(received)
            assert all(r["token"] == "a" * 40 for r in received)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_oversized_request_refused_before_write(tmp_path):
    async def scenario():
        client = BridgeClient(tmp_path)
        with pytest.raises(BridgeUnavailable, match="64 KiB"):
            await client._send("preview_plan", {"plan": "x" * MAX_MESSAGE_BYTES})
        assert not client._pending
        await client.close()
    asyncio.run(scenario())


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="官方 SDK 仅安装于 .venv-mcp")
def test_official_sdk_stdio_discovery_without_endpoint(tmp_path):
    from mcp import Client, StdioServerParameters

    async def scenario():
        project = Path(__file__).resolve().parent.parent
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(project / "car_debug_mcp" / "launch.py"), "--project-directory", str(tmp_path)],
            cwd=tmp_path)
        async with Client(parameters) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == TOOL_NAMES
            start = next(tool for tool in tools.tools if tool.name == "start_stage")
            assert "人" in start.description and "digest" in start.description
            for tool in tools.tools:
                assert tool.annotations.open_world_hint is False
                assert tool.annotations.read_only_hint is (tool.name in {
                    "get_status", "get_templates", "get_progress", "get_results"})
            result = await client.call_tool("get_status", {})
            assert result.structured_content["error"] == "bridge_unavailable"
        assert not (tmp_path / ".runtime").exists()
    asyncio.run(asyncio.wait_for(scenario(), 15.0))
