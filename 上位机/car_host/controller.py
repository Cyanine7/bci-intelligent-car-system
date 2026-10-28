"""应用协调：UI 不直接访问串口，也不处理协议字节。"""

from __future__ import annotations

import re
import json
import math
import time
from time import perf_counter
from copy import deepcopy
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from .history import TelemetryHistory
from .models import TelemetrySample
from .device import DeviceCapabilities, DeviceResult, MotionService, ParameterService, capabilities_for_protocol, create_protocol_adapter
from .project_protocol import (PROJECT_V1, ProjectV1Adapter, CAPS, ACK, PARAMS, PROFILE_CAPS,
                               ENCODER_CAPS, COMMAND_TYPES, PARAMETER_KEYS, ACK_MESSAGES,
                               PWM_MAX_DURATION_MS, PROJECT_SPEED_LIMIT_MM_S)
from .encoder_totals import EncoderTotalsService
from .speed_control_service import SpeedControlService
from .yaw_service import YawService
from .yaw_models import COMMANDS as YAW_COMMANDS
from .speed_control_models import SPEED_COMMANDS, SPEED_DIAG
from .receive_health import ReceiveHealth, rx_metadata, rx_trace
from .recording import LogEntry, SessionRecorder
from .transport import ConnectionConfig, source_for_kind
from .worker import WorkerInterface, create_worker


MAX_RX_EVENT_BYTES = 4096


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
    encoder_totals_received = Signal(object)
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
        self.encoder_totals = EncoderTotalsService(self)
        self.speed_control = SpeedControlService(self)
        self.yaw_control = YawService(self)
        self.latest_encoder_totals = None
        self.encoder_status = "尚未查询全量编码器能力"
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
        self._recording_rx_started = True
        self._recording_boundary_stop_deadline = None
        self.receive_health = ReceiveHealth()
        self._io_events = deque()
        self._pumping_io = False
        self._rx_pump_scheduled = False
        self.io_backlog_exceeded = False
        self._last_io_batch = {}
        self._rx_events_rejected = 0
        self._stop_observations = {}
        self.profile_status = "尚未查询分段能力"
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
        self.latest_encoder_totals = None
        self.encoder_status = "尚未查询全量编码器能力"
        self.encoder_totals.invalidate("连接改变，手动计数基线失效")
        self.speed_control.reset()
        self.yaw_control.reset()
        self.command_metadata.clear()
        self.caps_received_utc = self.parameters_received_utc = None
        self._project_pending.clear()
        self._motion_latched_seq = self._state_required_seq = 0
        self._device_dropped = (0, 0)
        self.config = config
        self.connection_id = getattr(worker, "connection_id", "") or uuid4().hex
        self.receive_health.begin_epoch(self.connection_id, None)
        self._io_events.clear()
        self._rx_events_rejected = 0
        self.io_backlog_exceeded = False
        self._stop_observations.clear()
        self.profile_status = "尚未查询分段能力"
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
        self.speed_control.download_running = False
        self.yaw_control.download_running = False
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
        if self.encoder_totals.baseline is not None:
            if command == "stop":
                self.encoder_totals.cancel()
            else:
                return DeviceResult(False, "busy", "手动五圈计数正在进行，请先结束或取消")
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
        return any(p["command"] in ("arm_pwm", "arm_speed", "set_pwm", "set_speed", "set_pwm_profile", "stop", "speed_arm", "speed_run", "speed_arm_pwm", "speed_pwm", "yaw_arm", "yaw_run")
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
        now = time.monotonic()
        self.recorder.record_trace(dict(kind="request_result", connection_id=self.connection_id,
            request_id=result.request_id, status=result.status, message=result.message,
            controller_monotonic=now))
        stop = self._stop_observations.get(result.request_id)
        if stop and result.status == "accepted":
            stop["ack_accepted_monotonic"] = now

    def _queue_project(self, command, payload, values=None):
        request_id = uuid4().hex
        if not self.worker.request_send(payload, request_id=request_id):
            return DeviceResult(False, "rejected", "发送队列已满或连接正在关闭")
        seq = self.adapter.seq
        queued_monotonic = time.monotonic()
        self.command_metadata[request_id] = dict(command=command, seq=seq,
                                                session=self.adapter.session,
                                                queued_monotonic=queued_monotonic,
                                                values=deepcopy(values or {}))
        while len(self.command_metadata) > 128:
            del self.command_metadata[next(iter(self.command_metadata))]
        self._project_pending[seq] = {"command": command, "request_id": request_id,
                                      "due": time.monotonic() + 2.0,
                                      "ack": False, "reply": False}
        result = DeviceResult(True, "queued", "已排队；等待 MCU ACK，不自动重发", request_id)
        self._remember_result(result)
        self.add_log(f"PROJECT_V1 {command} 已排队，session={self.adapter.session:08X} seq={seq}",
                     payload=payload, direction="QUEUED", request_id=request_id)
        self.recorder.record_trace(dict(kind="command_queued", connection_id=self.connection_id,
                                       request_id=request_id, **self.command_metadata[request_id]))
        if command in ("arm_pwm", "arm_speed", "set_pwm", "set_speed", "set_pwm_profile", "stop", "speed_arm", "speed_run", "speed_arm_pwm", "speed_pwm", "yaw_arm", "yaw_run"):
            self._supersede_stop_observations(command, seq, queued_monotonic)
        if command == "stop":
            self.speed_control.download_running = False
            self.yaw_control.download_running = False
            self._stop_observations[request_id] = dict(request_id=request_id,
                session=self.adapter.session, seq=seq, queued_monotonic=queued_monotonic)
            while len(self._stop_observations) > 32:
                del self._stop_observations[next(iter(self._stop_observations))]
        if command in ("arm_pwm", "arm_speed", "set_pwm", "set_speed", "set_pwm_profile", "speed_arm", "speed_run", "speed_arm_pwm", "speed_pwm", "yaw_arm", "yaw_run"):
            self._state_required_seq = seq
        if command in ("set_pwm", "set_speed", "set_pwm_profile", "speed_run", "speed_pwm", "yaw_run"):
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
                            if p["command"] in ("read_parameters", "apply_parameters", "query_profile_caps", "query_encoder_totals")),
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
        if command in YAW_COMMANDS:
            try:
                values = self.yaw_control.prepare(command, values)
                payload = self.adapter.encode_command(command, values)
            except (ValueError, TypeError, KeyError) as exc:
                return DeviceResult(False, "rejected", str(exc))
            result = self._queue_project(command, payload, values)
            self.changed.emit()
            return result
        if command in SPEED_COMMANDS:
            try:
                values = self.speed_control.prepare(command, values)
                payload = self.adapter.encode_command(command, values)
            except (ValueError, TypeError, KeyError) as exc:
                return DeviceResult(False, "rejected", str(exc))
            result = self._queue_project(command, payload, values)
            self.changed.emit()
            return result
        if command == "query_profile_caps":
            if not self.project_state_fresh or self.latest.armed or self.project_request_pending or self.project_motion_active:
                return DeviceResult(False, "rejected", "分段能力查询需要新鲜 STATE、失能且无在途请求")
            self.capabilities = replace(self.capabilities, pwm_profile=False)
            self.profile_status = "等待分段 CAPS 与 ACK"
        if command == "query_encoder_totals":
            if not self.project_state_fresh or self.latest.armed or self.project_request_pending or self.project_motion_active:
                return DeviceResult(False, "rejected", "全量计数查询需要新鲜 STATE、失能且无在途请求")
            self.capabilities = replace(self.capabilities, encoder_totals=False)
            self.encoder_status = "等待全量编码器 CAPS 与 ACK"
        if command in ("arm_pwm", "arm_speed", "set_pwm", "set_speed", "set_pwm_profile", "apply_parameters"):
            if self.io_backlog_exceeded:
                return DeviceResult(False, "rejected", "接收存在超预算积压；等待通信健康恢复")
            if not self.project_state_fresh:
                return DeviceResult(False, "rejected", "需要 500 ms 内且已覆盖前次控制指令的有效 STATE")
            sample = self.latest
            pending_motion = self.project_control_pending or any(
                p["command"] in ("query_profile_caps", "query_encoder_totals") for p in self._project_pending.values())
            if pending_motion:
                return DeviceResult(False, "rejected", "前次控制请求尚未确认，请等待 ACK 与 STATE")
            if command in ("arm_pwm", "arm_speed", "apply_parameters") and sample.armed:
                return DeviceResult(False, "rejected", "必须先 STOP 并确认设备失能")
            if command in ("arm_pwm", "arm_speed") and not sample.local_enable:
                return DeviceResult(False, "rejected", "设备 STATE 表示本地禁止使能")
            if command in ("set_pwm", "set_speed", "set_pwm_profile"):
                mode = 2 if command == "set_speed" else 1
                if not sample.armed or sample.control_mode != mode:
                    return DeviceResult(False, "rejected", "请先显式使能对应模式并等待 STATE 确认")
                if self._motion_latched_seq:
                    return DeviceResult(False, "rejected", "一次实验正在执行；不能重复、续租或延长期限")
                if command == "set_pwm_profile":
                    segments = values.get("segments")
                    limit = min(self.project_limits["pwm_limit"], self.project_limits["profile_pwm_limit"])
                    if not isinstance(segments, list) or len(segments) != 2:
                        return DeviceResult(False, "rejected", "分段实验需要恰好两段")
                    for segment in segments:
                        if (not isinstance(segment, dict) or set(segment) != {"left_pwm", "right_pwm", "duration_ms"}
                            or any(type(segment.get(k)) is not int or not -limit <= segment[k] <= limit for k in ("left_pwm", "right_pwm"))
                            or type(segment.get("duration_ms")) is not int or segment["duration_ms"] < 1):
                            return DeviceResult(False, "rejected", "分段 PWM 或时长无效")
                    duration = sum(segment["duration_ms"] for segment in segments)
                    if duration > min(self.project_limits["max_duration_ms"], self.project_limits["profile_max_total_ms"]):
                        return DeviceResult(False, "rejected", "分段总时长超过实际 CAPS")
                    if any(segments[0][k] * segments[1][k] < 0 for k in ("left_pwm", "right_pwm")):
                        return DeviceResult(False, "rejected", "同轮两段不得换向")
                else:
                    limit = self.project_limits["pwm_limit" if mode == 1 else "speed_limit_mm_s"]
                    duration = values.get("duration_ms")
                    if (any(isinstance(values.get(key), bool) or not isinstance(values.get(key), int)
                            or not -limit <= values[key] <= limit for key in ("left", "right"))
                        or isinstance(duration, bool) or not isinstance(duration, int)
                        or not 1 <= duration <= min(self.project_limits["max_duration_ms"], PWM_MAX_DURATION_MS if mode == 1 else 1000)):
                        return DeviceResult(False, "rejected", "运动值或时长超过 CAPS 声明的上限")
                # STATE.tick advances on the advertised control period. A
                # millisecond estimate can run ahead of that discrete clock and
                # reject a full 1000ms pulse even with zero transport latency.
                # Round elapsed control ticks down; acquisition age stays exact.
                control_hz = self.project_limits["control_hz"]
                elapsed_ticks = max(0, int((time.monotonic() - sample.received_monotonic) * control_hz))
                elapsed_ms = int(elapsed_ticks * 1000 / control_hz)
                values["deadline_tick"] = (sample.device_time_ms + elapsed_ms + duration) & 0xFFFFFFFF
        try:
            payload = self.adapter.encode_command(command, values)
        except (ValueError, TypeError) as exc:
            return DeviceResult(False, "rejected", str(exc))
        result = self._queue_project(command, payload, values)
        self.changed.emit()
        return result

    def _begin_project_session(self):
        self.speed_control.reset()
        self.yaw_control.reset()
        payload = self.adapter.begin_session()
        self.latest = None
        self.receive_health.begin_epoch(self.connection_id, self.adapter.session,
                                        counters=dict(parser=self._parser_counters()), now=time.monotonic())
        self._trace_health_baselines()
        result = self._queue_project("hello", payload)
        self.protocol_status = "等待 CAPS / HELLO ACK" if result.success else result.message

    def _handle_project_events(self, received_monotonic=None):
        for event in self.adapter.pop_events():
            if event.kind in (0x97, 0x98):
                try:self.yaw_control.diagnostic(event)
                except ValueError as exc:self.add_log(str(exc), "WARNING")
                continue
            if event.kind == SPEED_DIAG:
                try:
                    self.speed_control.diagnostic(event)
                except ValueError as exc:
                    self.add_log(str(exc), "WARNING")
                continue
            pending = self._project_pending.get(event.seq)
            if pending is None:
                self.add_log(f"忽略未关联或已超时的回包 seq={event.seq}", "WARNING")
                continue
            command = pending["command"]
            if command in YAW_COMMANDS:
                try:self.yaw_control.handle(event, pending)
                except (ValueError, KeyError, TypeError) as exc:self.add_log(str(exc), "WARNING")
                continue
            if command in SPEED_COMMANDS:
                try:
                    self.speed_control.handle(event, pending)
                except (ValueError, KeyError) as exc:
                    self.add_log(str(exc), "WARNING")
                continue
            if event.kind == CAPS:
                if command != "hello" or event.seq != 1:
                    self.add_log("忽略未关联 HELLO 的 CAPS", "WARNING")
                    continue
                features, pwm, speed, duration, hz, state_hz, mode = event.values
                if features & ~0xF or not (0 < pwm <= 10000 and 0 < speed <= PROJECT_SPEED_LIMIT_MM_S and 0 < duration <= PWM_MAX_DURATION_MS and hz > 0 and state_hz > 0):
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
            elif event.kind == PROFILE_CAPS:
                if command != "query_profile_caps":
                    self.add_log("忽略未关联查询的分段 CAPS", "WARNING")
                    continue
                features, version, segments, pwm, total, hz, state_hz = event.values
                if (features != 3 or version != 1 or segments != 2 or not 0 < pwm <= 10000
                    or not 2 <= total <= PWM_MAX_DURATION_MS or hz != 100 or state_hz != 20):
                    self.add_log("分段 CAPS 不符合有限双段契约", "ERROR")
                    continue
                pending["profile_limits"] = dict(profile_version=version, profile_max_segments=segments,
                    profile_pwm_limit=pwm, profile_max_total_ms=total, profile_control_hz=hz,
                    profile_state_hz=state_hz, profile_min_segment_ms=1)
                pending["reply"] = True
            elif event.kind == ENCODER_CAPS:
                if command != "query_encoder_totals":
                    self.add_log("忽略未关联查询的全量编码器 CAPS", "WARNING")
                    continue
                version, bits, hz, report_hz, reserved, counts, diameter, spacing = event.values
                if ((version, bits, hz, report_hz, reserved) != (1, 64, 100, 20, 0)
                    or not 1 <= counts <= 10000000 or not 1000 <= diameter <= 1000000
                    or not 1000 <= spacing <= 5000000):
                    self.add_log("全量编码器 CAPS 不符合计数契约", "ERROR")
                    continue
                pending["encoder_limits"] = dict(encoder_version=version,
                    encoder_bits=bits, encoder_sample_hz=hz, encoder_report_hz=report_hz,
                    encoder_counts_per_turn_nominal=counts, wheel_diameter_um=diameter,
                    wheel_center_spacing_um=spacing)
                pending["reply"] = True
            elif event.kind == ACK:
                kind, status = event.values
                if kind != COMMAND_TYPES[command] or status >= len(ACK_MESSAGES):
                    self.add_log("忽略命令类型或状态错误的 ACK", "WARNING")
                    continue
                stop = self._stop_observations.get(pending["request_id"])
                if stop and received_monotonic is not None:
                    stop["ack_captured_monotonic"] = received_monotonic
                self.recorder.record_trace(dict(kind="ack_observation", connection_id=self.connection_id,
                    request_id=pending["request_id"], seq=event.seq, session=event.session,
                    status=status, captured_monotonic=received_monotonic,
                    accepted_monotonic=time.monotonic()))
                if status:
                    result = DeviceResult(False, "rejected", ACK_MESSAGES[status], pending["request_id"])
                    self._remember_result(result)
                    self.add_log(result.message, "WARNING", request_id=result.request_id)
                    if command in ("read_parameters", "apply_parameters"):
                        self.parameter_status = "MCU 拒绝：" + result.message
                    if command == "hello":
                        self.capabilities = DeviceCapabilities(protocol=PROJECT_V1, reason="HELLO 被 MCU 拒绝，请重新连接")
                        self.protocol_status = "握手被拒绝"
                    if command == "query_profile_caps":
                        self.capabilities = replace(self.capabilities, pwm_profile=False)
                        self.profile_status = "当前固件未支持分段测量：" + result.message
                    if command == "query_encoder_totals":
                        self.capabilities = replace(self.capabilities, encoder_totals=False)
                        self.encoder_status = "当前固件未支持全量累计：" + result.message
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
            needs_reply = command in ("hello", "read_parameters", "apply_parameters", "query_profile_caps", "query_encoder_totals")
            if pending["ack"] and (not needs_reply or pending["reply"]):
                if command == "query_profile_caps":
                    self.project_limits.update(pending["profile_limits"])
                    self.capabilities = replace(self.capabilities, pwm_profile=True)
                    self.profile_status = "已确认双段 PWM 能力及完整分段遥测"
                if command == "query_encoder_totals":
                    self.project_limits.update(pending["encoder_limits"])
                    self.capabilities = replace(self.capabilities, encoder_totals=True)
                    self.encoder_status = "已确认100Hz累计、20Hz快照；查询不使能电机"
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
                if pending["command"] == "query_profile_caps":
                    self.capabilities = replace(self.capabilities, pwm_profile=False)
                    self.profile_status = "分段能力查询结果未知；未启用，不自动重发"
                if pending["command"] == "query_encoder_totals":
                    self.capabilities = replace(self.capabilities, encoder_totals=False)
                    self.encoder_status = "全量计数查询结果未知；不自动重发"
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

    def _parser_counters(self):
        return {key: value for key, value in getattr(self.adapter, "diagnostics", {}).items()
                if key not in ("buffered_bytes", "epoch", "parser_epoch")}

    def communication_health(self):
        queue = self._queue_snapshot()
        parser = getattr(self.adapter, "diagnostics", {})
        counters = dict(parser=self._parser_counters(),
                        device=dict(rx_dropped=getattr(self.latest, "rx_dropped", None),
                                    tx_dropped=getattr(self.latest, "tx_dropped", None)),
                        transport_dropped=queue.get("overflow_total"),
                        controller_rx_rejected=self._rx_events_rejected,
                        recording_dropped=self.recorder.dropped_total,
                        recording_incomplete=self.recording_incomplete)
        if self.latest is None:
            self.receive_health.invalidate_latest()
        snapshot = self.receive_health.snapshot(time.monotonic(), counters=counters, queue=queue)
        self._trace_health_baselines(snapshot.get("baseline_events_dropped", 0))
        snapshot.update(io_backlog_exceeded=self.io_backlog_exceeded,
                        connected=self.connected,
                        last_batch=dict(self._last_io_batch), profile_status=self.profile_status,
                        parser_epoch=parser.get("parser_epoch"),
                        recording_incomplete=self.recording_incomplete,
                        stops=[{k: v for k, v in stop.items() if not k.startswith("_")}
                               for stop in self._stop_observations.values()])
        return snapshot

    def _trace_health_baselines(self, dropped=None):
        if dropped is None:
            dropped = self.receive_health.baseline_events_dropped
        if dropped is not None:
            previous = getattr(self, "_health_baseline_dropped", 0)
            if dropped > previous and self.recorder.is_active:
                self.recording_incomplete = True
                self.recording_status = "录制不完整"
                self.add_log("健康基线诊断队列溢出；原始记录不完整", "ERROR", persist=False)
            self._health_baseline_dropped = dropped
        for record in self.receive_health.pop_baseline_events():
            self.recorder.record_trace(record)

    def _queue_snapshot(self):
        worker = self.worker
        if worker is None:
            return dict(depth=len(self._io_events), controller_pending=len(self._io_events))
        method = getattr(worker, "event_queue_snapshot", None)
        snapshot = method() if method else dict(depth=len(getattr(worker, "events", ())))
        return dict(snapshot, controller_pending=len(self._io_events))

    def _supersede_stop_observations(self, command, seq, now):
        for stop in self._stop_observations.values():
            if (stop["session"] != self.adapter.session or "continuous_zero_monotonic" in stop
                    or "superseded_by_seq" in stop):
                continue
            stop.update(superseded_by_seq=seq, superseded_by_command=command,
                        observation_ended_monotonic=now, observation_status="superseded")
            stop.pop("_quiet_since", None)
            self.recorder.record_trace(dict(kind="stop_observation", connection_id=self.connection_id,
                **{key: value for key, value in stop.items() if not key.startswith("_")}))

    def _observe_stop(self, sample):
        for stop in self._stop_observations.values():
            if "superseded_by_seq" in stop:
                continue
            if sample.device_session != stop["session"] or sample.device_last_seq < stop["seq"]:
                continue
            previous = set(stop)
            stopped = not sample.armed and sample.pwm_left == 0 and sample.pwm_right == 0
            if stopped and "first_disarmed_monotonic" not in stop:
                stop.update(first_disarmed_monotonic=sample.received_monotonic,
                            first_disarmed_tick=sample.device_time_ms)
            zero = stopped and sample.signed_left_speed_mps == 0 and sample.signed_right_speed_mps == 0
            if zero:
                if "first_zero_monotonic" not in stop:
                    stop.update(first_zero_monotonic=sample.received_monotonic, first_zero_tick=sample.device_time_ms)
                if (stop.get("_quiet_last") is None or sample.received_monotonic - stop["_quiet_last"] >= .5):
                    stop["_quiet_since"] = sample.received_monotonic
                stop.setdefault("_quiet_since", sample.received_monotonic)
                if sample.received_monotonic - stop["_quiet_since"] >= 1 and "continuous_zero_monotonic" not in stop:
                    stop.update(continuous_zero_monotonic=sample.received_monotonic,
                                continuous_zero_tick=sample.device_time_ms)
            else:
                stop.pop("_quiet_since", None)
            stop["_quiet_last"] = sample.received_monotonic
            if set(stop) - previous - {"_quiet_last", "_quiet_since"}:
                self.recorder.record_trace(dict(kind="stop_observation", connection_id=self.connection_id,
                    **{k: v for k, v in stop.items() if not k.startswith("_")}))

    def _reject_oversized_rx(self, event):
        """Retire an invalid worker event without copying an unbounded payload."""
        self._rx_events_rejected += 1
        self.bytes_received += len(event.payload)
        self.adapter.reset()
        self.latest = None
        self.receive_health.invalidate_latest()
        if self.recorder.is_running:
            self.recording_incomplete = True
        self.add_log(f"RX 事件超过传输上限 {MAX_RX_EVENT_BYTES} 字节，已丢弃；接收与录制可能不完整", "ERROR")
        self.recorder.record_trace(dict(kind="rx_rejected", reason="oversized_event",
            connection_id=self.connection_id, rx_block_id=getattr(event, "rx_block_id", None),
            byte_count=len(event.payload), captured_monotonic=event.monotonic,
            controller_monotonic=time.monotonic()))

    def pump_io(self, max_events=256, max_bytes=65536, time_budget_ms=5):
        """Nonblocking ordered input pump shared by polling and experiment health checks."""
        if type(max_events) is not int or max_events < 1:
            raise ValueError("接收事件预算必须为正整数")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("接收字节预算必须为正整数")
        if (isinstance(time_budget_ms, bool) or not isinstance(time_budget_ms, (int, float))
                or not math.isfinite(time_budget_ms) or time_budget_ms <= 0):
            raise ValueError("接收时间预算必须为正有限毫秒数")
        max_events = min(max_events, 256)
        max_bytes = min(max_bytes, 65536)
        if self._pumping_io:
            return dict(reentrant=True, budget_exceeded=self.io_backlog_exceeded)
        self._pumping_io = True
        started = time.monotonic()
        budget_start = perf_counter()
        worker = self.worker
        processed = 0
        processed_bytes = 0
        input_result = {}
        finished = False
        try:
            if worker is not None:
                pull = getattr(worker, "pull_available_input", None)
                if pull:
                    input_result = pull(max_bytes=max_bytes)
                # Snapshot completion before drain: a live worker may append its
                # final events between the drain and an isFinished check.
                finished = worker.isFinished()
                if not self._io_events:
                    self._io_events.extend(worker.drain_events(max_count=max_events))
                dropped = worker.take_overflow_count()
                if dropped:
                    self.adapter.reset()
                    if isinstance(self.adapter, ProjectV1Adapter):
                        self.latest = None
                        self.receive_health.invalidate_latest()
                    self.add_log(f"通信事件缓存溢出，丢失 {dropped} 个事件；遥测与录制可能存在缺口", "ERROR")
                    if self.recorder.is_running:
                        self.recording_incomplete = True
                while self._io_events and processed < max_events:
                    if processed and (perf_counter() - budget_start) * 1000 >= time_budget_ms:
                        break
                    next_event = self._io_events[0]
                    if next_event.connection_id and next_event.connection_id != self.connection_id:
                        # Retired connections cannot pin the head of the byte budget.
                        self._io_events.popleft()
                        processed += 1
                        continue
                    next_size = len(next_event.payload)
                    if next_event.kind == "rx" and next_size > MAX_RX_EVENT_BYTES:
                        self._io_events.popleft()
                        processed += 1
                        self._reject_oversized_rx(next_event)
                        continue
                    if processed_bytes + next_size > max_bytes:
                        break
                    event = self._io_events.popleft()
                    processed += 1
                    processed_bytes += len(event.payload)
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
                        record_rx = True
                        if event.kind == "rx" and self.recorder.is_active and not self._recording_rx_started:
                            # Admit raw bytes and decoded rows together, at a real frame boundary.
                            # Do not reconstruct the missing prefix from a decoded snapshot.
                            if self.adapter.diagnostics.get("buffered_bytes", 0) == 0:
                                self._recording_rx_started = True
                                self.recorder.record_trace(dict(kind="recording_rx_boundary_start",
                                    connection_id=self.connection_id, monotonic=event.monotonic))
                            else:
                                record_rx = False
                                self.recorder.record_trace(dict(kind="recording_boundary_wait",
                                    connection_id=self.connection_id, monotonic=event.monotonic,
                                    omitted_rx_bytes=len(event.payload)))
                        tx_message = {"SIMULATOR": "模拟发送已接受，仅用于通信验证",
                                      "SIMULATION_PROJECT_V1": "假设备已接收，仅用于 PROJECT_V1 软件验证",
                                      "SERIAL": "已写入串口，未确认MCU执行",
                                      "BLUETOOTH_SPP": "已写入蓝牙连接，未确认MCU执行"}[self.source]
                        self.add_log(tx_message if event.kind == "tx" else "收到原始数据",
                                     payload=event.payload, direction=event.kind.upper(), utc=event.utc,
                                     monotonic=event.monotonic, request_id=event.request_id,
                                     persist=record_rx)
                        if event.kind == "rx":
                            processing_started = time.monotonic()
                            self.bytes_received += len(event.payload)
                            if isinstance(self.adapter, ProjectV1Adapter):
                                parser_before = self.adapter.diagnostics
                                samples = self.adapter.feed(event.payload, event.monotonic, event.utc,
                                                            rx_metadata=rx_metadata(event))
                                parser_after = self.adapter.diagnostics
                                damaged = any(parser_after.get(k, 0) > parser_before.get(k, 0) for k in (
                                    "rejected_frames", "discarded_bytes", "partial_timeouts", "event_dropped",
                                    "unknown_frames", "session_rejected", "state_invalid", "state_replayed",
                                    "encoder_invalid", "encoder_replayed",
                                    "diagnostics_dropped", "issues_dropped"))
                                if damaged and self.recorder.is_running:
                                    self.recording_incomplete = True
                            else:
                                samples = self.adapter.feed(event.payload, event.monotonic, event.utc)
                            for sample in samples:
                                sample = replace(sample, connection_id=self.connection_id,
                                    controller_started_monotonic=processing_started,
                                    controller_completed_monotonic=time.monotonic())
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
                                if record_rx:
                                    self.recorder.record_sample(sample)
                                self.receive_health.begin_epoch(
                                    self.connection_id, sample.device_session,
                                    counters=dict(parser=self._parser_counters()), now=time.monotonic())
                                self.receive_health.observe_sample(sample, time.monotonic())
                                if sample.protocol == PROJECT_V1:
                                    self._observe_stop(sample)
                                if (self._motion_latched_seq and sample.protocol == PROJECT_V1
                                        and not sample.armed and sample.device_last_seq >= self._motion_latched_seq):
                                    self._motion_latched_seq = 0
                                self.sample_received.emit(sample)
                            if isinstance(self.adapter, ProjectV1Adapter) and self.connected and not self._closing_connection:
                                self._handle_project_events(event.monotonic)
                                for total in self.adapter.pop_encoder_totals():
                                    total = replace(total, connection_id=self.connection_id)
                                    self.latest_encoder_totals = total
                                    self.encoder_totals.observe(total)
                                    if record_rx:
                                        self.recorder.record_encoder_totals(total)
                                    self.encoder_totals_received.emit(total)
                            for issue in self.adapter.pop_issues():
                                self.add_log(issue, "WARNING")
                            diagnostics = getattr(self.adapter, "pop_diagnostics", None)
                            if diagnostics:
                                for diagnostic in diagnostics():
                                    self.recorder.record_trace(dict(diagnostic, connection_id=self.connection_id))
                            self.recorder.record_trace(rx_trace(event, processing_started, time.monotonic()))
                        else:
                            self.bytes_sent += len(event.payload)
                            stop = self._stop_observations.get(event.request_id)
                            if stop:
                                stop.setdefault("written_monotonic", event.monotonic)
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
                queue = self._queue_snapshot()
                if finished and not self._io_events and not queue.get("depth", 0) and processed < max_events:
                    # 已结束的线程不会继续入队；本轮满批时保留它到下次poll。
                    self.worker = None
                    self.connected = False
                    self.capabilities = DeviceCapabilities()
                    self.connection_status = "未连接"
            queue = self._queue_snapshot()
            pending_count = len(self._io_events) + queue.get("depth", 0)
            has_pending = bool(pending_count or input_result.get("budget_exceeded", False))
            oldest = [event.monotonic for event in self._io_events if event.monotonic is not None]
            if queue.get("oldest_monotonic") is not None:
                oldest.append(queue["oldest_monotonic"])
            pending_age = max(0.0, time.monotonic() - min(oldest)) if oldest else 0.0
            # The 5ms callback budget is a scheduling yield, not a queue capacity.
            # Byte/event exhaustion, aged backlog and input/overflow bounds remain errors.
            self.io_backlog_exceeded = bool(input_result.get("budget_exceeded", False)
                or (has_pending and (processed >= max_events or processed_bytes >= max_bytes
                    or pending_count >= max_events or pending_age >= .5)))
            if has_pending and not self.io_backlog_exceeded and not self._rx_pump_scheduled:
                self._rx_pump_scheduled = True
                QTimer.singleShot(0, self._continue_rx_pump)
            completed = time.monotonic()
            batch = dict(kind="controller_batch", connection_id=self.connection_id,
                started_monotonic=started, completed_monotonic=completed, processed=processed,
                event_count=processed, bytes_read=input_result.get("bytes_read", 0),
                processed_bytes=processed_bytes,
                remaining_bytes=input_result.get("remaining_bytes", 0),
                budget_exceeded=self.io_backlog_exceeded, queue=queue, input=input_result,
                budget_yielded=has_pending, pending_age_ms=pending_age * 1000,
                latest_frame_seq=getattr(self.latest, "frame_seq", None),
                latest_age_ms=(completed - self.latest.received_monotonic) * 1000 if self.latest else None)
            self._last_io_batch = batch
            self.receive_health.observe_batch(batch)
            self._trace_health_baselines()
            if processed or has_pending:
                self.recorder.record_trace(batch)
            return dict(batch)
        finally:
            self._pumping_io = False

    def _continue_rx_pump(self):
        self._rx_pump_scheduled = False
        if not self._shutting_down:
            self.pump_io()

    def poll(self) -> None:
        self._poll_scanner()
        self.pump_io()
        now = time.monotonic()
        if self._recording_boundary_stop_deadline is not None:
            pending = self.adapter.diagnostics.get("buffered_bytes", 0)
            if not pending or now >= self._recording_boundary_stop_deadline or not self.connected:
                self._recording_boundary_stop_deadline = None
                if pending:
                    self.recording_incomplete = True
                    self.add_log("录制结束等待完整帧超时或连接关闭，保留残片并标记不完整", "ERROR")
                self._stop_recording_now()
        self._expire_project_requests(now)
        self.speed_control.poll()
        self.yaw_control.poll()
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
        self._recording_boundary_stop_deadline = None
        self._recording_rx_started = not isinstance(self.adapter, ProjectV1Adapter) or not self.adapter.diagnostics.get("buffered_bytes", 0)
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
        if self.recorder.is_active and isinstance(self.adapter, ProjectV1Adapter) and self.adapter.diagnostics.get("buffered_bytes", 0):
            if self._recording_boundary_stop_deadline is None:
                self._recording_boundary_stop_deadline = time.monotonic() + 0.2
                self.add_log("录制结束等待当前完整帧，最多200ms")
            return False
        return self._stop_recording_now()

    def _stop_recording_now(self) -> bool:
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
