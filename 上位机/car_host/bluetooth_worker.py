"""Nonblocking parent-side Bluetooth supervision; no Bluetooth APIs run here."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import uuid

from PySide6.QtCore import QObject, QProcess, QTimer

from .bluetooth_ipc import (
    IpcError, MAX_PAYLOAD_BYTES, MAX_PIPE_BYTES, MessageDecoder, decode_payload,
    encode_message, normalize_address, validate_channel,
)
from .transport import ConnectionConfig, IoEvent


class _ProcessSession(QObject):
    SEND_QUEUE_CAPACITY = 128
    EVENT_QUEUE_CAPACITY = 1024
    CONTROL_CAPACITY = 256
    STATUS_CAPACITY = 32
    MAX_PAYLOAD_BYTES = MAX_PAYLOAD_BYTES
    MAX_DEVICES = 128
    CONNECT_TIMEOUT = 10.0
    SCAN_TIMEOUT = 10.0
    SEND_TIMEOUT = 3.0
    STOP_TIMEOUT = 2.0
    source = "BLUETOOTH_SPP"

    def __init__(self, connection_id: str = "", *, process_command=None):
        super().__init__()
        self.connection_id = connection_id or uuid.uuid4().hex
        # Validate identifiers before creating a process.
        encode_message("stop", self.connection_id)
        self._command = tuple(process_command) if process_command else (
            sys.executable, "-m", "car_host.bluetooth_service",
        )
        if not self._command or not all(isinstance(part, str) and part for part in self._command):
            raise ValueError("蓝牙后台命令无效")
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
        self._process.started.connect(self._on_started)
        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_process_error)
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)
        self._decoder = MessageDecoder(commands=False)
        self._data = deque()
        self._control = deque()
        self._sequence = 0
        self._overflow = 0
        self._started = False
        self._finished = False
        self._stopping = False
        self._opened = False
        self._kill_requested = False
        self._child_closed = False
        self._had_error = False
        self._closed_emitted = False
        self._closed_message = ""
        self._capacity_failed = False
        self._deadline = 0.0
        self._stop_deadline = 0.0
        self._queue = deque()
        self._inflight = None
        self._known_devices = set()
        self._stderr_tail = bytearray()
        self._stderr_reported = False

    def _initial_message(self) -> bytes:
        raise NotImplementedError

    def start(self) -> None:
        """A session is single-use; create another object for a new connection."""
        if self._started or self._finished:
            return
        self._started = True
        self._deadline = time.monotonic() + (self.SCAN_TIMEOUT if self._is_scan else self.CONNECT_TIMEOUT)
        self._timer.start()
        self._process.start(self._command[0], list(self._command[1:]))

    @property
    def _is_scan(self) -> bool:
        return False

    def request_stop(self) -> None:
        if self._finished or self._stopping:
            return
        self._stopping = True
        self._closed_message = "用户取消扫描" if self._is_scan else "用户断开连接"
        self._stop_deadline = time.monotonic() + self.STOP_TIMEOUT
        pending = len(self._queue)
        self._queue.clear()
        if pending:
            self._emit("status", message=f"已取消 {pending} 条排队发送")
        if self._inflight is not None:
            self._emit("error", message="连接关闭：在途发送结果未知，不会自动重发", request_id=self._inflight[0])
            self._inflight = None
        if not self._started:
            self._finish()
        elif self._process.state() == QProcess.ProcessState.Running:
            self._write_ipc(encode_message("stop", self.connection_id))
            self._process.closeWriteChannel()

    def drain_events(self, max_count: int = 256) -> list[IoEvent]:
        result = []
        for _ in range(max(0, min(max_count, self.EVENT_QUEUE_CAPACITY))):
            if not self._data and not self._control:
                break
            if not self._data or (self._control and self._control[0][0] < self._data[0][0]):
                result.append(self._control.popleft()[1])
            else:
                result.append(self._data.popleft()[1])
        return result

    def take_overflow_count(self) -> int:
        count, self._overflow = self._overflow, 0
        return count

    def isFinished(self) -> bool:
        return self._finished

    def isRunning(self) -> bool:
        return self._started and not self._finished

    def wait(self, milliseconds: int = 0) -> bool:
        """Compatibility query only; shutdown is asynchronous, never a pipe wait."""
        return self._finished

    def _on_started(self) -> None:
        if self._finished:
            self._process.kill()
            return
        self._write_ipc(encode_message("stop", self.connection_id) if self._stopping else self._initial_message())
        if self._stopping:
            self._process.closeWriteChannel()

    def _write_ipc(self, line: bytes) -> bool:
        if self._kill_requested or self._process.state() != QProcess.ProcessState.Running:
            return False
        if self._process.bytesToWrite() + len(line) > MAX_PIPE_BYTES:
            self._fail_and_kill("蓝牙后台待写管道溢出；发送结果未知")
            return False
        written = self._process.write(line)
        if written != len(line):
            self._fail_and_kill("蓝牙后台管道写入失败；发送结果未知")
            return False
        return True

    def _read_channel(self, channel) -> bytes:
        self._process.setReadChannel(channel)
        available = self._process.bytesAvailable()
        if available > MAX_PIPE_BYTES:
            self._overflow += 1
            self._fail_and_kill("蓝牙后台输出管道溢出，已结束连接")
            # Draining bounded pieces also prevents QProcess retaining output.
            return bytes(self._process.read(MAX_PIPE_BYTES))
        return bytes(self._process.read(min(available, MAX_PIPE_BYTES)))

    def _read_stdout(self) -> None:
        data = self._read_channel(QProcess.ProcessChannel.StandardOutput)
        if self._finished or self._kill_requested:
            return
        try:
            for message in self._decoder.feed(data):
                if self._finished or self._kill_requested:
                    break
                self._handle_message(message)
        except IpcError as exc:
            self._fail_and_kill(f"蓝牙后台协议错误：{exc}")

    def _read_stderr(self) -> None:
        data = self._read_channel(QProcess.ProcessChannel.StandardError)
        self._stderr_tail.extend(data[-2048:])
        if len(self._stderr_tail) > 2048:
            del self._stderr_tail[:-2048]
        self._process.setReadChannel(QProcess.ProcessChannel.StandardOutput)
        if data and not self._stderr_reported and not self._finished:
            self._stderr_reported = True
            self._emit("status", message="蓝牙后台诊断：" + data[-1024:].decode("utf-8", "replace"))

    def _handle_message(self, message: dict) -> None:
        if message["connection_id"] != self.connection_id:
            return
        kind = message["kind"]
        if self._stopping and kind not in {"error", "closed", "status"} and not (self._is_scan and kind == "device"):
            return
        if self._child_closed and kind != "closed":
            return
        payload = decode_payload(message)
        request_id = message.get("request_id", "")
        text = message.get("message", "")
        if kind == "opened":
            if not self._is_scan and not self._opened:
                self._opened = True
                self._emit("opened", message=text or "蓝牙 SPP 已连接")
        elif kind == "rx":
            if self._opened and payload:
                self._emit("rx", payload=payload, monotonic=message.get("monotonic"), utc=message.get("utc"))
        elif kind == "tx":
            if self._inflight is not None and request_id == self._inflight[0]:
                if payload != self._inflight[1]:
                    self._fail_and_kill("蓝牙后台发送确认载荷不匹配")
                    return
                self._inflight = None
                self._emit("tx", payload=payload, request_id=request_id,
                           monotonic=message.get("monotonic"), utc=message.get("utc"))
        elif kind == "device":
            if not self._is_scan:
                self._fail_and_kill("连接后台返回了扫描事件")
                return
            try:
                address = normalize_address(payload.decode("ascii"))
            except (ValueError, UnicodeError) as exc:
                self._fail_and_kill(f"蓝牙后台设备地址无效：{exc}")
                return
            if address in self._known_devices:
                return
            if len(self._known_devices) >= self.MAX_DEVICES:
                self._overflow += 1
                return
            self._known_devices.add(address)
            self._emit("device", message=text, payload=address.encode("ascii"))
        elif kind == "error":
            self._had_error = True
            self._closed_message = text or "蓝牙后台错误"
            if self._inflight is not None and not request_id:
                request_id = self._inflight[0]
                text = (text or "蓝牙后台错误") + "；在途发送结果未知"
            self._emit("error", message=text or "蓝牙后台错误", request_id=request_id)
            self._stopping = True
            self._stop_deadline = time.monotonic() + self.STOP_TIMEOUT
            self._queue.clear()
            self._inflight = None
        elif kind == "closed":
            self._child_closed = True
            if text and not self._had_error:
                self._closed_message = text
            self._stopping = True
            self._stop_deadline = time.monotonic() + self.STOP_TIMEOUT
            self._queue.clear()
            self._process.closeWriteChannel()
        elif kind == "status":
            self._emit("status", message=text)

    def _tick(self) -> None:
        if self._finished:
            return
        now = time.monotonic()
        if self._kill_requested:
            return
        if self._stopping:
            if now >= self._stop_deadline:
                self._fail_and_kill("蓝牙后台停止超时，已终止进程")
            return
        if self._is_scan:
            if now >= self._deadline:
                self._emit("status", message="扫描已达到 10 秒，正在结束")
                self.request_stop()
            return
        if not self._opened:
            if now >= self._deadline:
                self._fail_and_kill("蓝牙连接超时，请核对 Windows 配对、MAC 和 RFCOMM 通道")
            return
        if self._inflight is not None:
            if now >= self._inflight[2]:
                self._fail_and_kill("蓝牙发送超时：结果未知，不会自动重发", request_id=self._inflight[0])
            return
        if self._queue:
            request_id, payload = self._queue.popleft()
            self._inflight = (request_id, payload, now + self.SEND_TIMEOUT)
            self._write_ipc(encode_message("send", self.connection_id, request_id=request_id, payload=payload))

    def _fail_and_kill(self, message: str, *, request_id: str = "") -> None:
        if self._finished or self._kill_requested:
            return
        if not request_id and self._inflight is not None:
            request_id = self._inflight[0]
        self._had_error = True
        self._closed_message = message
        self._stopping = True
        self._kill_requested = True
        self._emit("error", message=message, request_id=request_id)
        self._queue.clear()
        self._inflight = None
        self._process.kill()
        if self._process.state() == QProcess.ProcessState.NotRunning:
            self._finish()

    def _on_process_error(self, error) -> None:
        if self._finished or self._kill_requested:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self._had_error = True
            self._closed_message = "无法启动蓝牙后台：" + self._process.errorString()
            self._emit("error", message=self._closed_message)
            self._finish()
        elif error != QProcess.ProcessError.Crashed:
            self._fail_and_kill("蓝牙后台进程错误：" + self._process.errorString())

    def _on_finished(self, exit_code, exit_status) -> None:
        if self._finished:
            return
        self._read_stdout()
        self._read_stderr()
        if not self._kill_requested:
            try:
                self._decoder.finish()
            except IpcError as exc:
                self._had_error = True
                self._emit("error", message=f"蓝牙后台协议错误：{exc}")
            if self._inflight is not None:
                self._had_error = True
                self._emit("error", message="蓝牙后台退出：在途发送结果未知，不会自动重发", request_id=self._inflight[0])
            if (not self._child_closed or exit_code != 0) and not self._had_error:
                self._had_error = True
                detail = self._stderr_tail.decode("utf-8", "replace").strip()
                self._emit("error", message=f"蓝牙后台意外退出（{exit_code}）" + (f"：{detail}" if detail else ""))
        self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._opened = False
        self._queue.clear()
        self._inflight = None
        self._timer.stop()
        self._decoder.clear()
        self._discard_output()
        if not self._closed_emitted:
            self._closed_emitted = True
            self._emit("closed", message=self._closed_message or ("蓝牙扫描已结束" if self._is_scan else "蓝牙连接已关闭"))

    def _discard_output(self) -> None:
        # The process is already stopped. Close the channels to prevent further
        # accumulation, then discard cached output using bounded allocations.
        if not self._process.isOpen():
            return
        for channel in (QProcess.ProcessChannel.StandardOutput, QProcess.ProcessChannel.StandardError):
            self._process.closeReadChannel(channel)
            self._process.setReadChannel(channel)
            while self._process.bytesAvailable():
                self._process.read(min(self._process.bytesAvailable(), MAX_PIPE_BYTES))
        self._process.setReadChannel(QProcess.ProcessChannel.StandardOutput)

    def _emit(self, kind: str, *, payload: bytes = b"", message: str = "", request_id: str = "",
              monotonic: float | None = None, utc: str | None = None) -> None:
        self._sequence += 1
        item = (self._sequence, IoEvent(kind=kind, payload=payload, message=message[:2048],
            monotonic=time.monotonic() if monotonic is None else monotonic,
            utc=utc if utc is not None else datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            connection_id=self.connection_id, request_id=request_id))
        if kind in {"opened", "tx", "error", "closed", "status"}:
            if kind == "status":
                statuses = [old for old in self._control if old[1].kind == "status"]
                if len(statuses) >= self.STATUS_CAPACITY or len(self._control) >= self.CONTROL_CAPACITY - 3:
                    if statuses:
                        self._control.remove(statuses[0])
                    else:
                        self._overflow += 1
                        return
                    self._overflow += 1
            else:
                # Reclaim ordinary status lines before consuming the three
                # slots reserved for the last result, failure and closure.
                while len(self._control) >= self.CONTROL_CAPACITY - 3:
                    status = next((old for old in self._control if old[1].kind == "status"), None)
                    if status is None:
                        break
                    self._control.remove(status)
                    self._overflow += 1
            self._control.append(item)
            if (kind != "closed" and len(self._control) >= self.CONTROL_CAPACITY - 2
                    and not self._capacity_failed and not self._finished and not self._kill_requested):
                self._capacity_failed = True
                self._overflow += 1
                self._fail_and_kill("蓝牙关键事件缓存已满，已终止连接；后续发送不会重发")
        else:
            if len(self._data) >= self.EVENT_QUEUE_CAPACITY - self.CONTROL_CAPACITY:
                self._data.popleft()
                self._overflow += 1
            self._data.append(item)


class BluetoothWorker(_ProcessSession):
    def __init__(self, config: ConnectionConfig, connection_id: str = "", *, process_command=None):
        self.config = config
        self.address = normalize_address(config.bluetooth_address)
        self.channel = validate_channel(config.rfcomm_channel)
        super().__init__(connection_id, process_command=process_command)

    def _initial_message(self) -> bytes:
        return encode_message("connect", self.connection_id, address=self.address, channel=self.channel)

    def request_send(self, payload: bytes, request_id: str = "") -> bool:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            return False
        if not payload or len(payload) > self.MAX_PAYLOAD_BYTES:
            return False
        if not self._opened or self._stopping or self._finished or len(self._queue) >= self.SEND_QUEUE_CAPACITY:
            return False
        request_id = request_id or uuid.uuid4().hex
        try:
            encode_message("send", self.connection_id, request_id=request_id, payload=payload)
        except IpcError:
            return False
        if (self._inflight is not None and self._inflight[0] == request_id) or any(item[0] == request_id for item in self._queue):
            return False
        self._queue.append((request_id, bytes(payload)))
        return True


class BluetoothScanner(_ProcessSession):
    @property
    def _is_scan(self) -> bool:
        return True

    def _initial_message(self) -> bytes:
        return encode_message("scan", self.connection_id)

    def stop(self) -> None:
        self.request_stop()
