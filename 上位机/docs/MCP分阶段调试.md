# 本地 MCP 与分阶段开环调试

2026-10-04，上位机 v0.5.0。本轮仅开发与软件验证，没有实车连接、控制、编译或烧录。架空实车的轮向、机械停止、真实链路时延与故障行为仍须分别验收。

## 启动和接入

现有上位机独占蓝牙。新增 `AutomationService` 在 Qt 主线程执行实验；本机 TCP 桥只监听 `127.0.0.1` 的随机端口，独立 MCP 进程通过 stdio 向 Codex 提供工具。MCP 不导入 Qt，不打开串口或蓝牙，不自动启动上位机。

1. 在上位机目录运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-mcp.ps1`，创建独立 `.venv-mcp` 并安装锁定的官方 MCP SDK 与依赖。现有 `.venv` 用于 Qt 上位机和 pytest。
2. 双击 `启动假设备调试.bat`，或运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start-automation.ps1 -Demo`。演示模式只接受 `PROJECT_V1_FAKE` 注入设备；扫描和连接真实设备都会拒绝。启动不会连接假设备，使用 MCP `connect_device` 显式连接。
3. 实车使用 `启动自动化调试.bat`，或 `.\.venv\Scripts\python.exe -m car_host --automation --no-auto-connect`。仍需显式连接，采用保存的 MAC、通道与 PROJECT_V1 配置。
4. 注册本目录交付的 [MCP 配置示例](../mcp-config.example.toml)，或在本目录执行下面的 Codex CLI 命令。注册后重新加载客户端 MCP 工具；若当前客户端没有重载入口，重启 Codex。SDK 初始化及工具发现不要求 GUI 已开启；GUI 未开启时工具返回接口不可用。

```powershell
$carHostPath = (Get-Location).Path
codex mcp add car_debug -- (Join-Path $carHostPath '.venv-mcp\Scripts\python.exe') (Join-Path $carHostPath 'car_debug_mcp\launch.py') --project-directory $carHostPath
codex mcp get car_debug --json
```

本工作区已实际注册全局 `car_debug`，存于 `C:\Users\Lenovo\.codex\config.toml`，保持其余 MCP 配置不变。绝对入口 `launch.py` 不依赖客户端工作目录；移动工程后应重新注册路径。运行环境完整依赖锁为 `requirements-mcp-lock.txt`（本机 Windows / Python 3.14 验证）；只安装运行依赖，测试依赖另在 `requirements-mcp-dev.txt`。

可发现的 10 个工具为 `get_status`、`connect_device`、`disconnect_device`、`get_templates`、`preview_plan`、`start_stage`、`get_progress`、`add_observation`、`stop` 和 `get_results`。工具发现已经通过官方 SDK 的真实 stdio 初始化与列举验收；既有聊天需要重载工具才能调用新增服务。

普通 `启动上位机.bat` 保留原有启动时尝试一次连接的行为；只有 `--automation` 开启控制桥。自动化脚本显式携带 `--no-auto-connect`，不修改保存的偏好。已有旧窗口需退出并重新启动。

控制桥通过 `.runtime/automation.json` 发布运行期端点及秘密令牌。该目录不纳入源码；不要上传端点文件或把令牌复制进日志。每个请求/响应最多 64 KiB，客户端最多 8 个，连接缓冲、在途请求和输出队列有界。同一项目用锁文件防止多个控制桥实例。关闭时删除当前端点。

## 工具与批准

工具包括状态读取、按保存配置连接/断开、模板、实验表预览、阶段启动、进度、观察追加、STOP 和结果索引。不暴露任意原始字节、直接 ARM/PWM 或 PI 修改工具。

`preview_plan` 接收以下草稿结构。此示例的 PWM 为 `null`，必须先填入明确讨论过的值才能通过校验；不是运动授权。

```json
{
  "name": "待确认的架空单轮实验",
  "stages": [{
    "stage_id": "left_repeat",
    "name": "左轮重复启动",
    "steps": [{
      "left_pwm": null,
      "right_pwm": 0,
      "duration_ms": 500,
      "repetitions": 5,
      "label": "待确认 PWM"
    }]
  }]
}
```

每阶段 1 个或多个步骤；重复次数展开为带编号的试次。最多 16 阶段、128 试次，单步骤重复 1..100 次。PWM 整数范围 ±6000，时长 1..1000 ms；开始前还按真实 CAPS 的较小上限复核。可指定 `stop_after_ms`，须大于 0 且小于运动时长；省略时按到期停止。提供轮向、重复启动、较长脉冲、速度曲线、到期停止和主动 STOP 六份草稿。

预览返回随机批准版本 `plan_id`、内容 SHA256 `digest` 和展开的完整实验表。上位机“查看实验表与进度”窗口同步显示。修改实验表参数须重新预览，使旧批准版本失效；内容相同的重新预览也创建新批准版本。一个批准版本只启动一个阶段，后续阶段需重新预览并逐阶段批准。

助手必须先展示当前阶段全部 PWM、时长、重复次数和停止条件，再等待人明确确认该版本及“已架空、现场准备完成”。随后 `start_stage` 使用 `plan_id`、`plan_digest`、`stage_id`、`approval_text` 和 `ready_confirmed=true`。批准原话会记录；布尔参数与文字本身不能证明人的授权，MCP 客户端/助手必须遵守实际对话授权，不能自行填写确认。

本次批准只覆盖开发与假设备验证，首次实车实验需随后单独批准。既有分析中的 900/1300 PWM 等建议不作为已批准值。

## 阶段运行与中止

阶段要求实际 CAPS 声明开环/参数读取、新鲜当前会话 STATE、本地允许、已失能、无在途请求和运动，且没有另一录制占用。先启动原始录制并读取实际 RAM，保存有采集时间的快照；实验 Journal 准备成功后才允许运动。

初始及每次之间须观察两轮失能、PWM 归零、零反馈连续至少 1 秒；最多等 5 秒。每次重新 ARM，等待匹配 ACK 与覆盖该 seq 的 STATE，再发送一次有限 PWM。ACK/STATE 确认等待 2 秒，超时结果未知且不重发。主动 STOP 前若已经到期，或没有观察到活动输出，该试次中止并标注观测不足，不能算主动停止通过。

MCP 客户端保持持久 TCP 连接，每秒发送心跳；连接消失或心跳超过 3 秒，中止阶段并尝试 STOP。掉线、会话变化、STATE 超过 500 ms、本地禁止、原始/实验记录失败、缓存丢失和 MCU 丢失累计变化同样中止。STOP 不保证穿透故障链路，日志分别保存排队、ACK 与 STATE 确认或未知。

运行期间人工 ARM/运动/参数读写被协调层拒绝，按钮禁用并显示原因；STOP 始终保留。人工断开、关闭或停止录制会先取消阶段。取消不可恢复，后续 ARM/PWM 不再发送。图形与日志暂停仍然采集和录制。阶段结束保存报告后释放控制权，不自动执行下一阶段、重连、恢复参数或重发命令。

零反馈 1 秒是软件继续条件，不能证明机械停稳。使用者必须在旁监督并记录物理方向、停稳和异常。首版自动停止试验为到期及主动 STOP；本地开关/断联作为故障中止场景，实车专项验收另行安排。

## 证据与指标

每阶段拥有独立目录。实车位于 `data/`；假设备位于 `.qa/automation_demo/`，来源为 `SIMULATION_PROJECT_V1`，不混作实车结果。

|文件|内容|
|---|---|
|telemetry.csv / communication.log|现有原始遥测、HEX 通信与完整性尾记录|
|plan.json / context.json|冻结批准表、会话、CAPS/实际参数采集时间、批准原话和验证边界|
|events.jsonl|阶段、试次、request_id/session/seq、ACK 与中止事件|
|results.csv / summary.json|逐次与汇总结果；失败和观测不足均保留|
|report.md / speed_curve.svg|中文报告、有符号轮速观测图|
|observations.jsonl|标注 human/assistant 来源的观察，阶段结束后仍可补充|

录制开始于既有连接之后，原始日志可能未覆盖 HELLO/CAPS；快照明确标注，不能补造握手字节或用本地 BIN 散列证明当前固件。

每轮取末 4 条有效输出 STATE，其中至少 3 条轮速与受测 PWM 同号才算持续反馈；少于 4 条为观测不足。零速度是有效失败观察。成功率只统计已完成且观测充分的受测试次；中止/失败、未测试、观测不足从分母排除。指标包括首次同号非零反馈的接收观测延迟、尾段中位数/均值/标准差、电压范围、软件输出观察及停止确认。

观测延迟优先相对 PWM 请求时刻，包含发送、链路和采样延迟，不是精确物理启动时间。20 Hz STATE 编码器只提供最新 10 ms 增量，不累积成完整里程。机械方向和停稳依赖人工观察。后补观察不会改写已生成的报告，查看 `observations.jsonl` 获取补充证据。

只有原始录制结束并检查完整性、实验报告异步保存成功后，阶段才发布完成。记录失败的运行仍可查询错误和已有路径，部分文件可能不存在。内存只保存最近 8 次结果索引，全部历史文件保留在目录中。

## 软件验收与复现

本轮完整 pytest 为 438 项及 4 个子测试通过。Qt 环境跳过的 SDK 专项已在独立 MCP 环境中验证，7 项通过；两个环境 `pip check` 均正常。故障覆盖和 GUI 截图见 [验证记录](验证记录.md)。

在本目录运行以下脚本可验证官方 SDK → stdio MCP → TCP 桥 → Qt 正式入口 → PROJECT_V1 假设备 → 原始记录与报告的完整路径：

```powershell
.\.venv-mcp\Scripts\python.exe scripts\verify_mcp_end_to_end.py
```

脚本拒绝已存在的控制桥，使用 `--automation-demo --no-auto-connect` 与 offscreen GUI；输出均在 `.qa/`，不扫描或连接实车。它列举 10 工具，显式连接软件假设备，执行两次到期停止与一次主动 STOP，验证独立心跳、文件关联和退出清理。其测试 PWM 仅用于假设备，不构成实车实验表或授权。
