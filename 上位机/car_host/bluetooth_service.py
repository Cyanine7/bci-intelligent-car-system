"""Isolated Qt Bluetooth process. It understands bytes, never MCU commands.

Start with ``python -m car_host.bluetooth_service``. Standard output is reserved
for IPC; Qt diagnostics remain on standard error. No Bluetooth operation is
started until a valid scan/connect command is received.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from queue import Empty, Full, Queue
import os
import sys
import threading
import time

from PySide6.QtCore import QCoreApplication, QIODevice, QObject, QTimer, Slot

from .bluetooth_ipc import (
    IpcError, MAX_PAYLOAD_BYTES, MAX_PIPE_BYTES, MessageDecoder, decode_payload,
    encode_message, normalize_address, validate_channel,
)


class SendPump(QObject):
    """One in-flight write, tolerant of synchronous bytesWritten signals.

    The platform write itself can block. The parent process owns the hard
    deadline and can terminate this entire process if it does not respond.
    """

    def __init__(self, socket, completed, failed, parent=None):
        super().__init__(parent)
        self.socket = socket
        self.completed = completed
        self.failed = failed
        self._active = None
        self._scheduled = False
        self._writing = False
        socket.bytesWritten.connect(self._bytes_written)

    @property
    def request_id(self) -> str:
        return self._active["id"] if self._active else ""

    @property
    def active(self) -> bool:
        return self._active is not None

    def send(self, payload: bytes, request_id: str) -> bool:
        if self._active is not None or not payload or len(payload) > MAX_PAYLOAD_BYTES:
            return False
        # Install state before write: WinRT emits bytesWritten synchronously.
        self._active = {"payload": payload, "id": request_id, "accepted": 0, "written": 0, "failure": ""}
        self._schedule()
        return True

    def cancel(self) -> None:
        self._active = None

    def _schedule(self) -> None:
        if not self._scheduled:
            self._scheduled = True
            QTimer.singleShot(0, self._advance)

    def _bytes_written(self, count: int) -> None:
        active = self._active
        if active is None:
            return
        if count < 0:
            active["failure"] = "蓝牙发送失败：底层写入返回负数，结果未知"
        else:
            active["written"] += count
        # Never write recursively from bytesWritten, even if emitted in write.
        self._schedule()

    @Slot()
    def _advance(self) -> None:
        self._scheduled = False
        active = self._active
        if active is None or self._writing:
            return
        if active["failure"]:
            self._fail(active)
            return
        if active["written"] > active["accepted"]:
            active["failure"] = "蓝牙写入计数异常，结果未知"
            self._fail(active)
            return
        payload = active["payload"]
        if active["accepted"] < len(payload):
            remaining = payload[active["accepted"]:]
            self._writing = True
            try:
                count = self.socket.write(remaining)
            except Exception as exc:
                active["failure"] = f"蓝牙写入异常：{type(exc).__name__}: {exc}；结果未知"
                count = -1
            finally:
                self._writing = False
            if self._active is not active:
                return
            if not isinstance(count, int) or count < 0 or count > len(remaining):
                active["failure"] = active["failure"] or "蓝牙发送不完整或失败，结果未知"
            else:
                active["accepted"] += count
            # A zero write is retried on a later timer, avoiding a busy loop.
            if count == 0 and not active["failure"]:
                QTimer.singleShot(20, self._schedule)
                return
        if active["failure"] or active["written"] > active["accepted"]:
            active["failure"] = active["failure"] or "蓝牙写入计数异常，结果未知"
            self._fail(active)
        elif active["accepted"] == len(payload) and active["written"] == len(payload):
            self._active = None
            self.completed(payload, active["id"])
        elif active["accepted"] < len(payload):
            self._schedule()

    def _fail(self, active: dict) -> None:
        if self._active is active:
            self._active = None
            self.failed(active["failure"], active["id"])


class BluetoothService(QObject):
    MAX_DEVICES = 128
    MAX_COMMANDS = 128

    def __init__(self, emit_message, parent=None):
        super().__init__(parent)
        self.emit_message = emit_message
        self.connection_id = ""
        self._mode = ""
        self._closed = False
        self._socket = None
        self._agent = None
        self._pump = None
        self._devices = set()
        self._device_limit_reported = False
        self._commands = Queue(maxsize=self.MAX_COMMANDS)
        self._reader_failure = deque(maxlen=1)
        self._reader_eof = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self.process_commands)
        self._timer.start()

    def start_input_reader(self) -> None:
        # Windows stdin pipes cannot be watched using a Unix fd notifier.
        # The daemon only decodes/queues; it never calls Qt or Bluetooth.
        threading.Thread(target=self._read_input, name="bluetooth-ipc-input", daemon=True).start()

    def _read_input(self) -> None:
        decoder = MessageDecoder(commands=True)
        try:
            # BufferedReader.read1 holds a Python buffer lock while waiting on
            # Windows pipes. A daemon holding that lock can abort Python during
            # interpreter shutdown. OS reads have no buffered-object lock.
            input_fd = sys.stdin.fileno()
            while True:
                data = os.read(input_fd, 4096)
                if not data:
                    decoder.finish()
                    self._reader_eof.set()
                    return
                for message in decoder.feed(data):
                    try:
                        self._commands.put_nowait(message)
                    except Full as exc:
                        raise IpcError("蓝牙后台命令缓存溢出") from exc
        except Exception as exc:
            self._reader_failure.append(f"后台输入失败：{type(exc).__name__}: {exc}")

    @Slot()
    def process_commands(self) -> None:
        if self._closed:
            return
        if self._reader_failure:
            self._fatal(self._reader_failure.pop())
            return
        for _ in range(16):
            try:
                message = self._commands.get_nowait()
            except Empty:
                break
            try:
                self.handle_command(message)
            except Exception as exc:
                self._fatal(f"蓝牙操作失败：{type(exc).__name__}: {exc}")
            if self._closed:
                return
        if self._reader_eof.is_set() and self._commands.empty():
            self._stop("父进程通信已关闭")

    def handle_command(self, message: dict) -> None:
        if self._closed:
            return
        connection_id = message["connection_id"]
        kind = message["kind"]
        if self.connection_id and connection_id != self.connection_id:
            return
        if not self.connection_id:
            self.connection_id = connection_id
        if kind == "stop":
            self._stop("已停止")
        elif kind in {"scan", "connect"}:
            if self._mode:
                raise IpcError("每个蓝牙后台只允许一个会话")
            self._mode = kind
            if kind == "scan":
                self._scan()
            else:
                self._connect(normalize_address(message.get("address", "")), validate_channel(message.get("channel")))
        elif kind == "send":
            payload = decode_payload(message)
            request_id = message.get("request_id", "")
            if not request_id or self._mode != "connect" or self._pump is None:
                self._fatal("蓝牙尚未连接，不能发送", request_id)
            elif not self._pump.send(payload, request_id):
                self._fatal("蓝牙发送缓存忙或载荷无效", request_id)

    def _scan(self) -> None:
        from PySide6.QtBluetooth import QBluetoothDeviceDiscoveryAgent

        self._agent = QBluetoothDeviceDiscoveryAgent(self)
        classic = QBluetoothDeviceDiscoveryAgent.DiscoveryMethod.ClassicMethod
        if not QBluetoothDeviceDiscoveryAgent.supportedDiscoveryMethods() & classic:
            self._fatal("当前平台不支持经典蓝牙扫描")
            return
        self._agent.deviceDiscovered.connect(self._device_found)
        self._agent.finished.connect(lambda: self._stop("扫描完成"))
        self._agent.canceled.connect(lambda: self._stop("扫描已取消"))
        self._agent.errorOccurred.connect(lambda _: self._fatal("蓝牙扫描失败：" + self._agent.errorString()))
        self._event("status", message="正在扫描经典蓝牙；Windows 可能返回缓存设备")
        self._agent.start(classic)

    def _device_found(self, info) -> None:
        from PySide6.QtBluetooth import QBluetoothDeviceInfo

        if self._closed or not info.coreConfigurations() & QBluetoothDeviceInfo.CoreConfiguration.BaseRateCoreConfiguration:
            return
        try:
            address = normalize_address(info.address().toString())
        except ValueError:
            return
        if address in self._devices:
            return
        if len(self._devices) >= self.MAX_DEVICES:
            if not self._device_limit_reported:
                self._device_limit_reported = True
                self._event("status", message="扫描设备已达 128 个，后续设备被忽略")
            return
        self._devices.add(address)
        self._event("device", payload=address.encode("ascii"), message=info.name()[:256])

    def _connect(self, address: str, channel: int) -> None:
        from PySide6.QtBluetooth import QBluetoothAddress, QBluetoothServiceInfo, QBluetoothSocket

        self._socket = QBluetoothSocket(QBluetoothServiceInfo.Protocol.RfcommProtocol, self)
        self._pump = SendPump(self._socket, self._sent, self._fatal, self)
        self._socket.connected.connect(self._connected)
        self._socket.readyRead.connect(self._receive)
        self._socket.disconnected.connect(self._disconnected)
        self._socket.errorOccurred.connect(self._socket_error)
        self._socket.connectToService(QBluetoothAddress(address), channel, QIODevice.OpenModeFlag.ReadWrite)

    @Slot()
    def _connected(self) -> None:
        if self._closed:
            return
        self._event("opened", message="蓝牙 SPP 已连接；有效遥测需另行确认")

    @Slot()
    def _receive(self) -> None:
        if self._closed:
            return
        if self._socket.bytesAvailable() > MAX_PIPE_BYTES:
            self._fatal("蓝牙接收缓存溢出，已关闭连接")
            return
        for _ in range(16):
            if not self._socket.bytesAvailable():
                break
            data = bytes(self._socket.read(MAX_PAYLOAD_BYTES))
            if not data:
                break
            self._event("rx", payload=data)
        if self._socket.bytesAvailable():
            QTimer.singleShot(0, self._receive)

    def _sent(self, payload: bytes, request_id: str) -> None:
        if not self._closed:
            self._event("tx", payload=payload, request_id=request_id)

    @Slot()
    def _disconnected(self) -> None:
        if not self._closed:
            self._fatal("蓝牙设备已断开；在途发送结果未知", self._pump.request_id if self._pump else "")

    def _socket_error(self, error) -> None:
        if not self._closed:
            self._fatal("蓝牙连接或通信失败：" + self._socket.errorString() + "；在途发送结果未知", self._pump.request_id if self._pump else "")

    def _fatal(self, message: str, request_id: str = "") -> None:
        if self._closed:
            return
        self._event("error", message=message, request_id=request_id)
        self._stop("通信失败", exit_code=2)

    def _stop(self, message: str, *, exit_code: int = 0) -> None:
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        if self._pump is not None:
            self._pump.cancel()
        self._event("closed", message=message)
        # Abort/quit outside signal callbacks, including reentrant write errors.
        QTimer.singleShot(0, lambda: self._cleanup(exit_code))

    def _cleanup(self, exit_code: int) -> None:
        if self._agent is not None and self._agent.isActive():
            self._agent.stop()
        if self._socket is not None:
            self._socket.abort()
        app = QCoreApplication.instance()
        if app is not None:
            app.exit(exit_code)

    def _event(self, kind: str, *, payload: bytes = b"", message: str = "", request_id: str = "") -> None:
        self.emit_message(encode_message(kind, self.connection_id, payload=payload,
            message=message[:2048], request_id=request_id, monotonic=time.monotonic(),
            utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")))


def main() -> int:
    app = QCoreApplication(sys.argv)

    def emit_message(line: bytes) -> None:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

    service = BluetoothService(emit_message)
    service.start_input_reader()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
