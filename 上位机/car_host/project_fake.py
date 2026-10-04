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
    STATE_LAYOUT, STOP, crc16, encode_frame,
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
        self._last_drop_fault = self._last_session_fault = None

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
            steps = 1000
        for _ in range(steps):
            self._last_time += self.TICK_SECONDS
            self.tick_ms = (self.tick_ms + 10) & 0xFFFFFFFF
            self._tick_number += 1
            if self.armed and self._deadline is not None and self._reached(self._deadline):
                self._stop(2)
            elif self.armed and self._deadline is None and self._idle_deadline is not None and self._reached(self._idle_deadline):
                self._stop(5)
            if self.session and self._tick_number % self.STATE_TICKS == 0 and not self.faults.get("stale"):
                self._state(min(now, self._last_time))
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
        if kind == STOP:
            self._stop(1)
        if kind == HELLO:
            if body or not session or session in self._sessions or len(self._sessions) >= 128:
                self._ack(kind, 1, session, seq)
                return True
            self._sessions.add(session)
            self.session, self.seq = session, seq
            self._stop(0)
            self._emit("rx", encode_frame(CAPS, session, seq, CAPS_LAYOUT.pack(15, 6000, 300, 1000, 100, 20, 1)))
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
        if kind == ARM:
            if len(body) != 1 or body[0] not in (1, 2):
                return 1
            if not self.local_enable:
                return 5
            if self.armed:
                return 4
            if body[0] == 2 and not any(self.parameters):
                return 7
            self.mode, self.armed = body[0], True
            self._idle_deadline = (self.tick_ms + 5000) & 0xFFFFFFFF
            return 0
        if kind in (SET_PWM, SET_SPEED):
            if len(body) != 10:
                return 1
            left, right, duration, deadline = struct.unpack("<hhHI", body)
            limit = 6000 if kind == SET_PWM else 300
            if not 1 <= duration <= 1000 or abs(left) > limit or abs(right) > limit:
                return 1
            if not self.local_enable:
                return 5
            if not self.armed or self.mode != (1 if kind == SET_PWM else 2):
                return 4
            remaining = (deadline - self.tick_ms) & 0xFFFFFFFF
            if not 0 < remaining < 0x80000000:
                return 8
            self.pwm_left, self.pwm_right = left, right
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
        if self.faults.get("residual_feedback") and not self.armed:
            left = right = 17  # Test-only nonzero feedback prevents quiet gate.
        body = STATE_LAYOUT.pack(self.tick_ms, self.seq, int(left / 17), int(right / 17),
                                left, right, self.pwm_left, self.pwm_right, 12000, 90,
                                self.mode, self.armed, self.reason, self.local_enable,
                                self.rx_dropped, self.tx_dropped, self.parameter_revision, 0)
        self._emit("rx", encode_frame(STATE, self.session, 0, body), monotonic=now)

    def _reached(self, deadline):
        return ((self.tick_ms - deadline) & 0xFFFFFFFF) < 0x80000000

    def _stop(self, reason):
        self.armed = False
        self.mode, self.reason = 0, reason
        self.pwm_left = self.pwm_right = 0
        self._deadline = self._idle_deadline = None

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
