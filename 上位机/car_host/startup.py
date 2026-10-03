"""One cancellable startup attempt; link failures never schedule retries."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

from .preferences import AppPreferences


class StartupAutoConnector(QObject):
    def __init__(self, controller, preferences_provider: Callable[[], AppPreferences],
                 *, delay_ms: int = 500, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.preferences_provider = preferences_provider
        self._consumed = False
        self.attempted = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._connect_once)

    @property
    def pending(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        if self._consumed or self.pending:
            return
        try:
            preferences = self.preferences_provider()
        except (ValueError, TypeError) as exc:
            self._consumed = True
            self.controller.add_log(f"启动自动连接已取消：配置无效：{exc}", "WARNING")
            return
        if not preferences.auto_connect:
            self._consumed = True
            return
        self._timer.start()

    def cancel(self) -> None:
        """Manual interaction consumes the attempt for this application run."""
        self._timer.stop()
        self._consumed = True

    def _connect_once(self) -> None:
        if self._consumed:
            return
        self._consumed = True
        host = self.controller
        if (getattr(host, "_shutting_down", False) or host.worker is not None
                or getattr(host, "scanner", None) is not None or host.connected):
            return
        try:
            preferences = self.preferences_provider()
            if not preferences.auto_connect:
                return
            config = preferences.profile.to_connection_config()
        except (ValueError, TypeError) as exc:
            host.add_log(f"启动自动连接已取消：配置无效：{exc}", "WARNING")
            return
        self.attempted = True
        host.add_log(f"启动自动连接：{preferences.profile.alias} · {config.bluetooth_address}；"
                     "仅尝试一次，失败或掉线后请手动连接")
        host.connect_device(config)
