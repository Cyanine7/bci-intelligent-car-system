"""Official SDK discovery with no GUI endpoint or hardware access."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from car_debug_mcp.server import TOOL_NAMES


async def verify():
    (ROOT / ".qa").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="car-sdk-discovery-", dir=ROOT / ".qa") as temporary:
        directory = Path(temporary)
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "car_debug_mcp" / "launch.py"), "--project-directory", str(directory)], cwd=directory)
        async with Client(parameters) as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == TOOL_NAMES
            readonly = {"get_status", "get_templates", "get_progress", "get_results",
                        "get_communication_health", "suggest_refinement"}
            for tool in listing.tools:
                assert tool.annotations.open_world_hint is False
                assert tool.annotations.read_only_hint is (tool.name in readonly)
            for name in ("start_stage", "start_plan"):
                tool = next(t for t in listing.tools if t.name == name)
                assert "授权" in tool.description
            result = await client.call_tool("get_status", {})
            assert result.structured_content["error"] == "bridge_unavailable"
        assert not (directory / ".runtime").exists()
        return dict(tools=len(listing.tools), annotations_verified=True,
                    bridge_unavailable=True, endpoint_created=False, real_hardware_access=False)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(asyncio.wait_for(verify(), 15)), ensure_ascii=False))
