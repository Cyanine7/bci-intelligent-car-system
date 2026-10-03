# bci-intelligent-car-system

脑控小车系统的 MCU 固件工程与 Windows 上位机。

- [MCU 工程与开发说明](bci-intelligent-control/README.md)：STM32 / FreeRTOS / Keil，PROJECT_V1 协议与有限运动控制。
- [上位机安装与使用](上位机/README.md)：Python / PySide6，串口与蓝牙 SPP、实验、参数和记录。
- [PROJECT_V1 协议](bci-intelligent-control/docs/PROJECT_V1协议.md)

本仓库保存源码、文档、测试与界面截图。虚拟环境、编译输出、采集数据和本机连接配置按各目录 .gitignore 排除。固件请按 MCU 工程的 build.ps1 重新构建；上位机首次使用请按 README 创建虚拟环境。

软件验证与实车验收分别记录，详见两个工程的验证文档。
