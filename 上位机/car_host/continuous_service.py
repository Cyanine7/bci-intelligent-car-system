"""Leased continuous control. Only this service schedules keyboard targets."""
from collections import deque
from dataclasses import asdict
import math
import time
import json
from datetime import datetime, timezone
from PySide6.QtCore import QTimer
from .device import DeviceResult
from .continuous_models import (COMMANDS, CAPS, STATE, KeyboardState, DiscreteDrivingState,
                                DrivingEvent, UPDATE_SECONDS,
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
        self.mode = 'discrete'
        self.discrete = DiscreteDrivingState()
        self.legacy = KeyboardState()
        self._key_batch = []
        self._input_sequence = 0
        self._seen_events = {}  # 每来源单调序号，最多32个来源；不淘汰后重新接受旧事件。
        if not hasattr(self, 'events'):
            self.events = deque(maxlen=128)
            path = self.host.data_directory / 'continuous_events.jsonl'
            try:
                if path.stat().st_size <= 1024*1024:
                    for line in path.read_text(encoding='utf-8').splitlines()[-128:]:
                        entry = json.loads(line)
                        if isinstance(entry, dict) and entry.get('kind') == 'continuous_stop' and isinstance(entry.get('reason'), str):
                            self.events.append(entry)
            except (OSError, ValueError, UnicodeError):
                pass
        self.last_stop = self.events[-1] if self.events else None
        self.communication = '等待连接'
        self.engaged = False
        self.arm_request = self.target_request = None
        self.caps_request = None
        self.arm_confirmed = False
        self.arm_seq = 0
        self.frozen_revision = None
        self.frozen_config = None
        self.due = 0.
        self.status = '请查询连续控制能力，读取内外环参数并校准零偏'
        self._drop_baseline = None
        self._last_tick = None
        self._last_report_seq = None
        self._backlog_since = None
        self.drop_delta = (0, 0)

    @property
    def keys(self):
        return self.discrete if self.mode == 'discrete' else self.legacy

    @property
    def state_fresh(self):
        h = self.host
        return bool(h.connected and h.latest and h.latest.device_session == h.adapter.session
                    and 0 <= time.monotonic() - h.latest.received_monotonic < (1. if self.mode == 'discrete' else .5)
                    and h.latest.device_last_seq >= h._state_required_seq)

    def set_mode(self, mode):
        if mode not in ('discrete', 'legacy'):
            raise ValueError('未知驾驶模式')
        if mode != self.mode:
            self.cancel('切换驾驶模式，已停止；请重新使能')
            self.mode = mode

    def _record_stop(self, reason, code):
        h = self.host; now = time.monotonic(); sample = h.latest
        request = self.target_request or self.arm_request
        metadata = h.command_metadata.get(request, {})
        self.last_stop = dict(kind='continuous_stop', utc=datetime.now(timezone.utc).isoformat(),
            source='continuous_service', code=code, reason=reason, mode=self.mode,
            session=getattr(h.adapter, 'session', None), device_mode=getattr(sample, 'control_mode', None),
            seq=metadata.get('seq'), request_age_ms=(now-metadata['queued_monotonic'])*1000 if metadata else None,
            state_age_ms=(now-sample.received_monotonic)*1000 if sample else None,
            continuous_age_ms=(now-self.latest['received'])*1000 if self.latest else None,
            backlog_ms=(now-self._backlog_since)*1000 if self._backlog_since is not None else 0,
            queue=getattr(h, '_last_io_batch', {}).get('queue', {}),
            hard_overflow=code == 'overflow', drop_delta=self.drop_delta,
            mcu_stop_reason=getattr(sample, 'stop_reason', None))
        self.events.append(self.last_stop)

    def _save_stop(self, entry):
        h = self.host
        h.recorder.record_trace(entry)
        h.add_log(entry['reason'], 'WARNING')
        # 独立于完整录制，最多保留128条停止诊断。
        path = h.data_directory / 'continuous_events.jsonl'
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in self.events), encoding='utf-8')
        except OSError as exc:
            h.add_log(f'停止诊断保存失败：{exc}', 'WARNING')

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
        return bool(self.engaged and self.arm_confirmed and self.state_fresh
                    and h.latest.armed and h.latest.control_mode == 6)

    def query_capabilities(self):
        result = self.host.send_command('continuous_caps')
        if result.success:
            self.caps_request = result.request_id
        return result

    def arm(self):
        if not self.ready:
            return DeviceResult(False, 'rejected', '需要连续CAPS、确认RAM、零偏校准、新鲜STATE及失能状态')
        self.discrete.clear();self.legacy.clear();self._key_batch.clear()
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
            self._backlog_since = None
            self.latest = None
            s = self.host.latest
            self._drop_baseline = (s.rx_dropped, s.tx_dropped)
            self.drop_delta = (0, 0)
            self.host.recorder.record_trace(dict(kind='continuous_arm_context',
                connection_id=self.host.connection_id, session=self.host.adapter.session,
                revision=self.frozen_revision, config=asdict(self.host.yaw_control.config),
                target_hz=2, lease_ms=LEASE_MS if self.mode == 'discrete' else 1000, input_mode=self.mode))
        return result

    def cancel(self, reason='STOP', send_stop=True, code='stop'):
        was_engaged = self.engaged
        if was_engaged:
            self._record_stop(reason, code)
        self.engaged = False
        self.timer.stop()
        self.discrete.clear();self.legacy.clear();self._key_batch.clear()
        owned = {self.arm_request, self.target_request} - {None}
        for seq, pending in tuple(self.host._project_pending.items()):
            if pending['request_id'] in owned:
                self.host._project_pending.pop(seq, None)
                self.host._remember_result(DeviceResult(False, 'unknown',
                    '连续控制已取消；此前请求结果未知，不重发', pending['request_id']))
        self.target_request = self.arm_request = None
        self.arm_confirmed = False
        if was_engaged or reason == 'STOP':
            self.status = reason
        if was_engaged and send_stop and self.host.connected:
            result = self.host.send_command('stop')
            self.status = reason
        else:
            result = DeviceResult(True, 'stopped', reason)
        if was_engaged:
            if send_stop:
                self._save_stop(self.last_stop)
            else:
                # 顶部STOP由调用者排队；写盘延后到当前事件结束。
                QTimer.singleShot(0, lambda entry=self.last_stop:self._save_stop(entry))
        return result

    def press(self, key):
        if self.running:
            if self.mode == 'discrete':
                if key in self.keys.held or key not in ('up','down','left','right','w','s','a','d'):
                    return
                self.keys.held.add(key)
                self._input_sequence += 1
                self._key_batch.append(DrivingEvent('keyboard', self._input_sequence, time.monotonic(), key))
                QTimer.singleShot(0, self.flush_input)
                return
            before = self.keys.target()
            self.keys.press(key, time.monotonic())
            after = self.keys.target()
            if any(a and not b for a, b in zip(before, after)):
                self._send_target(after, immediate=True)

    def release(self, key):
        if self.mode == 'discrete':
            self.flush_input()
            self.keys.held.discard(key)
            return
        before = self.keys.target()
        self.keys.release(key, time.monotonic())
        after = self.keys.target()
        # A released axis bypasses both the 2 Hz cadence and an ordinary in-flight target.
        if self.running and any(a and not b for a, b in zip(before, after)):
            self._send_target(after, immediate=True)

    def flush_input(self):
        batch, self._key_batch = self._key_batch, []
        self.apply_events(batch)

    def apply_events(self, events):
        """同一批次先归并；适配器/未来识别器共享入口，不接触传输。"""
        if self.mode != 'discrete' or not self.running:
            return
        commands = []
        now = time.monotonic()
        for event in events:
            if (not isinstance(event.source, str) or not 0 < len(event.source) <= 64
                    or type(event.sequence) is not int or event.sequence < 0
                    or type(event.generated) not in (int,float) or not math.isfinite(event.generated)
                    or not 0 <= now-event.generated <= 1.
                    or event.command not in ('up','down','left','right','w','s','a','d')
                    or event.sequence <= self._seen_events.get(event.source, -1)
                    or event.source not in self._seen_events and len(self._seen_events) >= 32):
                continue
            self._seen_events[event.source] = event.sequence
            commands.append(event.command)
        self.keys.apply(commands)
        self.poll()

    def prepare(self, command, values):
        h = self.host
        fresh = self.state_fresh if command == 'continuous_target' else h.project_state_fresh
        if not fresh or h.io_backlog_exceeded:
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
                          flags=0 if self.mode == 'discrete' else 1,
                          deadline_tick=(h.latest.device_time_ms + elapsed + (LEASE_MS if self.mode == 'discrete' else 1000) - 10) & 0xffffffff)
        return values

    def handle(self, event, pending):
        h = self.host
        command = pending['command']
        if command != 'continuous_caps':
            age = time.monotonic() - h.command_metadata[pending['request_id']]['queued_monotonic']
            if age >= (ACK_SECONDS if self.mode == 'discrete' else .2) - 1e-9:
                self.cancel('连续请求超过确认期限，已停止', code='ack_timeout')
                return
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
                    self.cancel('连续控制请求被拒绝：'+message, code='rejected')
                return
            pending['ack'] = True
        elif event.kind == 0x99 and command == 'continuous_caps':
            version, flags, hz, report, lease, speed, omega, accel, angular = CAPS.unpack(event.values[0])
            if version == 1:
                self.status = '需更新连续驾驶固件（需要v2）'
                h._project_pending.pop(event.seq, None)
                h._remember_result(DeviceResult(False, 'rejected', self.status, pending['request_id']))
                return
            if (version != 2 or flags not in (0, 7) or hz != 100 or report != 20 or lease != LEASE_MS
                    or not math.isclose(speed, 1.18, abs_tol=1e-6)
                    or not math.isfinite(omega) or not 1. <= omega <= OMEGA_LIMIT
                    or (accel, angular) != (.5, 2.)):
                raise ValueError('连续控制CAPS不符合协议')
            pending['value'] = dict(ready=flags == 7, speed_limit=speed, omega_limit=omega, version=version)
        else:
            return
        if pending.get('ack') and (command != 'continuous_caps' or 'value' in pending):
            if command == 'continuous_caps':
                self.caps = pending['value']
                h.adapter.continuous_negotiated = True
                for keys in (self.discrete, self.legacy):
                    keys.speed_limit = self.caps['speed_limit']
                    keys.omega_limit = self.caps['omega_limit']
                    keys.base_v = min(keys.base_v, keys.speed_limit)
                    keys.base_omega = min(keys.base_omega, keys.omega_limit)
                self.legacy.clamp_multiplier()
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
        if self._last_report_seq is not None and not 0 < ((event.seq-self._last_report_seq)&0xffffffff) < 0x80000000:
            return
        self._last_tick = v[0]
        self._last_report_seq = event.seq
        names = ('tick', 'accepted_seq', 'revision', 'deadline', 'requested_v', 'requested_omega',
                 'v', 'omega', 'measured_omega', 'left_ref', 'right_ref', 'pwm_left', 'pwm_right', 'gyro_age', 'flags')
        self.latest = dict(zip(names, v), received=event.received_monotonic if event.received_monotonic is not None else time.monotonic())
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
        self.due = time.monotonic() + UPDATE_SECONDS

    def poll(self):
        caps_result = self.host.command_results.get(self.caps_request)
        if caps_result and caps_result.status == 'unknown':
            self.status = '连续能力查询未确认；不自动重发'
            self.caps_request = None
        if not self.engaged:
            return
        h = self.host; now = time.monotonic()
        tolerant = self.mode == 'discrete'
        state_limit = 1. if tolerant else .5
        telemetry_limit = 1. if tolerant else .2
        ack_limit = ACK_SECONDS if tolerant else .2
        if not h.connected:
            self.cancel('连接已断开：'+h.connection_status, send_stop=False, code='disconnected');return
        if not h.latest or h.latest.device_session != h.adapter.session:
            self.cancel('设备会话变化或STATE无效', code='session');return
        state_age = now-h.latest.received_monotonic
        if not 0 <= state_age < state_limit:
            self.cancel(f'STATE年龄{state_age*1000:.0f}ms，达到{state_limit*1000:.0f}ms期限；连接仍在', code='state_timeout');return
        backlogged = h.io_backlog_exceeded or (tolerant and getattr(h, '_last_io_batch', {}).get('budget_yielded', False))
        if backlogged:
            if self._backlog_since is None:self._backlog_since = now
            if not tolerant or now-self._backlog_since >= .5-1e-9:
                self.cancel('通信积压持续500ms，已停止；连接仍在' if tolerant else '通信积压，已停止', code='backlog');return
        else:
            self._backlog_since = None
        if not h.latest.local_enable:
            self.cancel('设备本地禁止使能', code='local_disabled');return
        drops = (h.latest.rx_dropped, h.latest.tx_dropped)
        if self._drop_baseline is not None:
            delta = tuple((a-b)&0xffffffff for a,b in zip(drops,self._drop_baseline))
            if any(delta):
                self.drop_delta = tuple(a+b for a,b in zip(self.drop_delta,delta))
                h.add_log(f'连续驾驶设备丢弃计数增加 RX/TX={delta}', 'WARNING')
                if not tolerant:
                    self.cancel('设备通信丢失', code='device_drop');return
        self._drop_baseline = drops
        for request in (self.arm_request, self.target_request):
            if not request:continue
            result = h.command_results.get(request)
            if result and result.status in ('unknown', 'rejected'):
                self.cancel('连续请求结果未知或被拒绝，已停止', code='request_unknown');return
            if result and result.status == 'queued':
                age = now-h.command_metadata[request]['queued_monotonic']
                if age >= ack_limit-1e-9:
                    self.cancel(f'连续请求{age*1000:.0f}ms未确认，达到{ack_limit*1000:.0f}ms期限；连接仍在', code='ack_timeout');return
        if self.arm_confirmed and (not h.latest.armed or h.latest.control_mode != 6):
            if h.latest.device_last_seq >= self.arm_seq:
                self.cancel(f'设备已退出连续控制，stop_reason={h.latest.stop_reason}', code='device_stopped');return
        if (h.yaw_control.revision != self.frozen_revision or h.yaw_control.config is None
                or h.yaw_control.config.pack() != self.frozen_config):
            self.cancel('本次冻结控制参数已变化', code='revision');return
        if not self.running:return
        telemetry_age = now-self.latest['received'] if self.latest else None
        # ARM之后允许第一条连续遥测到达，仍受ACK/STATE及设备空闲期限约束。
        if self.latest is None:
            arm_age = now-h.command_metadata[self.arm_request]['queued_monotonic']
            if arm_age >= telemetry_limit:
                self.cancel('等待首条连续遥测超时', code='telemetry_timeout')
            return
        if not 0 <= telemetry_age < telemetry_limit:
            self.cancel(f'连续遥测年龄{telemetry_age*1000:.0f}ms，达到{telemetry_limit*1000:.0f}ms期限；连接仍在', code='telemetry_timeout');return
        if self.latest['revision'] != self.frozen_revision and self.latest['flags'] & 1:
            self.cancel('MCU冻结参数版本变化', code='revision');return
        if not self.latest['flags'] & 2 or self.latest['gyro_age'] > 60:
            self.cancel('设备IMU无效或超过60ms', code='imu');return
        warnings = []
        for request in (self.arm_request,self.target_request):
            result=h.command_results.get(request)
            if result and result.status=='queued':
                age=now-h.command_metadata[request]['queued_monotonic']
                if age>=.2:warnings.append(f'ACK等待{age*1000:.0f}ms')
        if telemetry_age>=.3:warnings.append(f'连续遥测{telemetry_age*1000:.0f}ms')
        if state_age>=.5:warnings.append(f'STATE {state_age*1000:.0f}ms')
        if any(self.drop_delta):warnings.append(f'累计丢弃增量{self.drop_delta}')
        if self._backlog_since is not None:warnings.append('正在排空通信积压')
        self.communication = '连接仍在 · '+('；'.join(warnings) if warnings else '连续驾驶正常')
        self.keys.poll(now)
        pending = h.command_results.get(self.target_request)
        if (not backlogged and now+1e-9 >= self.due
                and (not pending or pending.status == 'accepted')):
            self._send_target(self.keys.target())
