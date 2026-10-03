"""Independent PROJECT_V1 feature panels; device semantics stay in services."""

from PySide6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QVBoxLayout)

from .project_protocol import PARAMETER_KEYS, STOP_REASONS


def state_description(host):
    sample = host.latest
    if sample is None or sample.armed is None:
        return "等待有效 STATE"
    reason = STOP_REASONS[sample.stop_reason]
    return (f"设备 tick {sample.device_time_ms} ms · {'已使能' if sample.armed else '已失能'} · "
            f"模式 {sample.control_mode} · 停止原因：{reason}\n"
            f"本地 {'允许' if sample.local_enable else '禁止'} · PWM {sample.pwm_left} / {sample.pwm_right} · "
            f"编码器 {sample.encoder_left} / {sample.encoder_right}")


class MotionPanel(QDialog):
    def __init__(self, host, mode=1):
        super().__init__()
        self.host, self.mode = host, mode
        self.last_request = None
        self.setMinimumWidth(550)
        layout = QVBoxLayout(self)
        note = QLabel("单次有限 PWM 实验" if mode == 1 else "单次有限轮速 PI 控制（mm/s）")
        layout.addWidget(note)
        note = QLabel("先显式使能并等待 STATE，再执行一次。每次到期失能，不重复、不自动续租。\n"
                      "正负号沿用框架；实车轮向尚需核对。零 PWM 表示软件输出归零。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        self.arm_hint = QLabel()
        self.arm_hint.setWordWrap(True)
        layout.addWidget(self.arm_hint)
        grid = QGridLayout()
        self.left, self.right, self.duration = QSpinBox(), QSpinBox(), QSpinBox()
        limit = 6000 if mode == 1 else 300
        for field in (self.left, self.right):
            field.setRange(-limit, limit)
            field.setValue(0)
        self.duration.setRange(1, 1000)
        self.duration.setValue(200)
        for row, (label, field) in enumerate((("左轮 PWM" if mode == 1 else "左轮 mm/s", self.left),
                ("右轮 PWM" if mode == 1 else "右轮 mm/s", self.right), ("时长 ms", self.duration))):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(field, row, 1)
        layout.addLayout(grid)
        row = QHBoxLayout()
        self.arm_button = QPushButton("显式使能 PWM" if mode == 1 else "显式使能 PI")
        self.run_button = QPushButton("执行一次")
        self.stop_button = QPushButton("STOP · 停止并失能")
        for button in (self.arm_button, self.run_button, self.stop_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.result_label = QLabel("尚未发送；使能不会自动启动运动")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        layout.addStretch()
        self.arm_button.clicked.connect(self._arm)
        self.run_button.clicked.connect(self._run)
        self.stop_button.clicked.connect(lambda: self._show_result(host.motion.stop()))
        host.changed.connect(self.refresh)
        self.refresh()

    def _show_result(self, result):
        self.last_request = result.request_id
        self.result_label.setText(result.message)
        self.refresh()

    def _arm(self):
        self._show_result(self.host.motion.arm_pwm() if self.mode == 1 else self.host.motion.arm_speed())

    def _run(self):
        method = self.host.motion.set_pwm if self.mode == 1 else self.host.motion.set_speed
        self._show_result(method(self.left.value(), self.right.value(), self.duration.value()))

    def refresh(self):
        host, sample = self.host, self.host.latest
        capability = host.capabilities.open_loop if self.mode == 1 else host.capabilities.motion
        ready = bool(capability and host.project_state_fresh and sample is not None)
        pending = host.project_control_pending
        self.arm_button.setEnabled(ready and not sample.armed and sample.local_enable and not pending)
        if not host.connected:
            arm_reason = "尚未连接设备。"
        elif not capability:
            arm_reason = host.capabilities.reason_for("open_loop" if self.mode == 1 else "motion")
        elif sample is None or sample.armed is None:
            arm_reason = "等待当前会话的有效 STATE。"
        elif not host.project_state_fresh:
            arm_reason = "需要 500 ms 内且已覆盖前次控制指令的当前会话 STATE。"
        elif pending:
            arm_reason = "前次控制请求尚未确认，请等待 ACK 与 STATE。"
        elif sample.armed:
            arm_reason = "设备已使能；执行对应模式实验，或先 STOP 并确认失能。"
        elif not sample.local_enable:
            arm_reason = "本地禁止：检查板卡电机使能开关（EN / PD3），等待 STATE 显示本地允许。"
        else:
            arm_reason = ""
        self.arm_hint.setText("使能不可用：" + arm_reason if arm_reason else "可显式使能；使能本身不会启动运动。")
        self.arm_button.setToolTip(arm_reason)
        self.run_button.setEnabled(ready and sample.armed and sample.control_mode == self.mode
                                  and not pending and not host.project_motion_active)
        self.stop_button.setEnabled(host.connected and host.config.protocol == "PROJECT_V1")
        self.state_label.setText(state_description(host) + ("\nSTATE 已过期；控制禁用" if not host.project_state_fresh else ""))
        if host.project_limits:
            limit = host.project_limits["pwm_limit" if self.mode == 1 else "speed_limit_mm_s"]
            for field in (self.left, self.right):
                field.setRange(-limit, limit)
            self.duration.setMaximum(host.project_limits["max_duration_ms"])
        if self.last_request in host.command_results:
            result = host.command_results[self.last_request]
            self.result_label.setText(f"{result.status} · {result.message}")


class ParameterPanel(QDialog):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.setMinimumWidth(590)
        layout = QVBoxLayout(self)
        note = QLabel("100 Hz 增量 PI：u += Kp × (error − 上次 error) + Ki × error，error 单位 m/s。\n"
                      "下面输入整数 q100（实际系数 × 100）；Ki 是每 10 ms 采样的系数。\n"
                      "四项同时应用到 RAM；必须先 STOP 并确认失能。没有 Kd，不写 Flash。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.actual_label = QLabel("实际 RAM：尚未回读")
        self.actual_label.setWordWrap(True)
        layout.addWidget(self.actual_label)
        grid = QGridLayout()
        self.draft_boxes = {}
        for row, (key, label) in enumerate(zip(PARAMETER_KEYS, ("左轮 Kp ×100", "左轮 Ki ×100", "右轮 Kp ×100", "右轮 Ki ×100"))):
            field = QSpinBox()
            field.setRange(0, 2000000)
            self.draft_boxes[key] = field
            grid.addWidget(QLabel("草稿 · " + label), row, 0)
            grid.addWidget(field, row, 1)
        layout.addLayout(grid)
        row = QHBoxLayout()
        self.read_button = QPushButton("读取实际 RAM")
        self.copy_button = QPushButton("实际值复制到草稿")
        self.apply_button = QPushButton("应用四项草稿并回读")
        self.stop_button = QPushButton("STOP")
        for button in (self.read_button, self.copy_button, self.apply_button, self.stop_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.result_label = QLabel("尚未发送草稿")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        layout.addStretch()
        self.read_button.clicked.connect(lambda: self._result(host.parameters.read_parameters()))
        self.apply_button.clicked.connect(lambda: self._result(host.parameters.apply_parameters(
            {key: field.value() for key, field in self.draft_boxes.items()})))
        self.copy_button.clicked.connect(self._copy)
        self.stop_button.clicked.connect(lambda: self._result(host.motion.stop()))
        host.changed.connect(self.refresh)
        self.refresh()

    def _result(self, result):
        self.result_label.setText(result.message)
        self.refresh()

    def _copy(self):
        if self.host.actual_parameters is not None:
            for key, field in self.draft_boxes.items():
                field.setValue(self.host.actual_parameters[key])

    def refresh(self):
        host = self.host
        pending = host.project_request_pending
        self.read_button.setEnabled(host.connected and host.capabilities.parameter_read and not pending)
        self.apply_button.setEnabled(host.capabilities.parameter_write and host.project_state_fresh
                                     and not host.latest.armed and not pending)
        self.copy_button.setEnabled(host.actual_parameters is not None)
        self.stop_button.setEnabled(host.connected and host.config.protocol == "PROJECT_V1")
        actual = host.actual_parameters
        if actual is not None:
            values = " / ".join(f"{key}={actual[key]} ({actual[key] / 100:g})" for key in PARAMETER_KEYS)
            self.actual_label.setText(f"实际 RAM revision={actual['revision']}\n{values}")
        else:
            self.actual_label.setText("实际 RAM：尚未回读；草稿值不代表设备参数")
        self.status_label.setText(host.parameter_status)
