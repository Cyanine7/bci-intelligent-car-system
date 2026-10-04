"""官方 MCP SDK 的 stdio 服务；工具仅转发到已有 GUI。"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .client import BridgeClient, BridgeUnavailable


TOOL_NAMES = frozenset({"get_status", "connect_device", "disconnect_device", "preview_plan",
                        "get_templates", "start_stage", "get_progress", "add_observation",
                        "stop", "get_results"})


def create_server(project_directory: Path):
    # SDK 放在独立环境；GUI 不依赖或导入本模块。
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    bridge = BridgeClient(project_directory)
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False)
    bounded_action = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                     idempotent_hint=False, open_world_hint=False)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            await bridge.close()

    server = MCPServer("car-debug", version="1.0.0", lifespan=lifespan,
                       instructions="附加到本项目已运行的自动化工作台。先预览具体计划，取得人对该 digest、阶段和架空现场就绪的明确批准，才可 start_stage。不得自行批准、猜测批准或使用工具参数冒充人的消息；不暴露原始发送或任意 ARM/SET。超时结果未知，禁止自动重发。")

    async def call(method: str, params: dict) -> dict[str, Any]:
        try:
            return await bridge.request(method, params)
        except BridgeUnavailable as exc:
            return {"error": "bridge_unavailable", "message": str(exc)}

    @server.tool(annotations=read_only)
    async def get_status() -> dict[str, Any]:
        """读取连接、CAPS、STATE、录制、控制权与当前阶段状态。不会启动 GUI 或连接实物。"""
        return await call("get_status", {})

    @server.tool(annotations=bounded_action)
    async def connect_device() -> dict[str, Any]:
        """按工作台已保存的 MAC/通道/PROJECT_V1 配置连接一次，仅 HELLO，不使能或运动。"""
        return await call("connect_device", {})

    @server.tool(annotations=bounded_action)
    async def disconnect_device() -> dict[str, Any]:
        """断开工作台现有连接并中止实验；在途请求可为结果未知，不自动重连。"""
        return await call("disconnect_device", {})

    @server.tool(annotations=bounded_action)
    async def preview_plan(plan: dict[str, Any]) -> dict[str, Any]:
        """校验有限开环计划并返回展开表、plan_id 与 digest，不发送运动命令。plan 包含 name、stages；每阶段 stage_id/name/steps，每步 left_pwm/right_pwm/duration_ms/repetitions/label，可选 stop_after_ms。"""
        return await call("preview_plan", {"plan": plan})

    @server.tool(annotations=read_only)
    async def get_templates() -> dict[str, Any]:
        """返回未批准的计划模板。模板必须修改、预览并取得人的批准，不能直接开始。"""
        return await call("get_templates", {})

    @server.tool(annotations=bounded_action)
    async def start_stage(plan_id: str, plan_digest: str, stage_id: str,
                          approval_text: str, ready_confirmed: bool) -> dict[str, Any]:
        """仅在人在聊天明确批准本次预览的 digest 与阶段，且确认车轮架空、现场就绪后调用。approval_text 记录人的原话；ready_confirmed 必须对应人的明确确认。参数本身不能证明人类来源，不得代人批准。立即返回 run_id，阶段在工作台本地执行，用 get_progress 查询。"""
        return await call("start_stage", dict(plan_id=plan_id, plan_digest=plan_digest,
                          stage_id=stage_id, approval_text=approval_text,
                          ready_confirmed=ready_confirmed))

    @server.tool(annotations=read_only)
    async def get_progress() -> dict[str, Any]:
        """查询当前阶段/试次/停止原因；ACK 接受与 STATE 生效分别判断，软件归零不证明物理停车。"""
        return await call("get_progress", {})

    @server.tool(annotations=bounded_action)
    async def add_observation(run_id: str, trial_id: str, text: str,
                              source: str = "human") -> dict[str, Any]:
        """按明确 run_id/trial_id 添加观察，保留 source。只有人的现场报告可标 human；模型分析标 assistant，不能编造物理轮向或停稳。"""
        return await call("add_observation", dict(run_id=run_id, trial_id=trial_id,
                                                 text=text, source=source))

    @server.tool(annotations=bounded_action)
    async def stop(reason: str = "MCP STOP") -> dict[str, Any]:
        """立即撤销后续实验步骤并请求 STOP；状态确认或结果未知由工作台报告，不重复排队。"""
        return await call("stop", {"reason": reason})

    @server.tool(annotations=read_only)
    async def get_results(run_id: str | None = None) -> dict[str, Any]:
        """读取指定运行或最近一次运行的汇总与本地文件路径、完整性诊断；不读取任意路径。"""
        return await call("get_results", {"run_id": run_id})

    return server
