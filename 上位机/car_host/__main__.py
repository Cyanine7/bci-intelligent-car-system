"""Run with ``python -m car_host`` from the project directory."""

import sys
from dataclasses import replace
from pathlib import Path

# Package __init__ prepares the Windows/Anaconda DLL search before Qt loads.
from PySide6.QtWidgets import QApplication

from .controller import HostController
from .preferences import PreferencesStore
from .startup import StartupAutoConnector
from .ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("小车调试工作台")
    app.setOrganizationName("BCI Car Lab")
    project_directory = Path(__file__).resolve().parent.parent
    demo = "--automation-demo" in sys.argv
    automation_enabled = demo or "--automation" in sys.argv
    if demo:
        from .project_fake import create_project_fake_worker
        def no_hardware_scanner():
            raise ValueError("假设备演示不扫描真实蓝牙")
        controller = HostController(project_directory / ".qa" / "automation_demo",
                                    worker_factory=create_project_fake_worker,
                                    scanner_factory=no_hardware_scanner)
    else:
        controller = HostController(project_directory / "data")
    store = PreferencesStore(project_directory / "config" / "preferences.json")
    loaded = store.load()
    if loaded.warning:
        controller.add_log(loaded.warning, "WARNING")
    elif not store.path.exists():
        try:
            store.save(loaded.preferences)
        except (OSError, ValueError) as exc:
            controller.add_log(f"初始设备配置保存失败：{exc}", "WARNING")
    preferences = loaded.preferences
    if "--no-auto-connect" in sys.argv or demo:
        preferences = replace(preferences, auto_connect=False)
        controller.add_log("本次启动已禁用自动连接；已保存的配置未修改")
    bridge = None
    if automation_enabled:
        from .automation import AutomationQuitGuard, AutomationService
        from .automation_bridge import AutomationBridge
        service = AutomationService(controller, project_directory, demo=demo)
        bridge = AutomationBridge(service, project_directory)
        if not bridge.start():
            service.shutdown()
            controller.add_log("自动化控制桥启动失败；可能已有实例正在使用", "ERROR")
            return 1
        controller.add_log("本机自动化接口已开启；阶段须在聊天中批准，连接不会使能")
        quit_guard = AutomationQuitGuard(app, controller)
    window = MainWindow(controller, preferences=preferences, preferences_store=store)
    startup = StartupAutoConnector(controller, window.current_preferences, parent=window)
    window.startup_connector = startup
    window.connection_interacted.connect(startup.cancel)
    window.show()
    startup.start()
    app.aboutToQuit.connect(startup.cancel)
    app.aboutToQuit.connect(controller.shutdown)
    if bridge is not None:
        app.aboutToQuit.connect(bridge.close)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
