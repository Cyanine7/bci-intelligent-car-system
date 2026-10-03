# PROJECT_V1 双端协议 v1

2026-10-03。本次实现使用 USART2 原有 230400/8N1 和蓝牙 SPP，所有硬件初始化及参数保持原样。新固件使用二进制 PROJECT_V1；旧 LEGACY_APP 仅供旧固件使用。此协议用于有限时长台架调试，尚未实车验收。

## 帧

全部整数小端，禁止直接发送 C 结构体。帧为 `A5 5A | version:u8=1 | type:u8 | length:u16 | session:u32 | seq:u32 | payload | crc:u16`，头部14字节，总长16+length，length<=64。CRC16 CCITT-FALSE 覆盖 version 至 payload（不含 A5 5A），poly=0x1021、init=0xFFFF、无反转、xorout=0。半帧100ms超时。有界 RX 512字节、TX 1024字节；溢出锁存停止并诊断。

HELLO 使用随机非零 session、seq=1，停止并创建新会话。同一次启动记住128个已用会话，重放HELLO只停止并拒绝；会话表耗尽拒绝新会话，需重启MCU。后续指令 session 必须匹配，seq 必须严格递增（本版不允许回绕，重建会话）。MCU 重启后 session=0。连接成功不启用能力；有效 CAPS 才启用。重连不重发参数、使能或运动。STOP 是例外：任何 session 的有效 STOP 都停止，ACK 回显请求身份；同会话的新STOP同时推进last_seq，防止迟到的旧ARM/SET重新启动。

## 消息表

| type | 消息 | payload | 行为 |
|---|---|---|---|
| 01 | HELLO | 空 | 停止、重置会话，返回 CAPS 和 ACK |
| 02 | ARM | mode:u8 (1=PWM,2=轮速PI) | 校验本地 EN/电压/Flag_Stop，目标归零；PI需两轮至少各一非零系数；空闲使能5秒到期 |
| 03 | SET_PWM | left:i16,right:i16,duration_ms:u16,deadline_tick:u32 | 需要PWM使能；软件上限±6000、1..1000ms；到期停止并失能 |
| 04 | STOP | 空 | 清除目标、积分和使能 |
| 05 | SET_SPEED | left_mm_s:i16,right_mm_s:i16,duration_ms:u16,deadline_tick:u32 | 需要PI使能；每轮±300mm/s、1..1000ms；到期停止并失能 |
| 06 | GET_PARAMS | 空 | 返回 PARAMS 和 ACK |
| 07 | SET_PARAMS | kp_left_q100:u32,ki_left_q100:u32,kp_right_q100:u32,ki_right_q100:u32 | 必须失能，系数0..2000000，原子应用到RAM并revision++；不写Flash，返回 PARAMS和ACK |
| 81 | CAPS | features:u16,pwm_limit:u16,speed_limit_mm_s:u16,max_duration_ms:u16,control_hz:u16,state_hz:u16,car_mode:u8 | features=0x0F: 遥测、参数、PWM、PI；car_mode沿用框架选择，不改变电位器参数 |
| 82 | ACK | command_type:u8,status:u8 | header seq/session回显；0接受，1错误长度/值，2错误会话，3旧序号，4未使能/模式不符，5本地禁止，6已使能不能调参，7PI未配置，8已过期，9未知类型 |
| 83 | STATE | tick_ms:u32,last_seq:u32,encoder_l:i32,encoder_r:i32,speed_l_mm_s:i16,speed_r_mm_s:i16,pwm_l:i16,pwm_r:i16,battery_mv:u16,battery_percent:i16,mode:u8,armed:u8,stop_reason:u8,local_enable:u8,rx_dropped:u32,tx_dropped:u32,param_revision:u32,reserved:u32=0 | 共48字节，末尾44..47为保留字段；20Hz，session为当前会话；正轮速/PWM表示框架定义的向前，实物极性待确认 |
| 84 | PARAMS | revision:u32,kp_left_q100:u32,ki_left_q100:u32,kp_right_q100:u32,ki_right_q100:u32 | header身份关联GET/SET请求；回读确认实际RAM值 |

stop_reason: 0=启动/会话重建，1=用户STOP/本地按键，2=运动到期，3=本地禁止，4=RX/TX溢出，5=ARM空闲到期。mode失能时为0。STATE 中 PWM 为下发给框架极性适配前的软件输出，零PWM为现有 Set_Pwm(0,0,SERVO_INIT)；不宣称驱动断电或车轮实际静止。

STATE.last_seq 是当前会话已消费的合法帧指令序号，拒绝的指令也推进水位，不表示执行成功。控制/停车每10ms检查一次，1ms的期限不会带来1ms硬实时停车；停止存在最多一个正常控制周期的量化延迟。调度恢复时发现采样间隔超过30ms会停止，但没有新增独立硬件看门狗，不能保证永久控制任务停滞时的软件停车。

黄金向量：session=1234、seq=1、空HELLO为 `A5 5A 01 01 00 00 D2 04 00 00 01 00 00 00 F4 51`；CRC标准检查串 `123456789` 结果为 `0x29B1`。双端测试与本机实际C核心均验证同一线格式。

## 运行与参数

例程TIM7周期功能由框架原有100Hz Balance_task调用；显示/电池/蜂鸣器由20Hz show_task，LED保持原任务。本地 USER单击停止，双击触发蜂鸣提示，不用按键启动固定PWM。编码器保持 Read_Encoder(2/3)、右轮符号换向和原有轮径/分辨率。PWM保持左轮负号、右轮正号的 Set_Pwm 适配。

PI为100Hz增量式：`u += kp*(error-error_previous)+ki*error`，error单位m/s；q100表示系数乘100。ki为每个10ms采样的系数，不等同于按秒归一化的Ki。默认四项为0，须先开环测量/人工调参才能使用PI。限制输出±6000，并在目标为零、失能、到期或故障时清除状态。没有Kd和参数持久化。

上位机利用最新STATE的设备tick与接收后经过时间估算deadline，必须有500ms内的新鲜STATE，duration<=1000；MCU同时检查绝对deadline和本地接收duration，取较早者。排队延迟会缩短或拒绝命令；ACK/参数读取/噪声不续租。已使能时重复ARM、执行期间再次SET均返回status4，不能延长本次实验；须STOP或到期失能后重新ARM。这是受限台架策略，没有完成时钟同步误差标定，不能作为正式脑控时效验收。

无线工具发送裸 `reset` 必须仍可进入BootLoader，但合法二进制帧中的相同字节不能触发复位。新接收路径对完整候选帧隔离reset匹配，最近二进制输入后100ms内也屏蔽裸reset，避免坏帧残余触发复位；不再调用旧AT字符串截获，模块文本作为噪声丢弃。新协议上位机禁用原始发送；烧录前停止运动并断开上位机，由烧录工具独占蓝牙连接。
