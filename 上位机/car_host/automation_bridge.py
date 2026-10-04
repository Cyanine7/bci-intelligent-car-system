"""GUI 线程内的回环控制桥；只转发已定义的实验服务方法。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QLockFile, QTimer
from PySide6.QtNetwork import QHostAddress, QTcpServer


MAX_MESSAGE_BYTES = 64 * 1024
MAX_CLIENTS = 8
MAX_WRITE_BYTES = 2 * MAX_MESSAGE_BYTES
HEARTBEAT_TIMEOUT = 3.0
METHODS = frozenset({"heartbeat", "get_status", "connect_device", "disconnect_device",
                     "preview_plan", "get_templates", "start_stage", "get_progress",
                     "add_observation", "stop", "get_results"})


@dataclass
class _Client:
    socket: object
    client_id: str = field(default_factory=lambda: uuid4().hex)
    buffer: bytearray = field(default_factory=bytearray)
    last_seen: float = field(default_factory=time.monotonic)
    ids: deque = field(default_factory=lambda: deque(maxlen=128))
    authenticated: bool = False
    scheduled: bool = False


class AutomationBridge(QObject):
    """所有 dispatch 与 client_lost 均在创建桥的 Qt 线程执行。"""

    def __init__(self, service, project_directory: Path, parent=None):
        super().__init__(parent)
        self.service = service
        self.project_directory = Path(project_directory).resolve()
        self.runtime_directory = self.project_directory / ".runtime"
        self.endpoint_path = self.runtime_directory / "automation.json"
        self.server = QTcpServer(self)
        self.server.setMaxPendingConnections(MAX_CLIENTS)
        self.server.newConnection.connect(self._accept)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._expire_clients)
        self._clients: dict[object, _Client] = {}
        self._lock = None
        self._token = ""
        self.last_error = ""

    def start(self) -> bool:
        if self.server.isListening():
            return True
        self.last_error = ""
        try:
            self.runtime_directory.mkdir(parents=True, exist_ok=True)
            self._lock = QLockFile(str(self.runtime_directory / "automation.lock"))
            # 运行进程不能仅因文件年龄被当作陈旧实例。
            self._lock.setStaleLockTime(0)
            if not self._lock.tryLock(0):
                self.last_error = "本项目已有自动化工作台运行，或实例锁无法取得"
                self._lock = None
                return False
            if not self.server.listen(QHostAddress.SpecialAddress.LocalHost, 0):
                self.last_error = "无法启动本地自动化桥"
                self.close()
                return False
            self._token = secrets.token_urlsafe(32)
            endpoint = dict(schema_version=1, host="127.0.0.1", port=self.server.serverPort(),
                            token=self._token, pid=os.getpid(),
                            project_directory=str(self.project_directory))
            temporary = self.endpoint_path.with_name(f"automation.{uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as handle:
                    if os.name != "nt":
                        os.chmod(temporary, 0o600)
                    json.dump(endpoint, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.endpoint_path)
            finally:
                temporary.unlink(missing_ok=True)
            self.timer.start()
            return True
        except OSError:
            # 不把 endpoint 内容、token 或异常载荷写进日志。
            self.last_error = "自动化端点文件无法保存"
            self.close()
            return False

    def close(self) -> None:
        self.timer.stop()
        self.server.close()
        for client in tuple(self._clients.values()):
            self._drop(client)
        if self._lock is not None:
            try:
                if self.endpoint_path.exists():
                    if self.endpoint_path.stat().st_size <= MAX_MESSAGE_BYTES:
                        saved = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
                        if saved.get("token") == self._token:
                            self.endpoint_path.unlink(missing_ok=True)
            except (OSError, ValueError, AttributeError):
                pass
            self._lock.unlock()
            self._lock = None
        self._token = ""

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if len(self._clients) >= MAX_CLIENTS or not socket.peerAddress().isLoopback():
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(MAX_MESSAGE_BYTES + 1)
            client = _Client(socket)
            self._clients[socket] = client
            socket.readyRead.connect(lambda c=client: self._read(c))
            socket.disconnected.connect(lambda c=client: self._drop(c))
            socket.errorOccurred.connect(lambda _error, c=client: self._drop(c))
            if socket.bytesAvailable():
                self._read(client)

    def _drop(self, client: _Client) -> None:
        if self._clients.pop(client.socket, None) is None:
            return
        if client.authenticated:
            try:
                self.service.client_lost(client.client_id)
            except Exception:
                pass
        client.socket.abort()
        client.socket.deleteLater()

    def _expire_clients(self) -> None:
        now = time.monotonic()
        for client in tuple(self._clients.values()):
            if now - client.last_seen > HEARTBEAT_TIMEOUT:
                self._drop(client)

    def _read(self, client: _Client) -> None:
        client.scheduled = False
        if client.socket not in self._clients:
            return
        # 每轮最多处理 32 条，给 STATE、STOP 与界面事件留下运行机会。
        processed = 0
        while processed < 32:
            newline = client.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(client.buffer[:newline])
                del client.buffer[:newline + 1]
                if not raw or not self._request(client, raw):
                    self._drop(client)
                    return
                processed += 1
                continue
            available = client.socket.bytesAvailable()
            if not available:
                break
            room = MAX_MESSAGE_BYTES - len(client.buffer)
            if room <= 0:
                self._drop(client)
                return
            client.buffer.extend(bytes(client.socket.read(min(room, available))))
        if client.socket in self._clients and (client.socket.bytesAvailable() or b"\n" in client.buffer):
            client.scheduled = True
            QTimer.singleShot(0, lambda c=client: self._read(c))
        elif len(client.buffer) >= MAX_MESSAGE_BYTES:
            self._drop(client)

    def _request(self, client: _Client, raw: bytes) -> bool:
        try:
            message = json.loads(raw.decode("utf-8"), parse_constant=lambda _v: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            return False
        if not isinstance(message, dict):
            return False
        token = message.get("token")
        if not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(token, self._token):
            return False
        client.authenticated = True
        client.last_seen = time.monotonic()
        request_id = message.get("id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            return False
        if request_id in client.ids:
            return self._reply(client, request_id, {"error": "duplicate_request", "message": "请求不能自动重发"})
        client.ids.append(request_id)
        method, params = message.get("method"), message.get("params", {})
        if not isinstance(method, str) or method not in METHODS or not isinstance(params, dict):
            return self._reply(client, request_id, {"error": "invalid_request", "message": "方法或参数不受支持"})
        try:
            result = self.service.dispatch(method, params, client.client_id)
            if not isinstance(result, dict):
                raise TypeError("服务响应必须为对象")
        except Exception:
            result = {"error": "service_error", "message": "自动化服务处理失败"}
        return self._reply(client, request_id, result)

    def _reply(self, client: _Client, request_id: str, result: dict) -> bool:
        try:
            payload = (json.dumps({"id": request_id, "result": result}, ensure_ascii=False,
                                  separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            payload = (json.dumps({"id": request_id, "result": {"error": "serialization_error"}})
                       + "\n").encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            payload = (json.dumps({"id": request_id, "result": {"error": "response_too_large",
                       "message": "响应超过 64 KiB；请缩小查询范围"}}) + "\n").encode("utf-8")
        if client.socket.bytesToWrite() + len(payload) > MAX_WRITE_BYTES:
            return False
        return client.socket.write(payload) == len(payload)
