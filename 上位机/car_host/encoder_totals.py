"""Manual-turn calibration from full MCU totals; no ARM/PWM or hardware access."""

from dataclasses import asdict
import time

from .device import DeviceResult


def signed_delta64(end, start):
    """Two's-complement modulo delta, including the signed 64-bit boundary."""
    return ((end - start + (1 << 63)) % (1 << 64)) - (1 << 63)


def snapshot_dict(snapshot):
    return asdict(snapshot) | {"raw_frame": snapshot.raw_frame.hex()}


class EncoderTotalsService:
    def __init__(self, host):
        self.host = host
        self.baseline = None
        self.result = None
        self.reason = "尚未开始手动计数"
        self._recording_directory = None
        self._owns_recording = False
        self._drops = None
        self._observed = None
        self._quiet_tick = None

    def invalidate(self, reason):
        self.baseline = None
        self._recording_directory = None
        self.reason = reason
        self._observed = self._quiet_tick = None

    def observe(self, snapshot):
        previous = self._observed
        identity = (snapshot.connection_id, snapshot.device_session, snapshot.epoch_tick)
        if previous is None or identity != (previous.connection_id, previous.device_session, previous.epoch_tick) or (
                snapshot.total_left, snapshot.total_right) != (previous.total_left, previous.total_right):
            self._quiet_tick = snapshot.device_time_ms
        self._observed = snapshot

    @property
    def quiet(self):
        s = self.host.latest_encoder_totals
        return bool(s and self._quiet_tick is not None
            and 200 <= ((s.device_time_ms-self._quiet_tick) & 0xffffffff) < 0x80000000)

    def query(self):
        return self.host.send_command("query_encoder_totals")

    @property
    def fresh(self):
        h, s = self.host, self.host.latest_encoder_totals
        return bool(h.connected and h.capabilities.encoder_totals and s and s.initialized
            and s.connection_id == h.connection_id and s.device_session == h.adapter.session
            and 0 <= time.monotonic() - s.received_monotonic < .5)

    def _ready(self):
        h = self.host
        if not self.fresh:
            raise ValueError("需要已确认能力及500ms内的全量计数")
        alignment = (h.latest.device_time_ms-h.latest_encoder_totals.device_time_ms) & 0xffffffff if h.latest else 0
        if 100 < alignment < 0xffffffff-100:
            raise ValueError("STATE与累计计数时间不一致，等待完整新快照")
        if not h.project_state_fresh or h.latest.armed or h.latest.pwm_left or h.latest.pwm_right:
            raise ValueError("手动计数需要新鲜STATE、失能及两轮PWM0")
        if h.control_owner or h.project_motion_active or h.project_request_pending:
            raise ValueError("其他功能正在控制或有在途请求")
        if h.io_backlog_exceeded:
            raise ValueError("接收积压，等待通信健康恢复")
        return h.latest_encoder_totals

    def begin(self):
        h = self.host
        try:
            current = self._ready()
            if h.latest.signed_left_speed_mps or h.latest.signed_right_speed_mps:
                raise ValueError("开始前请两轮停稳")
            if self.baseline is not None:
                raise ValueError("手动计数已开始，请先结束或取消")
            self._owns_recording = not h.recorder.is_running
            if self._owns_recording and not h.start_recording():
                raise ValueError("录制尚未准备好")
            if not h.recorder.is_active or h.recording_incomplete:
                raise ValueError("录制不完整，不能开始计数")
            marker = dict(kind="encoder_calibration_start", source="local_ui",
                          snapshot=snapshot_dict(current))
            # This is a fresh software baseline, preceding the operator's turns.
            if not h.recorder.record_trace(marker):
                raise ValueError("基线未能写入有界录制队列")
            self.baseline = current
            self.result = None
            self._recording_directory = h.recorder.directory
            self._drops = h._device_dropped
            self.reason = "已记录起点：先左轮5圈，再右轮5圈；停稳后点击结束"
            return DeviceResult(True, "baseline", self.reason)
        except ValueError as exc:
            self.reason = str(exc)
            return DeviceResult(False, "rejected", self.reason)

    def finish(self, left_turns=5, right_turns=5):
        h, start = self.host, self.baseline
        try:
            if start is None:
                raise ValueError("尚未记录起点")
            if any(type(n) is not int or not 1 <= n <= 100 for n in (left_turns, right_turns)):
                raise ValueError("左右实际圈数必须为1..100整数")
            end = self._ready()
            if (end.connection_id, end.device_session, end.epoch_tick) != (
                    start.connection_id, start.device_session, start.epoch_tick):
                raise ValueError("连接、会话或计数起点改变；请重新记录")
            if not 0 < ((end.sample_seq-start.sample_seq) & 0xffffffff) < 0x80000000:
                raise ValueError("尚无更新的控制周期")
            if end.delayed_samples != start.delayed_samples:
                raise ValueError("期间存在0ms或超过30ms采样周期，计数结果需复核")
            if not h.recorder.is_active or h.recorder.directory != self._recording_directory:
                raise ValueError("期间录制被停止或换目录")
            if h.recording_incomplete or h.recorder.dropped_total or h.recorder.failure_message or h._device_dropped != self._drops:
                raise ValueError("期间存在记录或设备丢失，不能发布计数结果")
            if h.latest.signed_left_speed_mps or h.latest.signed_right_speed_mps:
                raise ValueError("请两轮停稳后再结束")
            if not self.quiet:
                raise ValueError("等待累计值至少200ms不再变化后再结束")
            deltas = [signed_delta64(end.total_left, start.total_left),
                      signed_delta64(end.total_right, start.total_right)]
            if not all(deltas):
                raise ValueError("某轮净计数为0；核对实际转圈操作")
            result = dict(source="human_turn_count_with_mcu_totals", left_turns=left_turns,
                right_turns=right_turns, delta_left=deltas[0], delta_right=deltas[1],
                counts_per_left_turn=abs(deltas[0])/left_turns,
                counts_per_right_turn=abs(deltas[1])/right_turns,
                baseline=snapshot_dict(start), end=snapshot_dict(end),
                directory=str(self._recording_directory))
            if not h.recorder.record_trace(dict(kind="encoder_calibration_end", **result)):
                raise ValueError("结果未能写入录制队列")
            self.result = result
            self.reason = (f"左轮 {result['counts_per_left_turn']:g} 计数/圈；"
                           f"右轮 {result['counts_per_right_turn']:g} 计数/圈")
            self.baseline = None
            if self._owns_recording:
                h.stop_recording()
            return DeviceResult(True, "measured", self.reason, values=result)
        except ValueError as exc:
            self.reason = str(exc)
            return DeviceResult(False, "rejected", self.reason)

    def cancel(self):
        self.baseline = None
        self.reason = "已取消手动计数；不操作电机"
        if self._owns_recording and self.host.recorder.is_active:
            self.host.stop_recording()
        self._owns_recording = False
