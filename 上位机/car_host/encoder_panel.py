"""Manual-turn UI; all device and recording operations stay in services."""

from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox


class EncoderTotalsPanel(QDialog):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.setWindowTitle("全量编码器 · 五圈计数")
        self.setMinimumWidth(650)
        layout = QVBoxLayout(self)
        note = QLabel("MCU每个控制周期累加，界面显示20Hz快照。全量累计与旧STATE单次增量分开保存。\n"
                      "1 查询能力 → 2 开始记录 → 3 先左轮5圈、再右轮5圈 → 4 停稳后结束。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.totals = QLabel()
        self.totals.setWordWrap(True)
        self.result = QLabel()
        self.result.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(self.totals)
        turns = QHBoxLayout()
        self.left_turns, self.right_turns = QSpinBox(), QSpinBox()
        for label, box in (("左轮实际圈数", self.left_turns), ("右轮实际圈数", self.right_turns)):
            box.setRange(1, 100)
            box.setValue(5)
            turns.addWidget(QLabel(label)); turns.addWidget(box)
        layout.addLayout(turns)
        buttons = QHBoxLayout()
        self.query_button = QPushButton("查询全量计数能力")
        self.begin_button = QPushButton("开始五圈记录")
        self.finish_button = QPushButton("结束并计算")
        self.cancel_button = QPushButton("取消本次计数")
        for button in (self.query_button, self.begin_button, self.finish_button, self.cancel_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addWidget(self.result)
        self.query_button.clicked.connect(lambda: self._show(host.encoder_totals.query()))
        self.begin_button.clicked.connect(lambda: self._show(host.encoder_totals.begin()))
        self.finish_button.clicked.connect(lambda: self._show(host.encoder_totals.finish(
            self.left_turns.value(), self.right_turns.value())))
        self.cancel_button.clicked.connect(self._cancel)
        host.changed.connect(self.refresh)
        self.refresh()

    def _show(self, result):
        self.result.setText(result.message)
        self.refresh()

    def _cancel(self):
        self.host.encoder_totals.cancel()
        self.result.setText(self.host.encoder_totals.reason)
        self.refresh()

    def refresh(self):
        h, service = self.host, self.host.encoder_totals
        self.status.setText(h.encoder_status)
        if h.capabilities.encoder_totals:
            limits = h.project_limits
            self.status.setText(h.encoder_status +
                f"\n固件轮径 {limits['wheel_diameter_um']/1000:g}mm · "
                f"轮心距 {limits['wheel_center_spacing_um']/1000:g}mm · "
                f"名义每圈 {limits['encoder_counts_per_turn_nominal']}计数")
        s = h.latest_encoder_totals
        self.totals.setText("尚无全量快照" if s is None else
            f"左累计 {s.total_left} · 右累计 {s.total_right}\n"
            f"采样tick {s.device_time_ms} · 周期 {s.interval_ms}ms · 总采样 {s.sample_seq} · "
            f"迟到采样累计 {s.delayed_samples} · {'新鲜' if service.fresh else '过期或未确认能力'}")
        quiet = bool(h.project_state_fresh and not h.latest.armed and not h.latest.pwm_left
                     and not h.latest.pwm_right and not h.control_owner and not h.project_request_pending)
        self.query_button.setEnabled(quiet and service.baseline is None)
        self.begin_button.setEnabled(quiet and service.fresh and service.baseline is None)
        self.finish_button.setEnabled(quiet and service.fresh and service.quiet and service.baseline is not None)
        self.cancel_button.setEnabled(service.baseline is not None)
        if service.result:
            self.result.setText(service.reason + "\n记录目录：" + service.result["directory"])

    def closeEvent(self, event):
        if self.host.encoder_totals.baseline is not None:
            self.host.encoder_totals.cancel()
        super().closeEvent(event)
