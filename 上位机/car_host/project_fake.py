"""Injectable PROJECT_V1 software device; never opens serial or Bluetooth.

The signed feedback is a deterministic test value, not a physical motor model.
This worker is deliberately separate from the legacy simulator and normal
transport factory. The automation demo factory accepts only its sentinel port.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import struct
import time
from uuid import uuid4

from .project_protocol import (
    ACK, ARM, CAPS, CAPS_LAYOUT, GET_PARAMS, HEADER, HELLO, PARAMS,
    PARAMS_LAYOUT, PROJECT_V1, SET_PARAMS, SET_PWM, SET_SPEED, STATE,
    STATE_LAYOUT, STOP, crc16, encode_frame, SET_PWM_PROFILE, GET_PROFILE_CAPS,
    PROFILE_CAPS, STATE_PROFILE, PROFILE_CAPS_LAYOUT, PROFILE_COMMAND_LAYOUT, PROFILE_STATE_LAYOUT,
    GET_ENCODER_CAPS, ENCODER_CAPS, ENCODER_TOTALS, ENCODER_CAPS_LAYOUT, ENCODER_TOTALS_LAYOUT,
    PROJECT_SPEED_LIMIT_MM_S,
)
from .transport import ConnectionConfig, IoEvent


FAKE_PORT = "PROJECT_V1_FAKE"
FAKE_SOURCE = "SIMULATION_PROJECT_V1"
FAKE_CONFIG = ConnectionConfig(kind="serial", port=FAKE_PORT, protocol=PROJECT_V1)


class ProjectFakeWorker:
    """WorkerInterface implementation advanced by drain_events or advance.

    Faults can be changed in ``faults`` during a test: ``dropped`` (RX count),
    ``disable``, ``stale`` (withhold STATE), ``reject`` (all or a command kind),
    ``missingack`` (all or a command kind), ``disconnect``, ``start_failure``
    (zero wheel feedback), ``ack_delay_ms`` and ``session_change``.
    """

    source = FAKE_SOURCE
    EVENT_QUEUE_CAPACITY = 1024
    MAX_PAYLOAD_BYTES = 4096
    TICK_SECONDS = .01
    STATE_TICKS = 5

    def __init__(self, config: ConnectionConfig, *, clock=time.monotonic, faults=None):
        if config.kind != "serial" or config.port != FAKE_PORT or config.protocol != PROJECT_V1:
            raise ValueError("PROJECT_V1 假设备只接受 serial/PROJECT_V1_FAKE/PROJECT_V1；不会访问硬件")
        self.config = config
        self.connection_id = uuid4().hex
        self.clock = clock
        self.faults = dict(faults or {})
        self.sent = deque(maxlen=1024)
        self._events = deque(maxlen=self.EVENT_QUEUE_CAPACITY)
        self._delayed = deque(maxlen=128)
        self._overflow = 0
        self._running = self._finished = False
        self._last_time = self.clock()
        self._tick_number = 0
        self.tick_ms = 0
        self.session = self.seq = 0
        self._sessions = set()
        self.armed = False
        self.mode = self.reason = 0
        self.local_enable = True
        self.pwm_left = self.pwm_right = 0
        self.rx_dropped = self.tx_dropped = 0
        self.parameter_revision = 0
        self.parameters = [0, 0, 0, 0]
        self._deadline = self._idle_deadline = None
        self._profile_enabled = False
        self._report_seq = 0
        self._encoder_enabled = False
        self._encoder_report_seq = 0
        self._encoder_epoch = 0
        self._encoder_initialized = False
        self._encoder_sample_seq = 0
        self._encoder_totals = [0, 0]
        self._encoder_delayed = 0
        self._clear_profile()
        self._last_drop_fault = self._last_session_fault = None
        from .speed_control_fake import FakeSpeedDevice
        self.speed = FakeSpeedDevice(self)
        from .yaw_fake import FakeYawDevice
        self.yaw = FakeYawDevice(self)

    def start(self):
        if self._running:
            return
        self._running, self._finished = True, False
        self._last_time = self.clock()
        self._emit("opened", message="PROJECT_V1 纯软件假设备已打开，无物理运动验证")

    def request_stop(self):
        self._stop(1)
        self._running, self._finished = False, True
        self._delayed.clear()
        self._emit("closed", message="PROJECT_V1 假设备已关闭")

    def isFinished(self):
        return self._finished

    def take_overflow_count(self):
        count, self._overflow = self._overflow, 0
        return count

    def drain_events(self, max_count=256):
        self.advance()
        return [self._events.popleft() for _ in range(min(max(0, max_count), len(self._events), self.EVENT_QUEUE_CAPACITY))]

    def set_fault(self, name, value=True):
        self.faults[name] = value

    def advance(self):
        now = self.clock()
        if not self._running:
            return
        if self.faults.get("disconnect"):
            self._stop(1)
            self._running, self._finished = False, True
            self._delayed.clear()
            self._emit("error", message="注入故障：PROJECT_V1 假设备掉线")
            self._emit("closed", message="假链路已断开，不自动恢复")
            return
        self.local_enable = not bool(self.faults.get("disable"))
        if not self.local_enable:
            self._stop(3)
        dropped = self.faults.get("dropped", 0)
        if dropped and dropped != self._last_drop_fault:
            self.rx_dropped += max(1, int(dropped))
            self._last_drop_fault = dropped
            self._stop(4)
        change = self.faults.get("session_change")
        if change and change != self._last_session_fault:
            self.session = (self.session + 1) & 0xFFFFFFFF or 1
            self._last_session_fault = change
            self._stop(0)
        steps = max(0, int((now - self._last_time + 1e-9) / self.TICK_SECONDS))
        # Work remains bounded even when a test advances its clock by hours.
        if steps > 1000:
            skipped = steps - 1000
            self.tick_ms = (self.tick_ms + skipped * 10) & 0xFFFFFFFF
            self._tick_number += skipped
            self._last_time += skipped * self.TICK_SECONDS
            self._encoder_delayed += 1
            steps = 1000
        for _ in range(steps):
            self._last_time += self.TICK_SECONDS
            self.tick_ms = (self.tick_ms + 10) & 0xFFFFFFFF
            self._tick_number += 1
            if not self._encoder_initialized:
                self._encoder_epoch = self.tick_ms
                self._encoder_initialized = True
            self._encoder_sample_seq = (self._encoder_sample_seq + 1) & 0xffffffff
            for side, key in enumerate(("manual_left_counts", "manual_right_counts")):
                self._encoder_totals[side] += int(self.faults.get(key, 0))
            if self.armed and self._deadline is not None and self._reached(self._deadline):
                self._stop(2)
            elif self.armed and self._deadline is None and self._idle_deadline is not None and self._reached(self._idle_deadline):
                self._stop(5)
            if self.armed and self.profile_status == 1 and self.segment_index != 1 and self._reached(self._profile_switch_tick):
                self.pwm_left, self.pwm_right = self._profile_second
                self.segment_index = 1
                self.segment1_actual_tick = self.tick_ms
            self.speed.tick()
            self.yaw.tick()
            if self.session and self._tick_number % self.STATE_TICKS == 0 and not self.faults.get("stale"):
                self._state(min(now, self._last_time))
                self.speed.report(min(now, self._last_time))
                self.yaw.report(min(now, self._last_time))
                if self._encoder_enabled:
                    self._encoder_report_seq = (self._encoder_report_seq + 1) & 0xffffffff
                    self._emit("rx", encode_frame(ENCODER_TOTALS, self.session, self._encoder_report_seq,
                        ENCODER_TOTALS_LAYOUT.pack(1, 1, self._encoder_epoch, self.tick_ms,
                            self._encoder_sample_seq, *self._encoder_totals, 10, self._encoder_delayed)),
                        monotonic=min(now, self._last_time))
        while self._delayed and self._delayed[0][0] <= now:
            _, payload = self._delayed.popleft()
            self._emit("rx", payload)

    def request_send(self, payload, request_id=""):
        self.advance()
        if not self._running or not isinstance(payload, (bytes, bytearray, memoryview)):
            return False
        payload = bytes(payload)
        if not HEADER.size + 2 <= len(payload) <= self.MAX_PAYLOAD_BYTES:
            return False
        magic, version, kind, length, session, seq = HEADER.unpack_from(payload)
        if (magic != b"\xa5\x5a" or version != 1 or length > 64
                or len(payload) != HEADER.size + length + 2
                or crc16(payload[2:-2]) != struct.unpack_from("<H", payload, len(payload) - 2)[0]):
            return False
        self.sent.append((payload, request_id))
        self._emit("tx", payload, request_id=request_id)
        body = payload[HEADER.size:-2]
        if kind == STOP and not body:
            self._stop(1)
            if session == self.session and seq > self.seq:
                self.seq = seq
            self._ack(kind, 0, session, seq)
            return True
        if kind == HELLO:
            if not body and session and seq == 1:
                self._stop(0)
            if body or not session or seq != 1 or session in self._sessions or len(self._sessions) >= 128:
                self._ack(kind, 1, session, seq)
                return True
            self._sessions.add(session)
            self.speed.reset_session()
            self.yaw.reset_session()
            self.session, self.seq = session, seq
            self._stop(0)
            self._profile_enabled = False
            self._encoder_enabled = False
            self._encoder_report_seq = 0
            self._report_seq = 0
            self._clear_profile()
            self._emit("rx", encode_frame(CAPS, session, seq, CAPS_LAYOUT.pack(15, 6000, PROJECT_SPEED_LIMIT_MM_S, 1000, 100, 20, 1)))
            self._ack(kind, 0, session, seq)
            return True
        if session != self.session:
            self._ack(kind, 2, session, seq)
            return True
        if seq <= self.seq:
            self._ack(kind, 3, session, seq)
            return True
        self.seq = seq
        status = self._execute(kind, body)
        self._ack(kind, status, session, seq)
        return True

    def _execute(self, kind, body):
        if self._fault_matches("reject", kind):
            return 5
        if kind in range(19,28) or (kind==ARM and body==b'\x05'):
            return self.yaw.execute(kind,body)
        if kind in range(11,19) or (kind==ARM and body in (b'\x03',b'\x04')):
            if self.yaw.busy:return 4
            result=self.speed.execute(kind,body)
            if result==0 and kind in (14,17):self.yaw.stage=0;self.yaw.samples=[];self.yaw.run=0
            return result
        if kind == ARM:
            if self.speed.busy or self.yaw.busy:
                return 4
            if len(body) != 1 or body[0] not in (1, 2):
                return 1
            if not self.local_enable:
                return 5
            if self.armed:
                return 4
            if body[0] == 2 and (not any(self.parameters[:2]) or not any(self.parameters[2:])):
                return 7
            self._stop(0)
            self._clear_profile()
            self.mode, self.armed = body[0], True
            self._idle_deadline = (self.tick_ms + 5000) & 0xFFFFFFFF
            return 0
        if kind in (SET_PWM, SET_SPEED):
            if len(body) != 10:
                return 1
            left, right, duration, deadline = struct.unpack("<hhHI", body)
            limit = 6000 if kind == SET_PWM else PROJECT_SPEED_LIMIT_MM_S
            if not 1 <= duration <= 1000 or abs(left) > limit or abs(right) > limit:
                return 1
            if not self.local_enable:
                return 5
            if not self.armed or self._deadline is not None or self.mode != (1 if kind == SET_PWM else 2):
                return 4
            remaining = (deadline - self.tick_ms) & 0xFFFFFFFF
            if not 0 < remaining <= 1000:
                return 8
            self.pwm_left, self.pwm_right = left, right
            self._deadline = (self.tick_ms + min(duration, remaining)) & 0xFFFFFFFF
            self._idle_deadline = None
            return 0
        if kind == GET_ENCODER_CAPS:
            if self.faults.get("encoder_unsupported"):
                return 9
            if body:
                return 1
            if self.armed or self._deadline is not None:
                return 4
            self._encoder_enabled = True
            self._emit("rx", encode_frame(ENCODER_CAPS, self.session, self.seq,
                ENCODER_CAPS_LAYOUT.pack(1, 64, 100, 20, 0, 1560, 66000, 160000)))
            return 0
        if kind == GET_PROFILE_CAPS:
            if self.faults.get("profile_unsupported"):
                return 9
            if body:
                return 1
            if self.armed or self._deadline is not None:
                return 4
            self._profile_enabled = True
            self._emit("rx", encode_frame(PROFILE_CAPS, self.session, self.seq,
                PROFILE_CAPS_LAYOUT.pack(3, 1, 2, 6000, 1000, 100, 20)))
            return 0
        if kind == SET_PWM_PROFILE:
            if not self._profile_enabled or self.faults.get("profile_unsupported"):
                return 9
            if len(body) != PROFILE_COMMAND_LAYOUT.size:
                return 1
            version, count, reserved, left, right, left2, right2, first_ms, duration, deadline = PROFILE_COMMAND_LAYOUT.unpack(body)
            if (version != 1 or count != 2 or reserved or not 1 <= first_ms < duration <= 1000
                    or any(abs(value) > 6000 for value in (left, right, left2, right2))
                    or left * left2 < 0 or right * right2 < 0):
                return 1
            if not self.local_enable:
                return 5
            if not self.armed or self.mode != 1 or self._deadline is not None:
                return 4
            remaining = (deadline - self.tick_ms) & 0xFFFFFFFF
            if not 0 < remaining <= 1000:
                return 8
            self.pwm_left, self.pwm_right = left, right
            self._profile_second = (left2, right2)
            self.profile_seq, self.profile_start_tick = self.seq, self.tick_ms
            self._profile_switch_tick = (self.tick_ms + first_ms) & 0xFFFFFFFF
            self.segment1_actual_tick = 0
            self.segment_index, self.profile_status = 0, 1
            self._deadline = (self.tick_ms + min(duration, remaining)) & 0xFFFFFFFF
            self._idle_deadline = None
            return 0
        if kind == STOP:
            return 1 if body else 0
        if kind == GET_PARAMS:
            if body:
                return 1
            self._params()
            return 0
        if kind == SET_PARAMS:
            if len(body) != 16:
                return 1
            values = struct.unpack("<IIII", body)
            if any(value > 2000000 for value in values):
                return 1
            if self.armed:
                return 6
            self.parameters = list(values)
            self.parameter_revision += 1
            self._params()
            return 0
        return 9

    def _fault_matches(self, name, kind):
        value = self.faults.get(name)
        return bool(value is True or (not isinstance(value, bool) and value == kind)
                    or isinstance(value, (list, tuple, set)) and kind in value)

    def _ack(self, kind, status, session, seq):
        if self._fault_matches("missingack", kind):
            return
        payload = encode_frame(ACK, session, seq, bytes((kind, status)))
        delay = float(self.faults.get("ack_delay_ms", 0)) / 1000
        if delay > 0:
            if len(self._delayed) == self._delayed.maxlen:
                self._overflow += 1
            self._delayed.append((self.clock() + delay, payload))
        else:
            self._emit("rx", payload)

    def _params(self):
        self._emit("rx", encode_frame(PARAMS, self.session, self.seq,
                   PARAMS_LAYOUT.pack(self.parameter_revision, *self.parameters)))

    def _state(self, now):
        left = 0 if self.faults.get("start_failure") else int(self.pwm_left / 10)
        right = 0 if self.faults.get("start_failure") else int(self.pwm_right / 10)
        if self.yaw.negotiated and self.mode==5:
            left,right=(int(x*1000) for x in self.yaw.measured)
        elif self.speed.negotiated and self.mode not in (1,2):
            left,right=(int(x*1000) for x in self.speed.measured)
        if self.faults.get("residual_feedback") and not self.armed:
            left = right = 17  # Test-only nonzero feedback prevents quiet gate.
        body = STATE_LAYOUT.pack(self.tick_ms, self.seq, int(left / 17), int(right / 17),
                                left, right, self.pwm_left, self.pwm_right, 12000, 90,
                                self.mode, self.armed, self.reason, self.local_enable,
                                self.rx_dropped, self.tx_dropped, self.parameter_revision, 0)
        self._report_seq = (self._report_seq + 1) & 0xFFFFFFFF
        if self._profile_enabled:
            body += PROFILE_STATE_LAYOUT.pack(self.profile_seq, self.profile_start_tick,
                self.segment1_actual_tick, self.segment_index, self.profile_status, 0)
        self._emit("rx", encode_frame(STATE_PROFILE if self._profile_enabled else STATE,
            self.session, self._report_seq, body), monotonic=now)

    def _reached(self, deadline):
        return ((self.tick_ms - deadline) & 0xFFFFFFFF) < 0x80000000

    def _stop(self, reason):
        if hasattr(self,'speed'):
            self.speed.stop(reason)
        if hasattr(self, "yaw"):
            self.yaw.stop(reason)
        if self.profile_status == 1:
            self.profile_status = 2
        self._profile_second = (0, 0)
        self.armed = False
        self.mode, self.reason = 0, reason
        self.pwm_left = self.pwm_right = 0
        self._deadline = self._idle_deadline = None

    def _clear_profile(self):
        self.profile_seq = self.profile_start_tick = self.segment1_actual_tick = 0
        self.segment_index, self.profile_status = 255, 0
        self._profile_switch_tick = 0
        self._profile_second = (0, 0)

    def _emit(self, kind, payload=b"", *, message="", request_id="", monotonic=None):
        if len(self._events) == self._events.maxlen:
            self._overflow += 1
        self._events.append(IoEvent(kind=kind, payload=payload, message=message,
                           monotonic=self.clock() if monotonic is None else monotonic,
                           utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                           connection_id=self.connection_id, request_id=request_id))


def create_project_fake_worker(config: ConnectionConfig):
    """Strict demo factory: real port/address choices cannot open hardware."""
    return ProjectFakeWorker(config)
