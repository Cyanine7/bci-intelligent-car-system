"""Leased continuous control. Only this service schedules keyboard targets."""
from collections import deque
from dataclasses import asdict
import math
import time
from PySide6.QtCore import QTimer
from .device import DeviceResult
from .continuous_models import (COMMANDS, CAPS, STATE, KeyboardState, UPDATE_SECONDS,
                                LEASE_MS, ACK_SECONDS, OMEGA_LIMIT)


class ContinuousService:
    def __init__(self, host):
        self.host = host
        self.reset()
        self.timer = QTimer(host)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.poll)

    def reset(self):
        if hasattr(self, 'timer'):
            self.timer.stop()
        self.caps = None
        self.latest = None
        self.live = deque(maxlen=600)
        self.keys = KeyboardState()
        self.engaged = False
        self.arm_request = self.target_request = None
        self.caps_request = None
        self.arm_confirmed = False
        self.arm_seq = 0
        self.frozen_revision = None
        self.frozen_config = None
        self.due = 0.
        self.sent_target = (0., 0.)
        self.status = '请查询连续控制能力，读取内外环参数并校准零偏'
        self._drop_baseline = None
        self._last_tick = None

    @property
    def ready(self):
        h = self.host
        if not self.caps or not self.caps['ready']:
            return False
        return bool(h.project_state_fresh and h.latest.local_enable and not h.latest.armed
                    and not h.project_request_pending and not h.project_motion_active
                    and not h.io_backlog_exceeded and h.yaw_control.config is not None
                    and h.yaw_control.config.sign == 1 and h.yaw_control.imu_ready
                    and not h.yaw_control.result_fetching and not h.speed_control.download_running)

    @property
    def running(self):
        h = self.host
        return bool(self.engaged and self.arm_confirmed and h.project_state_fresh
                    and h.latest.armed and h.latest.control_mode == 6)

    def query_capabilities(self):
        result = self.host.send_command('continuous_caps')
        if result.success:
            self.caps_request = result.request_id
        return result

    def arm(self):
        if not self.ready:
            return DeviceResult(False, 'rejected', '需要连续CAPS、确认RAM、零偏校准、新鲜STATE及失能状态')
        self.keys.clear()
        self.frozen_revision = self.host.yaw_control.revision
        self.frozen_config = self.host.yaw_control.config.pack()
        result = self.host.send_command('continuous_arm')
        if result.success:
            self.engaged = True
            self.timer.start()
            self.arm_request = result.request_id
            self.arm_seq = self.host.command_metadata[result.request_id]['seq']
            self.arm_confirmed = False
            self.target_request = None
            self.due = 0.
            self.sent_target = (0., 0.)
            s = self.host.latest
            self._drop_baseline = (s.rx_dropped, s.tx_dropped)
            self.host.recorder.record_trace(dict(kind='continuous_arm_context',
                connection_id=self.host.connection_id, session=self.host.adapter.session,
                revision=self.frozen_revision, config=asdict(self.host.yaw_control.config),
                target_hz=2, lease_ms=LEASE_MS))
        return result

    def cancel(self, reason='STOP', send_stop=True):
        was_engaged = self.engaged
        self.engaged = False
        self.timer.stop()
        self.keys.clear()
        owned = {self.arm_request, self.target_request} - {None}
        for seq, pending in tuple(self.host._project_pending.items()):
            if pending['request_id'] in owned:
                self.host._project_pending.pop(seq, None)
                self.host._remember_result(DeviceResult(False, 'unknown',
                    '连续控制已取消；此前请求结果未知，不重发', pending['request_id']))
        self.target_request = self.arm_request = None
        self.arm_confirmed = False
        self.sent_target = (0., 0.)
        if was_engaged or reason == 'STOP':
            self.status = reason
        if was_engaged and send_stop and self.host.connected:
            result = self.host.send_command('stop')
            self.status = reason
            return result
        return DeviceResult(True, 'stopped', reason)

    def press(self, key):
        if self.running:
            before = self.keys.target()
            self.keys.press(key, time.monotonic())
            after = self.keys.target()
            if any(a and not b for a, b in zip(before, after)):
                self._send_target(after, immediate=True)

    def release(self, key):
        before = self.keys.target()
        self.keys.release(key, time.monotonic())
        after = self.keys.target()
        # A released axis bypasses both the 2 Hz cadence and an ordinary in-flight target.
        if self.running and any(a and not b for a, b in zip(before, after)):
            self._send_target(after, immediate=True)

    def prepare(self, command, values):
        h = self.host
        if not h.project_state_fresh or h.io_backlog_exceeded:
            raise ValueError('需要健康通信和新鲜STATE')
        if command == 'continuous_caps':
            if h.latest.armed or h.project_request_pending or h.project_motion_active:
                raise ValueError('能力查询需要失能且无在途请求')
            self.caps = None
            h.adapter.continuous_negotiated = False
        elif not self.caps or not self.caps['ready']:
            raise ValueError('尚未确认连续控制能力')
        elif command == 'continuous_arm':
            if not self.ready:
                raise ValueError('连续控制尚未就绪')
        elif command == 'continuous_target':
            if not self.running:
                raise ValueError('未使能连续控制')
            if abs(values['v']) > self.caps['speed_limit'] + 1e-7 or abs(values['omega']) > self.caps['omega_limit'] + 1e-7:
                raise ValueError('连续目标超过实际CAPS能力')
            if h.yaw_control.revision != self.frozen_revision or h.yaw_control.config.pack() != self.frozen_config:
                raise ValueError('控制参数已变化')
            elapsed = max(0, int((time.monotonic() - h.latest.received_monotonic) * 100)) * 10
            # Leave one control tick for clock-grid/float rounding, never extend the lease.
            values.update(revision=self.frozen_revision,
                          deadline_tick=(h.latest.device_time_ms + elapsed + LEASE_MS - 10) & 0xffffffff)
        return values

    def handle(self, event, pending):
        h = self.host
        command = pending['command']
        if event.kind == 0x82:
            actual, status = event.values
            if actual != COMMANDS[command]:
                return
            if status:
                h._project_pending.pop(event.seq, None)
                from .project_protocol import ACK_MESSAGES
                message = ACK_MESSAGES[status] if status < len(ACK_MESSAGES) else '未知拒绝状态'
                self.status = message
                h._remember_result(DeviceResult(False, 'rejected', message, pending['request_id']))
                if command != 'continuous_caps':
                    self.cancel('连续控制请求被拒绝')
                return
            pending['ack'] = True
        elif event.kind == 0x99 and command == 'continuous_caps':
            version, flags, hz, report, lease, speed, omega, accel, angular = CAPS.unpack(event.values[0])
            if (version != 1 or flags not in (0, 3) or hz != 100 or report != 20 or lease != LEASE_MS
                    or not math.isclose(speed, 1.18, abs_tol=1e-6)
                    or not math.isfinite(omega) or not 1. <= omega <= OMEGA_LIMIT
                    or (accel, angular) != (.5, 2.)):
                raise ValueError('连续控制CAPS不符合协议')
            pending['value'] = dict(ready=flags == 3, speed_limit=speed, omega_limit=omega)
        else:
            return
        if pending.get('ack') and (command != 'continuous_caps' or 'value' in pending):
            if command == 'continuous_caps':
                self.caps = pending['value']
                h.adapter.continuous_negotiated = True
                self.keys.speed_limit = self.caps['speed_limit']
                self.keys.omega_limit = self.caps['omega_limit']
                self.keys.clamp_multiplier()
            if command == 'continuous_arm' and self.engaged:
                self.arm_confirmed = True
            h._project_pending.pop(event.seq, None)
            h._remember_result(DeviceResult(True, 'accepted', 'MCU已接受', pending['request_id']))
            if command == 'continuous_caps' or self.engaged:
                self.status = '连续控制能力已确认' if command == 'continuous_caps' else '连续控制已接受'

    def diagnostic(self, event):
        if not self.caps:
            return
        v = STATE.unpack(event.values[0])
        if (v[1] > self.host.adapter.seq or v[-1] & ~31 or not all(math.isfinite(x) for x in v[4:11])
                or abs(v[4]) > 1.180001 or abs(v[5]) > self.caps['omega_limit'] + 1e-6):
            raise ValueError('连续控制遥测无效')
        if self._last_tick is not None and not 0 < ((v[0] - self._last_tick) & 0xffffffff) < 0x80000000:
            return
        self._last_tick = v[0]
        names = ('tick', 'accepted_seq', 'revision', 'deadline', 'requested_v', 'requested_omega',
                 'v', 'omega', 'measured_omega', 'left_ref', 'right_ref', 'pwm_left', 'pwm_right', 'gyro_age', 'flags')
        self.latest = dict(zip(names, v), received=time.monotonic())
        self.live.append(self.latest)
        self.host.recorder.record_trace(dict(kind='continuous_state', connection_id=self.host.connection_id,
                                             session=self.host.adapter.session, **self.latest))

    def _send_target(self, target, immediate=False):
        if immediate and self.target_request:
            # Superseded requests cannot schedule later movement or fail a newer zero target.
            for seq, p in tuple(self.host._project_pending.items()):
                if p['request_id'] == self.target_request:
                    self.host._project_pending.pop(seq)
                    self.host._remember_result(DeviceResult(False, 'superseded', '已由松键归零取代', self.target_request))
        result = self.host.send_command('continuous_target', dict(v=target[0], omega=target[1]))
        if not result.success:
            self.cancel(result.message)
            return
        self.target_request = result.request_id
        self.sent_target = target
        self.due = time.monotonic() + UPDATE_SECONDS

    def poll(self):
        caps_result = self.host.command_results.get(self.caps_request)
        if caps_result and caps_result.status == 'unknown':
            self.status = '连续能力查询未确认；不自动重发'
            self.caps_request = None
        if not self.engaged:
            return
        h = self.host
        fresh = bool(h.connected and h.latest and h.latest.device_session == h.adapter.session
                     and 0 <= time.monotonic() - h.latest.received_monotonic < .5)
        if not fresh or h.io_backlog_exceeded:
            self.cancel('连接中断、STATE过期或通信积压');return
        if (not h.latest.local_enable or self._drop_baseline != (h.latest.rx_dropped, h.latest.tx_dropped)):
            self.cancel('本地禁止或设备通信丢失');return
        for request in (self.arm_request, self.target_request):
            if not request:
                continue
            r = h.command_results.get(request)
            if r and r.status in ('unknown', 'rejected'):
                self.cancel('请求未确认，已停止');return
            if r and r.status == 'queued' and time.monotonic() - h.command_metadata[request]['queued_monotonic'] >= ACK_SECONDS - 1e-9:
                self.cancel('连续请求200ms未确认，已停止');return
        if self.arm_confirmed and (not h.latest.armed or h.latest.control_mode != 6):
            # ARM ACK can precede its covering STATE.
            if h.latest.device_last_seq >= self.arm_seq:
                self.cancel('设备已退出连续控制');return
        if not self.running:
            return
        now = time.monotonic()
        if not self.latest or now - self.latest['received'] > .2 or not self.latest['flags'] & 2:
            self.cancel('连续遥测或IMU已过期');return
        self.keys.poll(now)
        pending = h.command_results.get(self.target_request)
        if now + 1e-9 >= self.due and (not pending or pending.status == 'accepted'):
            self._send_target(self.keys.target(self.caps['speed_limit'], self.caps['omega_limit']))
