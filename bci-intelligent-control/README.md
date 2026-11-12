# MCU 小车固件

2026-11-04更新连续控制扩展v2：独立ARM6、100Hz双环和20Hz遥测，2000ms绝对期限；默认普通零目标平滑，TARGET flags bit0保留原按住键模式的松键立即归零。上位机默认离散方向锁存＋WASD独立调速，可切换原操控；需要双端升级和实际v2 CAPS确认。内外环RAM参数及零偏冻结，不自动更改。

连续模式目标角速度上限为3.75rad/s（最大差速0.6m/s / 轮距0.16m），由实际连续CAPS报告；本次RAM差速限幅继续生效。角速度有限实验ARM5仍保持原上限。

2026-10-22新增角速度实验V1：ARM5、IMU角速度PI外环、双轮带目标前置滤波的速度PI内环，支持前进/原地转向与横向开环对照。默认2s＋2s，单段≤5s、总计≤10s，1040个84字节记录槽位；与原速度V2互斥共用记录存储。控制/IMU故障及STOP取消后段，配置仅写RAM。


2026-10-15新增速度阶跃扩展v2：新开环mode4和新闭环mode3、两段总3000ms、输出限幅16500、binary64 PI参数及384条100Hz缓存。算法填写指南列出由用户完成的三个函数；生产PI和目标前置滤波标记均已启用；实际执行仍以设备CAPS为准。

STM32 / FreeRTOS / Keil 工程，与 Python 上位机通过串口或蓝牙 SPP 使用 PROJECT_V1 通信。

工程入口：`USER/WHEELTEC.uvprojx`。当前实现包括显式使能、有限时长 PWM、轮速 PI、ACK / STATE 遥测和四项 RAM 参数。参数默认全零，使用轮速闭环前需要完成开环测量与 PI 整定。

[PROJECT_V1 双端协议 v1](docs/PROJECT_V1协议.md) 说明帧格式、命令、回执与停止语义。配套上位机见 [安装与使用说明](../上位机/README.md)。

在本目录重新构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

构建使用 Keil ARMCC5，并通过 `tools/verify_firmware.py` 检查 HEX / BIN 一致性、Flash 边界、向量和初始化，输出 `firmware_manifest.json`。生成的 BIN / HEX 与编译中间文件不纳入源码仓库。

连接后的 HELLO 仅停止并建立新会话，实际 CAPS 与新鲜 STATE 确认后才能执行相应操作。参数仅保存在 RAM，不写 Flash。软件验证与实车验收分别记录；轮向、PI 稳定性和断联停车需按实物验收结果判断。

公开协议见 docs/PROJECT_V1协议.md；实验操作见配套上位机文档。
