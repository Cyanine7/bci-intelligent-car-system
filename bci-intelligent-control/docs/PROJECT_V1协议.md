# PROJECT_V1 双端协议 v1

2026-10-23单轮轮速上限由300mm/s提高到1180mm/s（PWM 16500按负载模型K≈7.21e-5折算约1.19m/s，留模型误差余量）。SET_SPEED目标、CAPS/PROFILE_CAPS回传及上位机校验同步更新；时长、时效和停止规则不变。

2026-10-15新增[速度阶跃扩展版本2](速度阶跃扩展V2协议.md)：基础帧和既有消息不变，新请求0x0B～0x12显式协商后使用，binary64配置、新开环/闭环两段、输出16500、总时长3000ms及100Hz缓存。旧接口6000/1000、10000/2000限制保留。

2026-10-08非零稳态辨识扩展：开环PWM上限±10000，固定PWM及两段profile总时长上限2000ms；CAPS/PROFILE_CAPS回传实际上限。轮速PI仍±300mm/s、最多1000ms、输出限幅±6000。帧格式、100Hz控制、20Hz遥测、停止及不续租规则不变；旧固件6000/1000仍按实际能力门控。本次测量专用BIN见工作区tmp/motor_measurement_build_20261008，原工程已有未完成IMU移植保留，专用构建沿用既有电机任务配置。

2026-10-08累计扩展更新。USART2仍为230400/8N1和蓝牙SPP，硬件初始化流程保持。按用户实测更新差速轮径66mm、轮心距160mm；编码器13线/四倍频/N30的名义参数保持待五圈核对。协议帧版本仍为1，扩展显式协商；实物验收与软件验证分开记录。

## 帧

全部整数小端，禁止直接发送 C 结构体。帧为 `A5 5A | version:u8=1 | type:u8 | length:u16 | session:u32 | seq:u32 | payload | crc:u16`，头部14字节，总长16+length，length<=64。CRC16 CCITT-FALSE 覆盖 version 至 payload（不含 A5 5A），poly=0x1021、init=0xFFFF、无反转、xorout=0。半帧100ms超时。有界 RX 512字节、TX 1024字节；溢出锁存停止并诊断。

HELLO 使用随机非零 session、seq=1，停止并创建新会话。同一次启动记住128个已用会话，重放HELLO只停止并拒绝；会话表耗尽拒绝新会话，需重启MCU。后续指令 session 必须匹配，seq 必须严格递增（本版不允许回绕，重建会话）。MCU 重启后 session=0。连接成功不启用能力；有效 CAPS 才启用。重连不重发参数、使能或运动。STOP 是例外：任何 session 的有效 STOP 都停止，ACK 回显请求身份；同会话的新STOP同时推进last_seq，防止迟到的旧ARM/SET重新启动。

## 消息表

| type | 消息 | payload | 行为 |
|---|---|---|---|
| 01 | HELLO | 空 | 停止、重置会话，返回 CAPS 和 ACK |
| 02 | ARM | mode:u8 (1=PWM,2=轮速PI) | 校验本地 EN/电压/Flag_Stop，目标归零；PI需两轮至少各一非零系数；空闲使能5秒到期 |
| 03 | SET_PWM | left:i16,right:i16,duration_ms:u16,deadline_tick:u32 | 需要PWM使能；软件上限±10000、1..2000ms；到期停止并失能 |
| 04 | STOP | 空 | 清除目标、积分和使能 |
| 05 | SET_SPEED | left_mm_s:i16,right_mm_s:i16,duration_ms:u16,deadline_tick:u32 | 需要PI使能；每轮±1180mm/s、1..1000ms；到期停止并失能 |
| 06 | GET_PARAMS | 空 | 返回 PARAMS 和 ACK |
| 07 | SET_PARAMS | kp_left_q100:u32,ki_left_q100:u32,kp_right_q100:u32,ki_right_q100:u32 | 必须失能，系数0..2000000，原子应用到RAM并revision++；不写Flash，返回 PARAMS和ACK |
| 08 | SET_PWM_PROFILE | 见下节，20字节 | 显式扩展协商后，一次原子提交有限两段PWM；PWM ARM且inactive才接受 |
| 09 | GET_PROFILE_CAPS | 空 | 显式、只读、失能时查询；返回PROFILE_CAPS和ACK，本会话随后使用STATE_PROFILE |
| 0A | GET_ENCODER_CAPS | 空 | 失能时显式查询，返回ENCODER_CAPS和ACK，随后独立上传20Hz全量快照；不使能、不清零、不运动 |
| 81 | CAPS | features:u16,pwm_limit:u16,speed_limit_mm_s:u16,max_duration_ms:u16,control_hz:u16,state_hz:u16,car_mode:u8 | features=0x0F: 遥测、参数、PWM、PI；car_mode沿用框架选择，不改变电位器参数 |
| 82 | ACK | command_type:u8,status:u8 | header seq/session回显；0接受，1错误长度/值，2错误会话，3旧序号，4未使能/模式不符，5本地禁止，6已使能不能调参，7PI未配置，8已过期，9未知类型 |
| 83 | STATE | tick_ms:u32,last_seq:u32,encoder_l:i32,encoder_r:i32,speed_l_mm_s:i16,speed_r_mm_s:i16,pwm_l:i16,pwm_r:i16,battery_mv:u16,battery_percent:i16,mode:u8,armed:u8,stop_reason:u8,local_enable:u8,rx_dropped:u32,tx_dropped:u32,param_revision:u32,reserved:u32=0 | 共48字节，末尾44..47为保留字段；20Hz，session为当前会话；正轮速/PWM表示框架定义的向前，实物极性待确认 |
| 84 | PARAMS | revision:u32,kp_left_q100:u32,ki_left_q100:u32,kp_right_q100:u32,ki_right_q100:u32 | header身份关联GET/SET请求；回读确认实际RAM值 |
| 85 | PROFILE_CAPS | extension_features:u16,profile_version:u8,max_segments:u8,pwm_limit:u16,max_total_ms:u16,control_hz:u16,state_hz:u16 | 12字节；features=3：bit0有限两段profile、bit1完整STATE_PROFILE；版本1/段数2/6000/1000/100/20 |
| 86 | STATE_PROFILE | 原STATE的48字节 + profile_seq:u32,profile_start_tick:u32,segment1_actual_tick:u32,segment_index:u8,profile_status:u8,reserved:u16=0 | 64字节完整遥测；扩展协商后20Hz替代STATE，一帧一个样本 |
| 87 | ENCODER_TOTALS | version:u16,flags:u16,epoch_tick:u32,sample_tick:u32,sample_seq:u32,total_left:i64,total_right:i64,interval_ms:u32,delayed_samples:u32 | 40字节，独立20Hz累计快照；不代替或刷新STATE |
| 88 | ENCODER_CAPS | version:u8,bits:u8,sample_hz:u16,report_hz:u16,reserved:u16,nominal_counts_per_turn:u32,wheel_diameter_um:u32,wheel_center_spacing_um:u32 | 20字节，版本1/64位/100Hz/20Hz/保留0；几何和名义计数取实际初始化配置 |

## 全量累计编码器扩展

Balance_task每次实际Read_Encoder后，以与STATE一致的左/右向前符号调用Project_EncoderSample。失能和本地禁止期间仍累计手动转轮计数，不额外读取或清零硬件计数器。累计从首次定时采样开始，HELLO/ARM/STOP、录制和显示操作均不清零；MCU复位后重新开始。

累计内部使用uint64模2^64加法，线格式为二补码i64。差值按模2^64的最短有符号差计算。epoch_tick是首次定时采样tick；sample_seq每次实际采样递增，sample_tick与interval_ms为最新采样信息；delayed_samples统计传入间隔0或超过30ms的样本。flags bit0表示累计已初始化，其余位0。

ENCODER_TOTALS头部seq有独立序列，不插入STATE的序列空间；sample_seq/tick按u32回绕。快照包含所有控制周期的总和，因此丢掉中间无线快照不等于丢掉累计计数。它不是100Hz逐周期轨迹，尚不能替代完整阶跃采样或实际PWM写入时刻。

普通HELLO只返回原CAPS/STATE，不自动发送0A或启用扩展。新Host需同时取得匹配ENCODER_CAPS与accepted ACK才允许五圈标定；旧MCU拒绝0A时不使用STATE稀疏增量伪造累计。Host基线与终点须同连接/会话/epoch、新鲜且无新增迟到采样和完整性失败，结束前累计值至少200ms未变化、轮速0。基线和终点保存独立marker，不能跨复位相减。

stop_reason: 0=启动/会话重建，1=用户STOP/本地按键，2=运动到期，3=本地禁止，4=RX/TX溢出，5=ARM空闲到期。mode失能时为0。STATE 中 PWM 为下发给框架极性适配前的软件输出，零PWM为现有 Set_Pwm(0,0,SERVO_INIT)；不宣称驱动断电或车轮实际静止。

STATE.last_seq 是当前会话已消费的合法帧指令序号，拒绝的指令也推进水位，不表示执行成功。控制/停车每10ms检查一次，1ms的期限不会带来1ms硬实时停车；停止存在最多一个正常控制周期的量化延迟。调度恢复时发现采样间隔超过30ms会停止，但没有新增独立硬件看门狗，不能保证永久控制任务停滞时的软件停车。

黄金向量：session=1234、seq=1、空HELLO为 `A5 5A 01 01 00 00 D2 04 00 00 01 00 00 00 F4 51`；CRC标准检查串 `123456789` 结果为 `0x29B1`。双端测试与本机实际C核心均验证同一线格式。

## 运行与参数

例程TIM7周期功能由框架原有100Hz Balance_task调用；显示/电池/蜂鸣器由20Hz show_task，LED保持原任务。本地 USER单击停止，双击触发蜂鸣提示，不用按键启动固定PWM。编码器保持 Read_Encoder(2/3)、右轮符号换向和原有轮径/分辨率。PWM保持左轮负号、右轮正号的 Set_Pwm 适配。

PI为100Hz增量式：`u += kp*(error-error_previous)+ki*error`，error单位m/s；q100表示系数乘100。ki为每个10ms采样的系数，不等同于按秒归一化的Ki。默认四项为0，须先开环测量/人工调参才能使用PI。限制输出±6000，并在目标为零、失能、到期或故障时清除状态。没有Kd和参数持久化。

上位机利用最新STATE的设备tick与采集后经过时间估算deadline，经过时间按实际CAPS.control_hz的周期向下取整，避免100Hz量化时将当前tick高估1..9ms而使1000ms命令被拒。采集时间和500ms新鲜度门槛不变，deadline仍只计算一次、duration<=1000；MCU同时检查绝对deadline和本地接收duration，取较早者。排队延迟会缩短或拒绝命令；ACK/参数读取/噪声不续租。已使能时重复ARM、执行期间再次SET均返回status4，不能延长本次实验；须STOP或到期失能后重新ARM。这是受限台架策略，没有完成时钟同步误差标定，不能作为正式脑控时效验收。

无线工具发送裸 `reset` 必须仍可进入BootLoader，但合法二进制帧中的相同字节不能触发复位。新接收路径对完整候选帧隔离reset匹配，最近二进制输入后100ms内也屏蔽裸reset，避免坏帧残余触发复位；不再调用旧AT字符串截获，模块文本作为噪声丢弃。新协议上位机禁用原始发送；烧录前停止运动并断开上位机，由烧录工具独占蓝牙连接。

## 有限两段PWM扩展（2026-10-04）

基础CAPS仍为13字节、features=0x0F；基础STATE仍为48字节、末4字节为0，版本号保持1。正式启动连接只有既有HELLO，不自动发送扩展查询、ARM或运动。用户在测量工作台或profile预览准备时显式调用GET_PROFILE_CAPS；命令必须失能，已使能返回ACK4。只有请求身份匹配的PROFILE_CAPS和accepted ACK同时确认后，Host才允许profile。未知扩展features或非法限制拒绝启用；旧固件对0x09返回ACK9时，Host保留固定PWM等基础功能并表示profile不支持。没有响应时结果未知，不重发。

新MCU仅在合法GET_PROFILE_CAPS后切换该会话为STATE_PROFILE；每50ms只发送这一种完整遥测。未查询的旧Host继续收到原CAPS/STATE，不接触未知feature位和新消息。新HELLO重置格式协商；重复查询、ACK和参数查询不续租运动。未协商即发送SET_PWM_PROFILE返回ACK9。profile不改变现有ARM、SET_PWM、SET_SPEED、STOP、seq水位及本地停机契约。

SET_PWM_PROFILE payload小端格式 `<BBHhhhhHHI>`，依次为 `profile_version:u8=1,segment_count:u8=2,reserved:u16=0,left0:i16,right0:i16,left1:i16,right1:i16,first_ms:u16,total_ms:u16,deadline_tick:u32`。两段每轮幅值≤6000，`1<=first_ms<total_ms<=1000`；同一轮两段非零PWM不得异号，允许第二段0和两段相同PWM。单轮测量须将另一侧两段都设0。参数在完整校验后原子接受，绝不先执行半张表。

接受时 `profile_start_tick=now`，计划切换tick=`now+first_ms`，有效截止=`min(now+total_ms,deadline_tick)`（按uint32回绕比较）。期限只设置一次。100Hz任务先检查到期和本地停机，再处理切段与输出；首段末端晚于或等于有效截止时第二段不执行。正常量化至下一个控制tick，真实切换tick以软件输出改为第二段的该次tick记录，不用无线收包时间替代。总时长不足一个控制周期可能没有第二段执行证据，报告不能据此判维持失败。

STATE_PROFILE头部seq仍为遥测report序号；`profile_seq`为本次profile命令seq，不能由STATE.last_seq替代。`segment_index=0/1`表示最近实际执行的段，255表示没有执行；`profile_status=0/1/2`为无profile/活动/已结束。无profile时seq/start/actual均0且index255；结束时保留本次seq、anchor、最后实际段和第二段实际切换tick，下一次成功ARM或新HELLO清除。index1时actual可以为0（有效tick回绕）；index0/255时actual必须为0。接受后同一控制tick被STOP、尚未输出时，保留status2/index255，不能把accepted ACK计为实际执行。

所有既有停机入口清除剩余段和目标，包括到期、任意session有效STOP、本地按键/禁止、控制采样迟到超过30ms、RX/TX溢出。切段不再次ARM，不依赖主机高频SET，不放宽1000ms或500ms新鲜度门槛。两段相同PWM也以实际index区分。STATE中的PWM与切换tick仅证明MCU软件输出；不证明驱动端电压、转动、失电、自由滑行或机械停车。

新增黄金向量（session1234）：

- GET_PROFILE_CAPS，seq2：`a55a01090000d204000002000000a668`。
- PROFILE_CAPS，seq2，features3/version1/2段/6000/1000/100/20：`a55a01850c00d204000002000000030001027017e803640014009b8a`。
- SET_PWM_PROFILE，seq4，1400/0→900/0、400+600ms、deadline1100：`a55a01081400d2040000040000000102000078050000840300009001e8034c040000e22f`。
- STATE_PROFILE，report seq7，tick600/last_seq4、左17mm/s/PWM900、profile seq4/anchor100/actual500/index1/status1：`a55a01864000d204000007000000580200000400000001000000000000001100000084030000e02e500001010001000000000000000000000000000000000400000064000000f4010000010100003331`。

黄金帧及分段停机由Python独立CRC、实际C核心和Host↔C字节桥验证；新增BIN的编译/分区/HEX一致性与实物烧录、CAPS回读、切段输出及维持测量分别验收。本文不表示已烧录或已完成新profile实车测量。


2026-10-22新增独立[角速度实验V1](角速度实验协议V1.md)，ARM mode5和0x13～0x1B命令须显式能力协商。单段5s、总10s仅适用于新角速度实验，不改变基础命令和速度V2的限制。
