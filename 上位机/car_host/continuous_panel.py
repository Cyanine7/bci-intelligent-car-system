"""Continuous-control page. Keyboard events never access device transport."""
import time
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton,
                              QVBoxLayout, QHBoxLayout, QGridLayout, QDoubleSpinBox, QGroupBox)
from .project_protocol import STOP_REASONS
from .continuous_models import OMEGA_LIMIT

KEYS = {Qt.Key.Key_Up: 'up', Qt.Key.Key_Down: 'down', Qt.Key.Key_Left: 'left',
        Qt.Key.Key_Right: 'right', Qt.Key.Key_Z: 'z', Qt.Key.Key_X: 'x'}


class DrivingPad(QFrame):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setObjectName('drivingPad')
        self.setMinimumHeight(200)
        self.setStyleSheet('#drivingPad {background:#f1f5f9;border:2px solid #cbd5e1;border-radius:12px;} '
                           '#drivingPad:focus {border-color:#2563eb;}')
        layout = QVBoxLayout(self)
        self.hint = QLabel('点击使能后，在此区域使用键盘操纵')
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.hint)
        grid = QGridLayout();layout.addLayout(grid)
        self.labels = {}
        for key, text, row, col in (('up','↑ 前进',0,1),('left','← 左转',1,0),
                                    ('down','↓ 后退',1,1),('right','→ 右转',1,2),
                                    ('z','Z 加速',0,3),('x','X 减速',1,3)):
            label = QLabel(text);label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(46);grid.addWidget(label,row,col);self.labels[key] = label
        tip = QLabel('按住运动 · 松键归零 · ↓← 左后 / ↓→ 右后 · 空格 STOP')
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter);layout.addWidget(tip)

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            event.accept();return
        if event.key() == Qt.Key.Key_Space:
            self.panel.stop();event.accept();return
        if event.key() in KEYS:
            self.panel.service.press(KEYS[event.key()]);self.panel.refresh();event.accept();return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            event.accept();return
        if event.key() in KEYS:
            self.panel.service.release(KEYS[event.key()]);self.panel.refresh();event.accept();return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self.panel.service.cancel('操纵区失去焦点，已STOP；请重新使能')
        super().focusOutEvent(event)


class ContinuousPanel(QWidget):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host;self.service = host.continuous_control
        layout = QVBoxLayout(self)
        title = QLabel('连续控制');title.setStyleSheet('font-size:22px;font-weight:600;');layout.addWidget(title)
        self.status = QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        row = QHBoxLayout();layout.addLayout(row);self.buttons = {}
        for key, text, callback in (
                ('caps','查询连续能力',self.service.query_capabilities),
                ('yaw_caps','查询闭环能力',host.yaw_control.query_capabilities),
                ('read','读取控制参数',host.yaw_control.read_config),
                ('calibrate','采集零偏（2s）',host.yaw_control.calibrate),
                ('status','读取IMU状态',host.yaw_control.read_status)):
            button = QPushButton(text);row.addWidget(button);self.buttons[key] = button
            button.clicked.connect(lambda checked=False, fn=callback:self.invoke(fn))
        group = QGroupBox('速度设定');layout.addWidget(group);row = QHBoxLayout(group)
        self.base_v = QDoubleSpinBox();self.base_v.setDecimals(2);self.base_v.setRange(.01,1.18);self.base_v.setSingleStep(.01);self.base_v.setValue(.1)
        self.base_w = QDoubleSpinBox();self.base_w.setDecimals(2);self.base_w.setRange(.01,OMEGA_LIMIT);self.base_w.setSingleStep(.05);self.base_w.setValue(.3)
        row.addWidget(QLabel('基准线速度 (m/s)'));row.addWidget(self.base_v)
        row.addWidget(QLabel('基准角速度 (rad/s)'));row.addWidget(self.base_w)
        self.multiplier = QLabel();row.addWidget(self.multiplier);row.addStretch()
        self.base_v.valueChanged.connect(lambda x:setattr(self.service.keys,'base_v',x))
        self.base_w.valueChanged.connect(lambda x:setattr(self.service.keys,'base_omega',x))
        self.params = QLabel();self.params.setWordWrap(True);layout.addWidget(self.params)
        row = QHBoxLayout();layout.addLayout(row)
        self.buttons['arm'] = QPushButton('使能连续控制');row.addWidget(self.buttons['arm'])
        self.buttons['arm'].clicked.connect(self.arm)
        self.buttons['stop'] = QPushButton('STOP / 空格');row.addWidget(self.buttons['stop'])
        self.buttons['stop'].setStyleSheet('background:#b91c1c;color:white;font-weight:600;padding:8px;')
        self.buttons['stop'].clicked.connect(self.stop)
        self.pad = DrivingPad(self);layout.addWidget(self.pad,1)
        self.targets = QLabel();self.targets.setWordWrap(True);layout.addWidget(self.targets)
        self.feedback = QLabel();self.feedback.setWordWrap(True);layout.addWidget(self.feedback)
        note = QLabel('上位机目标 2Hz · MCU 闭环 100Hz · 松键/STOP 立即发送 · 失焦或切页 STOP')
        note.setStyleSheet('color:#475569;');note.setWordWrap(True);layout.addWidget(note)
        self.timer = QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.refresh);self.timer.start()
        self.window().installEventFilter(self)
        self.refresh()

    def invoke(self, fn):
        result = fn();self.service.status = result.message;self.refresh()

    def arm(self):
        self.invoke(self.service.arm)
        if self.service.engaged:
            self.pad.setFocus(Qt.FocusReason.OtherFocusReason)

    def stop(self):
        self.service.cancel('STOP',send_stop=False)
        self.invoke(lambda:self.host.send_command('stop'))

    def hideEvent(self, event):
        self.service.cancel('已离开连续控制页，已STOP')
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        if self.service.engaged:
            if event.type() == QEvent.Type.ApplicationDeactivate:
                self.service.cancel('应用失去焦点，已STOP')
            elif obj is self.window() and (event.type() in (QEvent.Type.WindowDeactivate,QEvent.Type.Close)
                    or event.type() == QEvent.Type.WindowStateChange and self.window().isMinimized()):
                self.service.cancel('窗口失焦或关闭，已STOP')
        return super().eventFilter(obj,event)

    def refresh(self):
        s=self.service;h=self.host;cfg=h.yaw_control.config
        self.status.setText(s.status)
        self.buttons['arm'].setEnabled(s.ready and not s.engaged)
        idle = not s.engaged and not (h.latest and h.latest.armed)
        for key in ('caps','yaw_caps','read','calibrate'):
            self.buttons[key].setEnabled(h.connected and idle and not h.project_request_pending)
        self.buttons['stop'].setEnabled(h.connected)
        self.base_v.setEnabled(idle);self.base_w.setEnabled(idle)
        if idle:
            self.base_w.setMaximum(s.caps['omega_limit'] if s.caps else OMEGA_LIMIT)
            self.base_v.setValue(s.keys.base_v);self.base_w.setValue(s.keys.base_omega)
        self.multiplier.setText(f'倍率 {s.keys.multiplier:.0%}（Z ×1.1 / X ×0.9）')
        if cfg:
            self.params.setText(f'确认RAM rev {h.yaw_control.revision} · 外环 PI {cfg.kp:.6g} / {cfg.ki:.6g} · '
                                f'左轮 {cfg.kp_left:.6g} / {cfg.ki_left:.6g} · 右轮 {cfg.kp_right:.6g} / {cfg.ki_right:.6g}')
        else:
            self.params.setText('控制参数尚未回读；在角速度环实验页应用内外环参数后，在此回读。')
        for key,label in self.pad.labels.items():
            active=key in s.keys.held
            label.setStyleSheet('border-radius:7px;padding:5px;background:'+('#2563eb;color:white;' if active else '#e2e8f0;color:#0f172a;'))
        self.pad.hint.setText('键盘已接管 · 按住方向键运动' if s.running else
                             '等待使能确认' if s.engaged else '未使能 · 点击“使能连续控制”开始')
        v,w=s.keys.target();d=s.latest or {}
        fresh = d and 0 <= time.monotonic()-d['received'] < .5
        limits = f"当前固件目标角速度上限 {s.caps['omega_limit']:.2f} rad/s" if s.caps else '角速度上限等待实际CAPS'
        if cfg:
            limits += f' · RAM差速上限 {cfg.delta_limit:.3f} m/s（几何折合约 {cfg.delta_limit/.16:.2f} rad/s）'
        if fresh and d['flags'] & 1:
            if d['flags'] & 4:limits += ' · 外环差速已限幅'
            if d['flags'] & 24:limits += ' · 轮速PWM已限幅'
        self.status.setText(s.status+'\n'+limits)
        actual=f"{d['v']:+.3f} m/s / {d['omega']:+.3f} rad/s" if fresh else '等待新鲜遥测'
        self.targets.setText(f'请求目标  Vx {v:+.3f} m/s · Vz {w:+.3f} rad/s    |    执行目标 {actual}')
        sample=h.latest
        if sample and h.project_state_fresh:
            measured=(sample.signed_left_speed_mps+sample.signed_right_speed_mps)/2
            gyro=f"{d['measured_omega']:+.3f} rad/s" if fresh else '等待遥测'
            reason=STOP_REASONS[sample.stop_reason] if sample.stop_reason < len(STOP_REASONS) else str(sample.stop_reason)
            self.feedback.setText(f'反馈 Vx {measured:+.3f} m/s · Vz {gyro} · PWM {sample.pwm_left} / {sample.pwm_right} · 最近停止：{reason}')
        else:
            self.feedback.setText('等待新鲜设备状态')
