"""应用协调：UI 不直接访问串口，也不处理协议字节。"""

from __future__ import annotations

import re
import json
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from .history import TelemetryHistory
from .models import TelemetrySample
from .device import DeviceCapabilities, DeviceResult, MotionService, ParameterService, capabilities_for_protocol, create_protocol_adapter
from .project_protocol import (PROJECT_V1, ProjectV1Adapter, CAPS, ACK, PARAMS,
                               COMMAND_TYPES, PARAMETER_KEYS, ACK_MESSAGES)
from .recording import LogEntry, SessionRecorder
from .transport import ConnectionConfig, source_for_kind
from .worker import WorkerInterface, create_worker


def encode_manual_payload(text: str, as_hex: bool, ending: str = "") -> bytes:
    if as_hex:
        cleaned = re.sub(r"\s+", "", text)
        if not cleaned or len(cleaned) % 2 or re.fullmatch(r"[0-9a-fA-F]+", cleaned) is None:
            raise ValueError("HEX 请输入完整字节，例如 7B 43 31 3A 32 3A 39 30 7D 24")
        payload = bytes.fromhex(cleaned)
    else:
        if not text:
            raise ValueError("发送内容不能为空")
        payload = text.encode("utf-8") + ending.encode("ascii")
    if len(payload) > 4096:
        raise ValueError("单次发送最多 4096 字节")
    return payload


class HostController(QObject):
    changed = Signal()
    log_added = Signal(object)
    logs_cleared = Signal()
    sample_received = Signal(object)
    request_updated = Signal(object)

    def __init__(self, data_directory: Path, parent=None, worker_factory=create_worker,
                 scanner_factory=None):
        super().__init__(parent)
        self.data_directory = Path(data_directory)
        self.worker_factory = worker_factory
        self.scanner_factory = scanner_factory
        self.worker: WorkerInterface | None = None
        self.scanner = None
        self.bluetooth_devices: list[tuple[str, str]] = []
        self.bluetooth_scan_status = "尚未扫描；首次连接请先在Windows设置配对"
        self._scan_devices: dict[str, str] = {}
        self._scan_error = ""
        self._pending_connection: ConnectionConfig | None = None
        self.adapter = create_protocol_adapter("LEGACY_APP", "SERIAL")
        self.capabilities = DeviceCapabilities()
        self.parameters = ParameterService(self)
        self.motion = MotionService(self)
        self.protocol_status = "未连接"
        self.project_limits = {}
        self.actual_parameters = None
        self.parameter_status = "尚未读取实际 RAM 参数"
        self.command_results: dict[str, DeviceResult] = {}
        self.command_metadata: dict[str, dict] = {}
        self.control_owner: str | None = None
        self.automation_service = None
        self.caps_received_utc = None
        self.parameters_received_utc = None
        self._project_pending = {}
        self._motion_latched_seq = 0
        self._state_required_seq = 0
        self._device_dropped = (0, 0)
        self.connection_id = ""
        self.config: ConnectionConfig | None = None
        self._closing_connection = False
        self._shutting_down = False
        self.history = TelemetryHistory()
        self.logs: deque[LogEntry] = deque(maxlen=1000)
        self.recorder = SessionRecorder()
        self.latest: TelemetrySample | None = None
        self.frame_times: deque[float] = deque(maxlen=200)
        self.connected = False
        self.connection_status = "未连接"
        self.source = "SERIAL"
        self.bytes_received = self.bytes_sent = self.frames_received = 0
        self.recording_status = "未录制"
        self.recording_incomplete = False
        self._recording_expected = False
        self._recording_stopping = False
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def add_log(self, message: str, level: str = "INFO", *, payload: bytes | None = None,
                direction: str | None = None, utc: str | None = None, monotonic: float | None = None,
                persist: bool = True, connection_id: str | None = None, request_id: str = "") -> None:
        entry = LogEntry(utc or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                         time.monotonic() if monotonic is None else monotonic,
                         self.source, level, message, payload, direction,
                         self.connection_id if connection_id is None else connection_id, request_id)
        self.logs.append(entry)
        if persist:
            self.recorder.record_log(entry)
        self.log_added.emit(entry)

    def connect_device(self, config: ConnectionConfig) -> bool:
        if self.control_owner:
            self.add_log("实验阶段占用控制权；请先停止阶段", "WARNING")
            return False
        if self._shutting_down:
            return False
        if self.worker is not None:
            self.add_log("请等待当前连接完全关闭", "WARNING")
            return False
        if config.kind == "serial" and not config.port.strip():
            self.add_log("请选择或填写串口名称", "ERROR")
            return False
        try:
            source = source_for_kind(config.kind)
            adapter = create_protocol_adapter(config.protocol, source)
            capabilities = capabilities_for_protocol(config.protocol)
            if config.kind == "simulation" and config.protocol == PROJECT_V1:
                raise ValueError("模拟设备仅实现 LEGACY_APP；PROJECT_V1 请连接新固件或使用协议测试夹具")
            if config.kind == "bluetooth_spp":
                address = config.bluetooth_address.strip().replace("-", ":").upper()
                if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address) or address == "00:00:00:00:00:00":
                    raise ValueError("请填写有效蓝牙MAC，例如 2A:A2:19:07:1B:8B")
                if isinstance(config.rfcomm_channel, bool) or not isinstance(config.rfcomm_channel, int) or not 1 <= config.rfcomm_channel <= 30:
                    raise ValueError("RFCOMM通道必须是1–30的整数")
                config = replace(config, bluetooth_address=address)
        except (ValueError, TypeError) as exc:
            self.add_log(str(exc), "ERROR")
            return False
        if self.scanner is not None:
            self._pending_connection = config
            self.cancel_bluetooth_scan()
            self.add_log("正在结束扫描，完成后连接所选设备")
            return True
        try:
            worker = self.worker_factory(config)
        except Exception as exc:
            self.add_log(f"通信组件创建失败：{exc}", "ERROR")
            return False
        if getattr(worker, "source", source) == "SIMULATION_PROJECT_V1":
            source = "SIMULATION_PROJECT_V1"
            adapter = create_protocol_adapter(config.protocol, source)
        self.source = source
        self.adapter = adapter
        self.capabilities = capabilities
        self.protocol_status = "等待连接及 CAPS 握手" if config.protocol == PROJECT_V1 else "LEGACY_APP"
        self.project_limits = {}
        self.actual_parameters = None
        self.parameter_status = "尚未读取实际 RAM 参数"
        self.command_results.clear()
        self.command_metadata.clear()
        self.caps_received_utc = self.parameters_received_utc = None
        self._project_pending.clear()
        self._motion_latched_seq = self._state_required_seq = 0
        self._device_dropped = (0, 0)
        self.config = config
        self.connection_id = getattr(worker, "connection_id", "") or uuid4().hex
        self._closing_connection = False
        self.latest = None
        self.history.clear()
        self.frame_times.clear()
        self.bytes_received = self.bytes_sent = self.frames_received = 0
        self.connected = False
        self.connection_status = "正在连接"
        self.worker = worker
        self._log_connection_metadata()
        try:
            self.worker.start()
        except Exception as exc:
            self.worker = None
            self.capabilities = DeviceCapabilities()
            self.connection_status = "连接失败"
            self.add_log(f"启动连接失败：{exc}", "ERROR")
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    def disconnect_device(self) -> None:
        if self.automation_service is not None and self.automation_service.active:
            self.automation_service.stop("连接被主动断开")
        self._pending_connection = None
        if self.worker is not None:
            self.connection_status = "正在断开"
            self.connected = False
            self._closing_connection = True
            self.capabilities = DeviceCapabilities()
            self._expire_project_requests(time.monotonic(), disconnected=True)
            self.worker.request_stop()
            self.changed.emit()

    def send_manual(self, text: str, as_hex: bool, ending: str = "") -> bool:
        try:
            payload = encode_manual_payload(text, as_hex, ending)
        except ValueError as exc:
            self.add_log(str(exc), "ERROR")
            return False
        if not self.connected or self.worker is None:
            self.add_log("未连接，无法发送", "ERROR")
            return False
        if not self.capabilities.raw_send:
            self.add_log(self.capabilities.reason_for("raw_send"), "ERROR")
            return False
        request_id = uuid4().hex
        if not self.worker.request_send(payload, request_id=request_id):
            self.add_log("发送队列已满或连接正在关闭，内容未发送", "ERROR")
            return False
        self.add_log("发送请求已排队，尚未确认写出", payload=payload,
                     direction="QUEUED", request_id=request_id)
        return True

    def send_command(self, command: str, values=None, *, owner=None) -> DeviceResult:
        """Semantic commands are encoded by a selected protocol, not by UI/link code."""
        if self.control_owner and owner != self.control_owner:
            if command == "stop" and self.automation_service is not None:
                return self.automation_service.stop("人工 STOP")
            return DeviceResult(False, "busy", "自动化阶段正在运行；请先停止阶段后再手动操作")
        if isinstance(self.adapter, ProjectV1Adapter):
            return self._send_project_command(command, values)
        encoder = getattr(self.adapter, "encode_command", None)
        if encoder is None:
            return DeviceResult(False, "unsupported", "当前协议未实现语义命令编码；不会发送猜测的字节")
        default_requirements = {"read_parameters": "parameter_read", "apply_parameters": "parameter_write"}
        requirements = getattr(self.adapter, "command_capabilities", {})
        requirement = requirements.get(command, default_requirements.get(command))
        if not requirement or not self.capabilities.supports(requirement):
            return DeviceResult(False, "unsupported", self.capabilities.reason_for(requirement or command))
        if not self.connected or self.worker is None or self._closing_connection:
            return DeviceResult(False, "disconnected", "设备尚未连接")
        try:
            payload = encoder(command, values)
            if not isinstance(payload, bytes) or not payload or len(payload) > 4096:
                raise ValueError("协议编码必须返回1–4096字节")
        except (ValueError, TypeError, NotImplementedError) as exc:
            return DeviceResult(False, "rejected", str(exc))
        request_id = uuid4().hex
        if not self.worker.request_send(payload, request_id=request_id):
            return DeviceResult(False, "rejected", "发送队列已满或连接正在关闭")
        self.add_log(f"语义命令已排队：{command}，尚未确认MCU接受", payload=payload,
                     direction="QUEUED", request_id=request_id)
        return DeviceResult(True, "queued", "请求已排队，未确认设备接受或生效", request_id)

    @property
    def project_state_fresh(self):
        sample = self.latest
        return bool(self.connected and isinstance(self.adapter, ProjectV1Adapter) and sample
                    and sample.device_session == self.adapter.session
                    and 0 <= time.monotonic() - sample.received_monotonic < .5
                    and sample.device_last_seq >= self._state_required_seq)

    @property
    def project_control_pending(self):
        return any(p["command"] in ("arm_pwm", "arm_speed", "set_pwm", "set_speed", "stop")
                   for p in self._project_pending.values())

    @property
    def project_motion_active(self):
        return bool(self._motion_latched_seq)

    @property
    def project_request_pending(self):
        return bool(self._project_pending)

    def _remember_result(self, result):
        self.command_results[result.request_id] = result
        while len(self.command_results) > 128:
            del self.command_results[next(iter(self.command_results))]
        self.request_updated.emit(result)

    def _queue_project(self, command, payload):
        request_id = uuid4().hex
        if not self.worker.request_send(payload, request_id=request_id):
            return DeviceResult(False, "rejected", "发送队列已满或连接正在关闭")
        seq = self.adapter.seq
        self.command_metadata[request_id] = dict(command=command, seq=seq,
                                                session=self.adapter.session)
        while len(self.command_metadata) > 128:
            del self.command_metadata[next(iter(self.command_metadata))]
        self._project_pending[seq] = {"command": command, "request_id": request_id,
                                      "due": time.monotonic() + 2.0,
                                      "ack": False, "reply": False}
        result = DeviceResult(True, "queued", "已排队；等待 MCU ACK，不自动重发", request_id)
        self._remember_result(result)
        self.add_log(f"PROJECT_V1 {command} 已排队，session={self.adapter.session:08X} seq={seq}",
                     payload=payload, direction="QUEUED", request_id=request_id)
        if command in ("arm_pwm", "arm_speed", "set_pwm", "set_speed"):
            self._state_required_seq = seq
        if command in ("set_pwm", "set_speed"):
            self._motion_latched_seq = seq
        if command in ("read_parameters", "apply_parameters"):
            self.parameter_status = "等待实际 RAM 回读与 ACK" if command == "read_parameters" else "草稿已排队，等待 ACK 与实际 RAM 回读"
        return result

    def _send_project_command(self, command, values=None):
        if not self.connected or self.worker is None or self._closing_connection:
            return DeviceResult(False, "disconnected", "设备尚未连接")
        if command not in self.adapter.command_capabilities:
            return DeviceResult(False, "unsupported", "未定义的 PROJECT_V1 功能")
        requirement = self.adapter.command_capabilities[command]
        # A valid STOP is explicitly allowed even while CAPS is pending.
        if command != "stop" and not self.capabilities.supports(requirement):
            return DeviceResult(False, "unsupported", self.capabilities.reason_for(requirement))
        if command == "stop":
            existing = next((p for p in self._project_pending.values() if p["command"] == "stop"), None)
            if existing is not None:
                return DeviceResult(True, "queued", "STOP 已排队，等待 MCU ACK 与 STATE；不会重复发送", existing["request_id"])
            if len(self._project_pending) >= 32:
                # Keep storage bounded without allowing parameter traffic to
                # consume the user's STOP slot. Retiring a request is not a
                # cancellation of bytes already queued in the transport.
                seq = next((seq for seq, p in self._project_pending.items()
                            if p["command"] in ("read_parameters", "apply_parameters")),
                           next(iter(self._project_pending)))
                retired = self._project_pending.pop(seq)
                result = DeviceResult(False, "unknown", "为 STOP 释放请求槽；此前已排队请求结果未知，不重发", retired["request_id"])
                self._remember_result(result)
                self.add_log(result.message, "WARNING", request_id=result.request_id)
                if retired["command"] in ("read_parameters", "apply_parameters"):
                    self.parameter_status = "参数请求结果未知；STOP 优先保留请求槽"
        elif len(self._project_pending) >= 32:
            return DeviceResult(False, "rejected", "语义请求已达 32 项上限，请等待结果")
        values = dict(values or {})
        if command in ("arm_pwm", "arm_speed", "set_pwm", "set_speed", "apply_parameters"):
            if not self.project_state_fresh:
                return DeviceResult(False, "rejected", "需要 500 ms 内且已覆盖前次控制指令的有效 STATE")
            sample = self.latest
            pending_motion = any(p["command"] in ("arm_pwm", "arm_speed", "set_pwm", "set_speed", "stop") for p in self._project_pending.values())
            if pending_motion:
                return DeviceResult(False, "rejected", "前次控制请求尚未确认，请等待 ACK 与 STATE")
            if command in ("arm_pwm", "arm_speed", "apply_parameters") and sample.armed:
                return DeviceResult(False, "rejected", "必须先 STOP 并确认设备失能")
            if command in ("arm_pwm", "arm_speed") and not sample.local_enable:
                return DeviceResult(False, "rejected", "设备 STATE 表示本地禁止使能")
            if command in ("set_pwm", "set_speed"):
                mode = 1 if command == "set_pwm" else 2
                if not sample.armed or sample.control_mode != mode:
                    return DeviceResult(False, "rejected", "请先显式使能对应模式并等待 STATE 确认")
                if self._motion_latched_seq:
                    return DeviceResult(False, "rejected", "一次实验正在执行；不能重复、续租或延长期限")
                limit = self.project_limits["pwm_limit" if mode == 1 else "speed_limit_mm_s"]
                duration = values.get("duration_ms")
                if (any(isinstance(values.get(key), bool) or not isinstance(values.get(key), int)
                        or not -limit <= values[key] <= limit for key in ("left", "right"))
                    or isinstance(duration, bool) or not isinstance(duration, int)
                    or not 1 <= duration <= self.project_limits["max_duration_ms"]):
                    return DeviceResult(False, "rejected", "运动值或时长超过 CAPS 声明的上限")
                elapsed_ms = max(0, int((time.monotonic() - sample.received_monotonic) * 1000))
                values["deadline_tick"] = (sample.device_time_ms + elapsed_ms + duration) & 0xFFFFFFFF
        try:
            payload = self.adapter.encode_command(command, values)
        except (ValueError, TypeError) as exc:
            return DeviceResult(False, "rejected", str(exc))
        result = self._queue_project(command, payload)
        self.changed.emit()
        return result

    def _begin_project_session(self):
        payload = self.adapter.begin_session()
        result = self._queue_project("hello", payload)
        self.protocol_status = "等待 CAPS / HELLO ACK" if result.success else result.message

    def _handle_project_events(self):
        for event in self.adapter.pop_events():
            pending = self._project_pending.get(event.seq)
            if pending is None:
                self.add_log(f"忽略未关联或已超时的回包 seq={event.seq}", "WARNING")
                continue
            command = pending["command"]
            if event.kind == CAPS:
                if command != "hello" or event.seq != 1:
                    self.add_log("忽略未关联 HELLO 的 CAPS", "WARNING")
                    continue
                features, pwm, speed, duration, hz, state_hz, mode = event.values
                if features & ~0xF or not (0 < pwm <= 6000 and 0 < speed <= 300 and 0 < duration <= 1000 and hz > 0 and state_hz > 0):
                    self.add_log("CAPS 数值不满足 PROJECT_V1 有限实验边界", "ERROR")
                    continue
                self.project_limits = dict(pwm_limit=pwm, speed_limit_mm_s=speed,
                    max_duration_ms=duration, control_hz=hz, state_hz=state_hz, car_mode=mode)
                self.caps_received_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                self.capabilities = DeviceCapabilities(protocol=PROJECT_V1, telemetry=bool(features & 1),
                    parameter_read=bool(features & 2), parameter_write=bool(features & 2),
                    open_loop=bool(features & 4), motion=bool(features & 8),
                    reason="CAPS 未声明该能力；PROJECT_V1 原始发送始终禁用")
                self.protocol_status = "CAPS 已确认；等待 HELLO ACK" if not pending["ack"] else "PROJECT_V1 握手完成"
                pending["reply"] = True
            elif event.kind == ACK:
                kind, status = event.values
                if kind != COMMAND_TYPES[command] or status >= len(ACK_MESSAGES):
                    self.add_log("忽略命令类型或状态错误的 ACK", "WARNING")
                    continue
                if status:
                    result = DeviceResult(False, "rejected", ACK_MESSAGES[status], pending["request_id"])
                    self._remember_result(result)
                    self.add_log(result.message, "WARNING", request_id=result.request_id)
                    if command in ("read_parameters", "apply_parameters"):
                        self.parameter_status = "MCU 拒绝：" + result.message
                    if command == "hello":
                        self.capabilities = DeviceCapabilities(protocol=PROJECT_V1, reason="HELLO 被 MCU 拒绝，请重新连接")
                        self.protocol_status = "握手被拒绝"
                    if self._motion_latched_seq == event.seq:
                        self._motion_latched_seq = 0
                    del self._project_pending[event.seq]
                    continue
                pending["ack"] = True
                result = DeviceResult(True, "accepted", "MCU 已接受；运动与停车状态以 STATE 为准", pending["request_id"])
                self._remember_result(result)
                self.add_log(result.message, request_id=result.request_id)
                if command == "hello" and pending["reply"]:
                    self.protocol_status = "PROJECT_V1 握手完成"
            elif event.kind == PARAMS:
                if command not in ("read_parameters", "apply_parameters"):
                    self.add_log("忽略未关联参数请求的 PARAMS", "WARNING")
                    continue
                revision, *coefficients = event.values
                if any(value > 2000000 for value in coefficients):
                    self.add_log("PARAMS 参数越界，未更新实际值", "ERROR")
                    continue
                if (self.actual_parameters is not None
                        and ((revision - self.actual_parameters["revision"]) & 0xFFFFFFFF) >= 0x80000000):
                    self.add_log("忽略 revision 倒退的 PARAMS；未覆盖较新的实际 RAM 值", "WARNING")
                    continue
                self.actual_parameters = dict(zip(PARAMETER_KEYS, coefficients), revision=revision)
                self.parameters_received_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                pending["reply"] = True
                self.parameter_status = "实际 RAM 值已回读；等待 ACK" if not pending["ack"] else "实际 RAM 值已回读确认（不写 Flash）"
            needs_reply = command in ("hello", "read_parameters", "apply_parameters")
            if pending["ack"] and (not needs_reply or pending["reply"]):
                if command in ("read_parameters", "apply_parameters"):
                    self.parameter_status = "实际 RAM 值已回读确认（不写 Flash）"
                del self._project_pending[event.seq]

    def _expire_project_requests(self, now, disconnected=False):
        for seq, pending in tuple(self._project_pending.items()):
            if disconnected or now >= pending["due"]:
                message = "连接已关闭，结果未知；不会重发" if disconnected else "等待 MCU 响应超时，结果未知；不会重发"
                result = DeviceResult(False, "unknown", message, pending["request_id"])
                self._remember_result(result)
                self.add_log(message, "WARNING", request_id=result.request_id)
                if pending["command"] == "hello":
                    self.protocol_status = "握手超时，请核对协议并重新连接"
                    self.capabilities = DeviceCapabilities(protocol=PROJECT_V1, reason=self.protocol_status)
                if pending["command"] in ("read_parameters", "apply_parameters"):
                    self.parameter_status = "参数请求结果未知；请失能后重新读取"
                del self._project_pending[seq]

    @property
    def bluetooth_scanning(self) -> bool:
        return self.scanner is not None

    def scan_bluetooth(self) -> bool:
        if self.worker is not None or self.scanner is not None or self._shutting_down:
            return False
        try:
            if self.scanner_factory is None:
                from .bluetooth_worker import BluetoothScanner
                scanner = BluetoothScanner()
            else:
                scanner = self.scanner_factory()
            self.scanner = scanner
            self._scan_devices.clear()
            self._scan_error = ""
            self.bluetooth_devices.clear()
            self.bluetooth_scan_status = "正在扫描经典蓝牙设备（最多10秒）"
            scanner.start()
        except Exception as exc:
            self.scanner = None
            self.bluetooth_scan_status = f"扫描启动失败：{exc}"
            self.add_log(self.bluetooth_scan_status, "ERROR")
            self.changed.emit()
            return False
        self.add_log("开始扫描经典蓝牙；列表可能包含离线缓存设备")
        self.changed.emit()
        return True

    def cancel_bluetooth_scan(self) -> None:
        if self.scanner is not None:
            self.bluetooth_scan_status = "正在停止扫描"
            self.scanner.request_stop()
            self.changed.emit()

    def _poll_scanner(self) -> None:
        scanner = self.scanner
        if scanner is None:
            return
        finished = scanner.isFinished()
        events = scanner.drain_events()
        dropped = scanner.take_overflow_count()
        if dropped:
            self.add_log(f"蓝牙扫描缓存溢出：{dropped}个事件", "WARNING")
        for event in events:
            if event.connection_id and event.connection_id != scanner.connection_id:
                continue
            if event.kind == "device":
                address = event.payload.decode("ascii", errors="ignore").upper()
                if re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
                    if address in self._scan_devices or len(self._scan_devices) < 128:
                        self._scan_devices[address] = event.message[:248] or "未命名设备"
                        self.bluetooth_devices = sorted(((name, mac) for mac, name in self._scan_devices.items()),
                                                        key=lambda device: (device[0].casefold(), device[1]))
            elif event.kind == "error":
                self._scan_error = event.message
                self.bluetooth_scan_status = event.message
                self.add_log(event.message, "ERROR", connection_id=event.connection_id)
            elif event.kind in ("status", "closed") and event.message:
                self.bluetooth_scan_status = self._scan_error or event.message
        if finished and len(events) < 256:
            self.scanner = None
            pending, self._pending_connection = self._pending_connection, None
            if pending is not None and not self._shutting_down:
                self.connect_device(pending)

    def _log_connection_metadata(self) -> None:
        if self.config is None:
            return
        config = self.config
        metadata = {"kind": config.kind, "protocol": config.protocol,
                    "connection_id": self.connection_id}
        if config.kind == "serial":
            metadata.update(port=config.port, baudrate=config.baudrate, framing="8N1")
        elif config.kind == "bluetooth_spp":
            metadata.update(name=config.device_name, address=config.bluetooth_address,
                            rfcomm_channel=config.rfcomm_channel)
        self.add_log("连接配置：" + json.dumps(metadata, ensure_ascii=False))

    @property
    def receive_frequency(self) -> float:
        if len(self.frame_times) < 2 or self.telemetry_status != "接收正常":
            return 0.0
        duration = self.frame_times[-1] - self.frame_times[0]
        return (len(self.frame_times) - 1) / duration if duration > 0 else 0.0

    @property
    def telemetry_status(self) -> str:
        if not self.connected:
            return "连接已断开" if self.latest is not None else "无有效数据"
        if self.latest is None:
            return "等待有效遥测"
        return "数据过期" if time.monotonic() - self.latest.received_monotonic > 1.0 else "接收正常"

    def poll(self) -> None:
        self._poll_scanner()
        worker = self.worker
        if worker is not None:
            # Snapshot completion before drain: a live worker may append its
            # final events between the drain and an isFinished check.
            finished = worker.isFinished()
            events = worker.drain_events()
            dropped = worker.take_overflow_count()
            if dropped:
                self.adapter.reset()
                if isinstance(self.adapter, ProjectV1Adapter):
                    self.latest = None
                self.add_log(f"通信事件缓存溢出，丢失 {dropped} 个事件；遥测与录制可能存在缺口", "ERROR")
                if self.recorder.is_running:
                    self.recording_incomplete = True
            for event in events:
                if event.connection_id and event.connection_id != self.connection_id:
                    # Ignore callbacks belonging to an already retired connection.
                    continue
                if event.kind == "opened":
                    if self._closing_connection or self._shutting_down:
                        continue
                    was_connected = self.connected
                    self.connected = True
                    self.connection_status = {"SIMULATOR": "模拟设备已连接", "SERIAL": "串口已打开",
                                              "SIMULATION_PROJECT_V1": "PROJECT_V1 假设备已连接（纯软件）",
                                              "BLUETOOTH_SPP": "蓝牙SPP已连接"}[self.source]
                    self.add_log(event.message or self.connection_status)
                    if isinstance(self.adapter, ProjectV1Adapter) and not was_connected:
                        self._begin_project_session()
                elif event.kind in ("rx", "tx"):
                    tx_message = {"SIMULATOR": "模拟发送已接受，仅用于通信验证",
                                  "SIMULATION_PROJECT_V1": "假设备已接收，仅用于 PROJECT_V1 软件验证",
                                  "SERIAL": "已写入串口，未确认MCU执行",
                                  "BLUETOOTH_SPP": "已写入蓝牙连接，未确认MCU执行"}[self.source]
                    self.add_log(tx_message if event.kind == "tx" else "收到原始数据",
                                 payload=event.payload, direction=event.kind.upper(), utc=event.utc,
                                 monotonic=event.monotonic, request_id=event.request_id)
                    if event.kind == "rx":
                        self.bytes_received += len(event.payload)
                        for sample in self.adapter.feed(event.payload, event.monotonic, event.utc):
                            sample = replace(sample, connection_id=self.connection_id)
                            if sample.protocol == PROJECT_V1:
                                counts = (sample.rx_dropped, sample.tx_dropped)
                                if counts != self._device_dropped and any(counts):
                                    self.add_log(f"MCU 缓存丢失累计 RX={counts[0]} TX={counts[1]}；数据可能存在缺口", "ERROR")
                                    if self.recorder.is_running:
                                        self.recording_incomplete = True
                                self._device_dropped = counts
                            self.latest = sample
                            self.frames_received += 1
                            self.frame_times.append(sample.received_monotonic)
                            self.history.append(sample)
                            self.recorder.record_sample(sample)
                            if (self._motion_latched_seq and sample.protocol == PROJECT_V1
                                    and not sample.armed and sample.device_last_seq >= self._motion_latched_seq):
                                self._motion_latched_seq = 0
                            self.sample_received.emit(sample)
                        if isinstance(self.adapter, ProjectV1Adapter) and self.connected and not self._closing_connection:
                            self._handle_project_events()
                        for issue in self.adapter.pop_issues():
                            self.add_log(issue, "WARNING")
                    else:
                        self.bytes_sent += len(event.payload)
                elif event.kind == "error":
                    self.add_log(event.message, "ERROR", request_id=event.request_id)
                    self.connection_status = "连接异常"
                    self.connected = False
                    self.capabilities = DeviceCapabilities()
                    self._expire_project_requests(time.monotonic(), disconnected=True)
                elif event.kind == "closed":
                    self.connected = False
                    self.capabilities = DeviceCapabilities()
                    self._expire_project_requests(time.monotonic(), disconnected=True)
                    self.connection_status = "未连接"
                    self.add_log(event.message or "连接已关闭")
                elif event.kind == "status" and event.message:
                    self.add_log(event.message, request_id=event.request_id)
            if finished and len(events) < 256:
                # 已结束的线程不会继续入队；本轮满批时保留它到下次poll。
                self.worker = None
                self.connected = False
                self.capabilities = DeviceCapabilities()
                self.connection_status = "未连接"
        now = time.monotonic()
        self._expire_project_requests(now)
        self.history.prune(now)
        while self.frame_times and self.frame_times[0] < now - 2.0:
            self.frame_times.popleft()
        error, dropped = self.recorder.take_notifications()
        if error:
            self.recording_incomplete = True
            self.recording_status = "录制失败"
            self._recording_expected = False
            self.add_log(error, "ERROR", persist=False)
        if dropped:
            self.recording_incomplete = True
            self.add_log(f"录制缓存溢出，丢失 {dropped} 条记录；文件不完整", "ERROR", persist=False)
        if self._recording_stopping and not self.recorder.is_running:
            self._recording_stopping = False
            self.recording_status = "录制不完整" if self.recording_incomplete else "录制已保存"
        if self._recording_expected and not self.recorder.is_active:
            self._recording_expected = False
            self.recording_status = "录制已停止"
        self.changed.emit()

    def start_recording(self) -> bool:
        if self.control_owner and self.recorder.is_running:
            self.add_log("自动化阶段正在录制，不能重启录制", "WARNING")
            return False
        if not self.connected:
            self.add_log("连接设备后才能开始录制", "WARNING")
            return False
        try:
            directory = self.recorder.start(self.data_directory)
        except Exception as exc:
            self.recording_status = "录制启动失败"
            self.add_log(f"录制启动失败：{exc}", "ERROR")
            return False
        self._recording_expected = True
        self._recording_stopping = False
        self.recording_incomplete = False
        self.recording_status = f"正在录制 · {directory.name}"
        self.add_log(f"开始录制：{directory}")
        self._log_connection_metadata()
        self.changed.emit()
        return True

    def stop_recording(self, *, owner=None) -> bool:
        if self.control_owner and owner != self.control_owner:
            if self.automation_service is not None:
                self.automation_service.stop("录制被人工停止")
            return False
        self.add_log("停止录制并保存文件")
        done = self.recorder.stop(timeout=0)
        if getattr(self.recorder, "dropped_total", 0) or getattr(self.recorder, "failure_message", None):
            self.recording_incomplete = True
        self._recording_expected = False
        self._recording_stopping = not done
        error, dropped = self.recorder.take_notifications()
        if error or dropped:
            self.recording_incomplete = True
            self.add_log(error or f"录制丢失 {dropped} 条记录", "ERROR", persist=False)
        self.recording_status = ("录制不完整" if self.recording_incomplete else "录制已保存") if done else "文件仍在保存"
        self.changed.emit()
        return done

    def clear_logs(self) -> None:
        self.logs.clear()
        self.logs_cleared.emit()

    def clear_history(self) -> None:
        self.history.clear()
        self.changed.emit()

    def save_visible_logs(self, path: Path, display_hex: bool) -> bool:
        try:
            Path(path).write_text("\n".join(entry.format(display_hex) for entry in self.logs) + "\n", encoding="utf-8")
            self.add_log(f"已保存当前日志：{path}")
            return True
        except OSError as exc:
            self.add_log(f"日志保存失败：{exc}", "ERROR")
            return False

    def shutdown(self) -> bool:
        # Never wait for a process/thread or disk flush on the GUI thread.
        if self.automation_service is not None and not self.automation_service.shutdown():
            return False
        if not self._shutting_down:
            self._shutting_down = True
            self._pending_connection = None
            self.cancel_bluetooth_scan()
            self.disconnect_device()
        for _ in range(5):
            self.poll()
            if self.worker is None and self.scanner is None:
                break
        if self.worker is not None or self.scanner is not None:
            return False
        if self.recorder.is_running:
            if self.recorder.is_active and not self._recording_stopping:
                self.stop_recording()
            if self.recorder.is_running:
                return False
        self.timer.stop()
        return True
