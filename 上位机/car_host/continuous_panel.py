"""Continuous-control page. Keyboard events never access device transport."""
import time
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton,
                              QVBoxLayout, QHBoxLayout, QGridLayout, QDoubleSpinBox, QGroupBox, QScrollArea, QComboBox)
from .project_protocol import STOP_REASONS
from .continuous_models import OMEGA_LIMIT

KEYS = {Qt.Key.Key_Up: 'up', Qt.Key.Key_Down: 'down', Qt.Key.Key_Left: 'left',
        Qt.Key.Key_Right: 'right', Qt.Key.Key_Z: 'z', Qt.Key.Key_X: 'x',
        Qt.Key.Key_W: 'w', Qt.Key.Key_S: 's', Qt.Key.Key_A: 'a', Qt.Key.Key_D: 'd'}


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
                                    ('z','Z 加速',0,3),('x','X 减速',1,3),
                                    ('w','W 线速＋',0,4),('s','S 线速－',1,4),
                                    ('a','A 转速－',0,5),('d','D 转速＋',1,5)):
            label = QLabel(text);label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(46);grid.addWidget(label,row,col);self.labels[key] = label
        self.tip = QLabel()
        self.tip.setWordWrap(True)
        self.tip.setAlignment(Qt.AlignmentFlag.AlignCenter);layout.addWidget(self.tip)

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
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        self.scroll_area = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName('formPage')
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        title = QLabel('连续控制');title.setObjectName('sectionTitle');layout.addWidget(title)
        self.status = QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        row = QHBoxLayout();layout.addLayout(row)
        row.addWidget(QLabel('操控模式'))
        self.mode = QComboBox()
        self.mode.addItem('离散指令 · 连续驾驶（默认）', 'discrete')
        self.mode.addItem('按住方向键 · Z/X调速（原操控）', 'legacy')
        row.addWidget(self.mode);row.addStretch()
        self.mode.currentIndexChanged.connect(self.change_mode)
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
        self.base_v = QDoubleSpinBox();self.base_v.setDecimals(2);self.base_v.setRange(0,1.18);self.base_v.setSingleStep(.05);self.base_v.setValue(.1)
        self.base_w = QDoubleSpinBox();self.base_w.setDecimals(2);self.base_w.setRange(0,OMEGA_LIMIT);self.base_w.setSingleStep(.1);self.base_w.setValue(.3)
        row.addWidget(QLabel('基准线速度 (m/s)'));row.addWidget(self.base_v)
        row.addWidget(QLabel('基准角速度 (rad/s)'));row.addWidget(self.base_w)
        self.multiplier = QLabel();row.addWidget(self.multiplier);row.addStretch()
        self.base_v.valueChanged.connect(lambda x:setattr(self.service.keys,'base_v',x))
        self.base_w.valueChanged.connect(lambda x:setattr(self.service.keys,'base_omega',x))
        self.readiness = QLabel();self.readiness.setWordWrap(True);layout.addWidget(self.readiness)
        self.params = QLabel();self.params.setWordWrap(True);layout.addWidget(self.params)
        row = QHBoxLayout();layout.addLayout(row)
        self.buttons['arm'] = QPushButton('使能连续控制');row.addWidget(self.buttons['arm'])
        self.buttons['arm'].clicked.connect(self.arm)
        self.buttons['stop'] = QPushButton('STOP / 空格');row.addWidget(self.buttons['stop'])
        self.buttons['stop'].setObjectName('danger')
        self.buttons['arm'].setObjectName('primary')
        self.buttons['stop'].clicked.connect(self.stop)
        self.pad = DrivingPad(self);layout.addWidget(self.pad,1)
        self.targets = QLabel();self.targets.setWordWrap(True);layout.addWidget(self.targets)
        self.feedback = QLabel();self.feedback.setWordWrap(True);layout.addWidget(self.feedback)
        self.communication = QLabel();self.communication.setWordWrap(True);layout.addWidget(self.communication)
        self.stop_diagnostic = QLabel();self.stop_diagnostic.setWordWrap(True);layout.addWidget(self.stop_diagnostic)
        self.note = QLabel()
        self.note.setStyleSheet('color:#475569;');self.note.setWordWrap(True);layout.addWidget(self.note)
        self.timer = QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.refresh);self.timer.start()
        self.window().installEventFilter(self)
        self.refresh()

    def change_mode(self):
        self.service.set_mode(self.mode.currentData())
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
        discrete = s.mode == 'discrete'
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(0 if discrete else 1)
        self.mode.blockSignals(False)
        self.status.setText(s.status)
        checks = (
            ('能力已确认', bool(s.caps and s.caps['ready'])),
            ('参数已回读', cfg is not None),
            ('IMU已就绪', h.yaw_control.imu_ready),
            ('状态新鲜且本地允许', bool(h.project_state_fresh and h.latest.local_enable)),
        )
        missing = [label for label, ok in checks if not ok]
        self.readiness.setText('  →  '.join(('✓ ' if ok else '○ ') + label for label, ok in checks))
        self.buttons['arm'].setToolTip(
            '就绪：点击使能后由操纵区接管键盘' if s.ready else
            '尚未满足使能条件：' + '、'.join(missing) if missing else
            '等待当前请求或实验结束，并确认设备已失能')
        self.buttons['arm'].setEnabled(s.ready and not s.engaged)
        idle = not s.engaged and not (h.latest and h.latest.armed)
        for key in ('caps','yaw_caps','read','calibrate'):
            self.buttons[key].setEnabled(h.connected and idle and not h.project_request_pending)
        self.buttons['stop'].setEnabled(h.connected)
        self.buttons['status'].setEnabled(h.connected and not h.project_request_pending)
        self.base_v.setEnabled(idle);self.base_w.setEnabled(idle)
        self.base_v.setMinimum(0 if discrete else .01)
        self.base_w.setMinimum(0 if discrete else .01)
        self.base_w.setMaximum(s.caps['omega_limit'] if s.caps else OMEGA_LIMIT)
        for control,value in ((self.base_v,s.keys.base_v),(self.base_w,s.keys.base_omega)):
            control.blockSignals(True);control.setValue(value);control.blockSignals(False)
        self.multiplier.setText(f'幅值 V {s.keys.base_v:.2f} / W {s.keys.base_omega:.2f}' if discrete else
                                f'倍率 {s.keys.multiplier:.0%}（Z ×1.1 / X ×0.9）')
        self.pad.tip.setText('按一次更新方向 · 同向保持、反向一次抵消 · 松键保持 · W/S线速、A/D转速 · 空格STOP' if discrete else
                            '按住运动 · 松键归零 · Z/X共同调速 · ↓← 左后 / ↓→ 右后 · 空格STOP')
        self.note.setText('目标每0.5秒发送 · MCU闭环100Hz · 普通归零平滑 · STOP/失焦/切页立即停止' if discrete else
                          '目标2Hz · MCU闭环100Hz · 松键立即归零 · STOP/失焦/切页立即停止')
        self.communication.setText(s.communication if s.engaged else ('连接仍在 · 控制已停止' if h.connected else h.connection_status))
        self.stop_diagnostic.setText('最近主机停止：'+s.last_stop['reason'] if s.last_stop else '最近主机停止：暂无')
        if cfg:
            self.params.setText(f'确认RAM rev {h.yaw_control.revision} · 外环 PI {cfg.kp:.6g} / {cfg.ki:.6g} · '
                                f'左轮 {cfg.kp_left:.6g} / {cfg.ki_left:.6g} · 右轮 {cfg.kp_right:.6g} / {cfg.ki_right:.6g}')
        else:
            self.params.setText('控制参数尚未回读；在角速度环实验页应用内外环参数后，在此回读。')
        for key,label in self.pad.labels.items():
            label.setVisible(key not in (('z','x') if discrete else ('w','s','a','d')))
            active=key in s.keys.held
            if discrete and key in ('up','down','left','right'):
                active = (s.keys.drive == (1 if key=='up' else -1) if key in ('up','down') else
                          s.keys.turn == (1 if key=='left' else -1))
            label.setStyleSheet('border-radius:7px;padding:5px;background:'+('#2563eb;color:white;' if active else '#e2e8f0;color:#0f172a;'))
        self.pad.hint.setText(('键盘已接管 · 方向状态锁存' if discrete else '键盘已接管 · 按住方向键运动') if s.running else
                             '等待使能确认' if s.engaged else '未使能 · 点击“使能连续控制”开始')
        v,w=s.keys.target();d=s.latest or {}
        fresh = d and 0 <= time.monotonic()-d['received'] < (1. if discrete else .2)
        limits = f"当前固件目标角速度上限 {s.caps['omega_limit']:.2f} rad/s" if s.caps else '角速度上限等待实际CAPS'
        if cfg:
            limits += f' · RAM差速上限 {cfg.delta_limit:.3f} m/s（几何折合约 {cfg.delta_limit/.16:.2f} rad/s）'
        if fresh and d['flags'] & 1:
            if d['flags'] & 4:limits += ' · 外环差速已限幅'
            if d['flags'] & 24:limits += ' · 轮速PWM已限幅'
        self.status.setText(s.status+'\n'+limits)
        actual=f"{d['v']:+.3f} m/s / {d['omega']:+.3f} rad/s" if fresh else '等待新鲜遥测'
        axes = (f"方向状态：{ {1:'前进',0:'前后中立',-1:'后退'}[s.keys.drive]} / "
                f"{ {1:'左转',0:'转向中立',-1:'右转'}[s.keys.turn]}\n") if discrete else ''
        self.targets.setText(axes+f'请求目标  Vx {v:+.3f} m/s · Vz {w:+.3f} rad/s    |    执行目标 {actual}')
        sample=h.latest
        if sample and (s.state_fresh if s.engaged else h.project_state_fresh):
            measured=(sample.signed_left_speed_mps+sample.signed_right_speed_mps)/2
            gyro=f"{d['measured_omega']:+.3f} rad/s" if fresh else '等待遥测'
            reason=STOP_REASONS[sample.stop_reason] if sample.stop_reason < len(STOP_REASONS) else str(sample.stop_reason)
            self.feedback.setText(f'反馈 Vx {measured:+.3f} m/s · Vz {gyro} · PWM {sample.pwm_left} / {sample.pwm_right} · 最近停止：{reason}')
        else:
            self.feedback.setText('等待新鲜设备状态')
