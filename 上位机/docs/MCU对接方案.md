# MCU 与上位机对接

2026-10-03。本轮按用户授权实现 MCU 驱动移植、PROJECT_V1 与上位机功能，保持框架硬件初始化流程及参数。完整字节定义以 [PROJECT_V1 双端协议 v1](../../bci-intelligent-control/docs/PROJECT_V1协议.md) 为准，操作步骤见 [上位机使用说明](PROJECT_V1使用.md)。

## 当前实现

| 部分 | 已实现行为 |
|---|---|
| 数据口 | USART2 原有 230400 / 8N1；蓝牙 SPP 或对应 COM 传输；初始化与轮径 / 编码器参数保持原样 |
| 帧 | A5 5A、version / type、长度、session、seq、小端 payload、CRC16 CCITT-FALSE；payload ≤64、半帧超时 100 ms |
| 会话 | 非零随机 session HELLO 只停止并握手；能力仅由有效 CAPS 启用，重连不恢复参数、使能或运动 |
| 状态 | 48 字节 STATE（含末尾 4 字节零保留），约 20 Hz；signed encoder / wheel speed、软件 PWM、tick、使能、停止原因、revision 和丢失累计 |
| 实验 | 显式 ARM 后单次 SET_PWM / SET_SPEED；CAPS 上限、500 ms 新鲜 STATE、duration ≤1000 ms、不可续租绝对 deadline；到期失能 |
| 参数 | 左右轮四项 kp / ki q100，100 Hz 增量 PI；失能时原子应用到 RAM，PARAMS 回读与 revision；无 Kd / Flash 持久化 |
| 结果 | QUEUED、链路写出、匹配 MCU ACK、参数实际回读与 STATE 分开；2 秒协议超时结果未知，无自动重发 |
| 停止 | STOP 清目标 / PI 状态并失能；到期、本地禁止 / 按键和溢出均由 MCU 停止；零软件 PWM 不宣称电机断电或物理静止 |

ARM 不得对已使能设备重新使能。执行中的 SET 不得重叠或延长期限，后续实验须到期失能或先 STOP。STOP 在相同会话且新 seq 时推进 last_seq，阻止其之前迟到的旧控制重新启动。HELLO 会话重放不能重置序号；MCU 有界保存本次启动最多 128 个已用 session，耗尽需重启。CRC 用于传输差错检测，session / seq 用于请求身份和旧指令隔离，均不等同于无线认证。

## 旧固件与现有无线烧录

LEGACY_APP 仍接受 `{C左轮速度幅值:右轮速度幅值:估算电量}$`，整数轮速 /100 为 m/s。它不含方向、设备 tick、CRC、ACK 和参数能力；上位机为旧固件保留原始单次发送，参数 / 运动请求返回 unsupported。模拟设备仅支持这份旧协议。

厂商完整参考固件 A/B/C 帧与 LEGACY_APP 不相同，不能根据相同字母推断参数接口。新 BIN 使用 PROJECT_V1，必须显式切换上位机协议。

用户已验证按课程文档无线烧录成功。使用已有烧录工具，烧录前停止并断开上位机让工具独占蓝牙连接。裸 reset 留给烧录通路；合法二进制候选帧与 reset 匹配隔离，上位机 PROJECT_V1 禁止原始发送。模块型号、UART 配置、MAC / RFCOMM 通道与当前运行固件仍按实物核对；直接 RFCOMM 通道不是 COM 号或电脑端波特率。

## 软件与实车验证边界

上位机软件夹具验证分片、CRC / 长度 / 噪声、session / seq、tick 回绕与回放、新鲜 STATE、显式 ARM、CAPS 上限、单次运动、参数实际 / 草稿、超时未知、STOP 槽位、重连无重发和录制。MCU 编译 / 核心逻辑验证见工程文档与根项目记忆；编译和假链路均不操作小车。

实车先架空驱动轮，核对板卡、编码器轮向 / 分辨率、框架正 PWM 与轮速极性，再做单轮短时开环测量、STOP / 到期 / 本地禁止、断线后的有限期限停车和记录完整性。根据测量人工配置增量 PI 后，验证受限轮速跟踪。完整时钟误差、实际静止判据、脑控接口、自动整定、v/omega 和正式运动控制仍未实现，不能作为实车验收结论。
