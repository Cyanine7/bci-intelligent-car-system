"""官方 MCP SDK 的 stdio 服务；工具仅转发到已有 GUI。"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .client import BridgeClient, BridgeUnavailable


TOOL_NAMES = frozenset({"get_status", "connect_device", "disconnect_device", "preview_plan",
                        "get_templates", "start_stage", "get_progress", "add_observation",
                        "stop", "get_results", "start_plan", "get_communication_health", "suggest_refinement",
                        "query_encoder_totals"})


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

    server = MCPServer("car-debug", version="1.2.0", lifespan=lifespan,
                       instructions="附加到已有工作台。运行前预览冻结具体digest并核对实际任务授权及架空现场就绪。已有人的完整测量授权可在明确预算、PWM、时长边界内沿用，无需重复许可；必须记录实际原话，不得伪造来源或自动扩权。细扫仅生成新草稿，不自动改当前表或执行。旧固件无实际分段CAPS时不能发送profile。不暴露原始发送或任意ARM/SET；超时未知不重发，STOP永久取消后续。")

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
    async def query_encoder_totals() -> dict[str, Any]:
        """显式查询全量编码器能力并启用20Hz累计快照；需失能、新鲜STATE和无在途动作。仅发送0A，不ARM、不清零计数、不运动；旧固件拒绝时不猜测或重发。"""
        return await call("query_encoder_totals", {})

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
        """校验并冻结，不发送运动。旧固定PWM表兼容；schema_version=2需suite={suite_id,total_entered_budget(1..4096)}，阶段stage_id/name/block_id/rest_before_ms/steps。步骤固定PWM或两段segments=[{left_pwm,right_pwm,duration_ms},...]二选一，control_type=candidate/high_hold/zero_output/fixed_pwm，repetitions/label/stop_after_ms可选。每表最多128试次，PWM幅值最多10000、总运动最多4000ms；执行仍按实际CAPS较小上限门控，旧2000ms固件不会因此获得4000ms能力。修改或补测须新预览。"""
        return await call("preview_plan", {"plan": plan})

    @server.tool(annotations=read_only)
    async def get_templates() -> dict[str, Any]:
        """返回未批准的计划模板。模板必须修改、预览并取得人的批准，不能直接开始。"""
        return await call("get_templates", {})

    @server.tool(annotations=bounded_action)
    async def start_stage(plan_id: str, plan_digest: str, stage_id: str,
                          approval_text: str, ready_confirmed: bool) -> dict[str, Any]:
        """运行指定阶段。须核对本次digest在人的实际任务授权内并已架空就绪；已有完整测量授权可沿用，approval_text记录实际原话。参数本身不能证明人类来源，不得代人批准或扩大边界。立即返回run_id，用get_progress查询；不自动进入其他阶段。"""
        return await call("start_stage", dict(plan_id=plan_id, plan_digest=plan_digest,
                          stage_id=stage_id, approval_text=approval_text,
                          ready_confirmed=ready_confirmed))

    @server.tool(annotations=bounded_action)
    async def start_plan(plan_id: str, plan_digest: str, approval_text: str,
                         ready_confirmed: bool) -> dict[str, Any]:
        """运行当前整张有限冻结表及静置任务；真实授权、现场就绪、CAPS/新鲜STATE/完整录制均须成立。MCP独立心跳；STOP取消全部剩余段、试次和等待，不恢复或重发。可沿用已明确的完整测量授权，不能制造新授权、改变预算或用连接推定profile能力。"""
        return await call("start_plan", dict(plan_id=plan_id, plan_digest=plan_digest,
            approval_text=approval_text, ready_confirmed=ready_confirmed))

    @server.tool(annotations=read_only)
    async def get_communication_health() -> dict[str, Any]:
        """读取当前会话各层采集年龄、交付延迟、解析/缓存/录制计数及停止观测；不连接、不ARM。UTC/monotonic/device tick不同来源，不把RPC往返当状态年龄，不称机械停时。"""
        return await call("get_communication_health", {})

    @server.tool(annotations=read_only)
    async def suggest_refinement(side: str, direction: int,
                                 run_ids: list[str] | None = None) -> dict[str, Any]:
        """仅从已终结且原始校核通过的profile运行生成有预算的新细扫草稿。side=left/right，direction=-1/1；至少3时间块每档3次，混合/非单调/不足结果保持未定。先50后25PWM步长，对照与锚点计预算。不修改冻结版本、不自动执行；草稿需新preview_plan。"""
        return await call("suggest_refinement", dict(side=side, direction=direction, run_ids=run_ids))

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
    async def get_results(run_id: str | None = None, suite_id: str | None = None) -> dict[str, Any]:
        """读取指定运行或最近一次运行的汇总与本地文件路径、完整性诊断；不读取任意路径。"""
        return await call("get_results", {"run_id": run_id, "suite_id": suite_id})

    return server
