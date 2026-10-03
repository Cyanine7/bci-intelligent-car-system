# MCU 小车固件

STM32 / FreeRTOS / Keil 工程，与 Python 上位机通过串口或蓝牙 SPP 使用 PROJECT_V1 通信。

工程入口：`USER/WHEELTEC.uvprojx`。当前实现包括显式使能、有限时长 PWM、轮速 PI、ACK / STATE 遥测和四项 RAM 参数。参数默认全零，使用轮速闭环前需要完成开环测量与 PI 整定。

[PROJECT_V1 双端协议 v1](docs/PROJECT_V1协议.md) 说明帧格式、命令、回执与停止语义。配套上位机见 [安装与使用说明](../上位机/README.md)。

在本目录重新构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

构建使用 Keil ARMCC5，并通过 `tools/verify_firmware.py` 检查 HEX / BIN 一致性、Flash 边界、向量和初始化，输出 `firmware_manifest.json`。生成的 BIN / HEX 与编译中间文件不纳入源码仓库。

连接后的 HELLO 仅停止并建立新会话，实际 CAPS 与新鲜 STATE 确认后才能执行相应操作。参数仅保存在 RAM，不写 Flash。软件验证与实车验收分别记录；轮向、PI 稳定性和断联停车需按实物验收结果判断。

此目录的公开文档仅保留 PROJECT_V1 协议。
