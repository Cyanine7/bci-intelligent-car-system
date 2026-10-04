"""中文四区调试工作台；所有串口行为交给 HostController。"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .controller import HostController
from .preferences import AppPreferences, BluetoothProfile
from .runtime import prepare_qt_fonts
from .transport import ConnectionConfig, list_serial_ports


STYLE = """
QMainWindow, QWidget#workbench { background: #eef2f6; color: #1d2c3e; }
QWidget { font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif; font-size: 12px; }
QLabel#title { font-size: 23px; font-weight: 700; color: #18324d; }
QLabel#subtitle { color: #66798b; }
QLabel#badge { background: #dfebe8; color: #245f52; padding: 6px 12px; border-radius: 12px; font-weight: 600; }
QGroupBox { background: white; border: 1px solid #d6e0e8; border-radius: 8px; margin-top: 18px; padding: 14px 10px 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 13px; padding: 0px 6px; color: #23415d; }
QPushButton { background: #f1f5f8; border: 1px solid #cddae4; border-radius: 5px; padding: 6px 12px; color: #23415d; }
QPushButton:hover { background: #e4edf4; }
QPushButton:disabled { color: #9aa8b3; background: #f4f6f8; }
QPushButton#primary { color: white; background: #276f78; border: 1px solid #276f78; font-weight: 600; }
QPushButton#primary:hover { background: #205e66; }
QPushButton#primary:disabled { background: #a0b8ba; border-color: #a0b8ba; }
QComboBox { border: 1px solid #ccd9e3; border-radius: 4px; padding: 5px 7px; background: white; min-height: 19px; }
QLineEdit, QSpinBox { border: 1px solid #ccd9e3; border-radius: 4px; padding: 5px 7px; background: white; min-height: 19px; }
QPlainTextEdit { border: 1px solid #d5dfe8; border-radius: 5px; background: #fbfcfe; padding: 5px; font-family: Consolas, 'Microsoft YaHei UI'; font-size: 12px; font-weight: 400; }
QLabel#metric { font-size: 27px; font-weight: 700; color: #1f5964; }
QLabel#muted { color: #7a8c9d; font-weight: 400; }
QFrame#metricCard { background: #f3f7fa; border-radius: 6px; }
QSplitter::handle { background: #e1e8ee; }
QStatusBar { background: #e3ebf1; color: #425c73; }
"""


class MainWindow(QMainWindow):
    connection_interacted = Signal()

    def __init__(self, controller: HostController, tool_registry=None, preferences=None,
                 preferences_store=None):
        super().__init__()
        prepare_qt_fonts()
        self.controller = controller
        self._preferences = preferences if preferences is not None else AppPreferences()
        self._preferences_store = preferences_store
        self._real_protocol = self._preferences.profile.protocol
        self._last_connection_kind = "bluetooth_spp"
        if tool_registry is None:
            from .tools import create_default_tool_registry
            tool_registry = create_default_tool_registry()
        self.tool_registry = tool_registry
        self._tool_windows: dict[str, QWidget] = {}
        self._bluetooth_devices_snapshot = None
        self._closing = False
        self._close_ready = False
        self._close_retry_timer = QTimer(self)
        self._close_retry_timer.setInterval(100)
        self._close_retry_timer.timeout.connect(self._retry_close)
        self.setWindowTitle("小车调试工作台 · PROJECT_V1")
        self.resize(1320, 900)
        self.setMinimumSize(1000, 720)
        self.setStyleSheet(STYLE)
        self._last_plot_frame = -1
        self._last_plot_update = 0.0
        root = QWidget()
        root.setObjectName("workbench")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("小车调试工作台")
        title.setObjectName("title")
        subtitle = QLabel("通信监测 · 实时曲线 · 实验数据记录")
        subtitle.setObjectName("subtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.source_badge = QLabel("未连接")
        self.source_badge.setObjectName("badge")
        header.addWidget(self.source_badge)
        layout.addLayout(header)
        layout.addWidget(self._build_connection_bar())
        if controller.automation_service is not None:
            bar = QHBoxLayout()
            self.automation_status_value = QLabel("等待实验表与阶段批准")
            self.automation_status_value.setWordWrap(True)
            bar.addWidget(self.automation_status_value, 1)
            self.automation_view_button = QPushButton("查看实验表与进度")
            self.automation_view_button.clicked.connect(self._show_automation)
            bar.addWidget(self.automation_view_button)
            layout.addLayout(bar)

        vertical = QSplitter(Qt.Orientation.Vertical)
        upper = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(self._build_data_panel(), 3)
        left_layout.addWidget(self._build_send_panel(), 2)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(325)
        left_scroll.setWidget(left)
        upper.addWidget(left_scroll)
        upper.addWidget(self._build_plot_panel())
        upper.setSizes([420, 840])
        upper.setStretchFactor(1, 1)
        vertical.addWidget(upper)
        vertical.addWidget(self._build_log_panel())
        vertical.setSizes([530, 220])
        layout.addWidget(vertical, 1)
        self.statusBar().showMessage("模拟设备可无硬件演示；蓝牙首次配对请在 Windows 设置完成。")

        self.controller.changed.connect(self.refresh_state)
        self.controller.log_added.connect(self._append_log)
        self.controller.logs_cleared.connect(self.log_view.clear)
        self.refresh_ports()
        self._connection_kind_changed()
        self.recording_status_label = QLabel("未录制")
        self.statusBar().addPermanentWidget(self.recording_status_label)
        self.refresh_state()

    def _build_connection_bar(self) -> QGroupBox:
        box = QGroupBox("连接与记录")
        column = QVBoxLayout(box)
        column.setSpacing(8)
        row = QHBoxLayout()
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("USB / 蓝牙串口", "serial")
        self.kind_combo.addItem("模拟设备（无硬件演示）", "simulation")
        self.kind_combo.addItem("蓝牙 SPP（直接无线）", "bluetooth_spp")
        self.kind_combo.setCurrentIndex(self.kind_combo.findData("bluetooth_spp"))
        self.kind_combo.setMinimumWidth(185)
        self.kind_combo.setToolTip("直接蓝牙使用 Classic RFCOMM/SPP；已有虚拟 COM 口仍可选择串口模式。")
        self.serial_fields = QWidget()
        serial_row = QHBoxLayout(self.serial_fields)
        serial_row.setContentsMargins(0, 0, 0, 0)
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(135)
        self.port_combo.setToolTip("填写COM口名称。USB对应哪个USART须按实物核对。")
        self.refresh_button = QPushButton("刷新串口")
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(["9600", "57600", "115200", "230400", "460800", "921600"])
        self.baud_combo.setCurrentText("230400")
        self.baud_combo.setMinimumWidth(95)
        self.baud_combo.setToolTip("仅用于串口模式；直接蓝牙的 MCU—模块 USART2 波特率由设备侧配置。")
        self.connect_button = QPushButton("连接")
        self.connect_button.setObjectName("primary")
        self.disconnect_button = QPushButton("断开")
        self.record_button = QPushButton("开始录制")
        self.record_button.setToolTip("保存telemetry.csv和communication.log；暂停显示仍继续录制。")
        row.addWidget(QLabel("设备"))
        row.addWidget(self.kind_combo)
        for label, widget in (("端口", self.port_combo), ("波特率", self.baud_combo)):
            serial_row.addWidget(QLabel(label))
            serial_row.addWidget(widget)
        serial_row.addWidget(self.refresh_button)
        serial_row.addWidget(QLabel("8N1"))
        row.addWidget(self.serial_fields, 1)
        self.protocol_label = QLabel("LEGACY_APP · 速度幅值遥测")
        self.protocol_label.setObjectName("muted")
        self.protocol_label.setToolTip("适用于当前 bci 固件的 C 帧遥测；厂家完整固件的 C 帧为参数，不能混用。")
        row.addStretch()
        row.addWidget(self.connect_button)
        row.addWidget(self.disconnect_button)
        row.addWidget(self.record_button)
        column.addLayout(row)

        protocol_row = QHBoxLayout()
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItem("LEGACY_APP · 旧固件 / 模拟", "LEGACY_APP")
        self.protocol_combo.addItem("PROJECT_V1 · 新固件 / 有限实验", "PROJECT_V1")
        self.protocol_combo.setCurrentIndex(self.protocol_combo.findData(self._preferences.profile.protocol))
        self.protocol_combo.setToolTip("新烧录固件请选择 PROJECT_V1；连接后仅自动 HELLO 握手，不自动使能或启动。模拟设备仅支持 LEGACY_APP。")
        self.stop_button = QPushButton("STOP · 停止并失能")
        self.stop_button.clicked.connect(self._stop_project)
        protocol_row.addWidget(QLabel("应用协议"))
        protocol_row.addWidget(self.protocol_combo)
        protocol_row.addWidget(self.protocol_label, 1)
        protocol_row.addWidget(self.stop_button)
        column.addLayout(protocol_row)

        self.bluetooth_panel = QWidget()
        bluetooth_column = QVBoxLayout(self.bluetooth_panel)
        bluetooth_column.setContentsMargins(0, 0, 0, 0)
        bluetooth_column.setSpacing(5)
        bluetooth_grid = QGridLayout()
        bluetooth_grid.setHorizontalSpacing(8)
        self.scan_button = QPushButton("扫描设备")
        self.cancel_scan_button = QPushButton("取消扫描")
        scan_actions = QWidget()
        scan_row = QHBoxLayout(scan_actions)
        scan_row.setContentsMargins(0, 0, 0, 0)
        scan_row.setSpacing(5)
        scan_row.addWidget(self.scan_button)
        scan_row.addWidget(self.cancel_scan_button)
        self.bluetooth_device_combo = QComboBox()
        self.bluetooth_device_combo.setMinimumWidth(175)
        self.bluetooth_device_combo.setPlaceholderText("扫描后选择，也可直接填写 MAC")
        self.bluetooth_device_combo.setToolTip("名称和 MAC 来自 Windows 发现或缓存；出现在列表中不代表当前在线。")
        self.bluetooth_filter = QLineEdit()
        self.bluetooth_filter.setPlaceholderText("按名称或 MAC 筛选")
        self.bluetooth_filter.setMaximumWidth(180)
        self.bluetooth_filter.setToolTip("只筛选已发现设备的名称或 MAC，不会自动重新扫描。")
        self.bluetooth_address = QLineEdit()
        self.bluetooth_address.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        self.bluetooth_address.setMinimumWidth(155)
        self.bluetooth_address.setToolTip("按 MAC 连接自己的设备，不依赖名称或扫描结果；可修改并保存配置。")
        self.bluetooth_address.setText(self._preferences.profile.address)
        self.rfcomm_channel = QSpinBox()
        self.rfcomm_channel.setRange(1, 30)
        self.rfcomm_channel.setValue(self._preferences.profile.channel)
        self.rfcomm_channel.setMaximumWidth(75)
        self.rfcomm_channel.setToolTip("RFCOMM 通道 1–30，默认 1；这不是 COM 口号，也不是 BLE UUID。")
        for index, (label, widget) in enumerate((
            ("蓝牙发现", scan_actions), ("设备名 · MAC", self.bluetooth_device_combo),
            ("名称 / MAC 过滤", self.bluetooth_filter), ("设备 MAC", self.bluetooth_address),
            ("通道", self.rfcomm_channel),
        )):
            caption = QLabel(label)
            caption.setObjectName("muted")
            bluetooth_grid.addWidget(caption, 0, index)
            bluetooth_grid.addWidget(widget, 1, index)
        bluetooth_grid.setColumnStretch(1, 2)
        bluetooth_grid.setColumnStretch(2, 1)
        bluetooth_grid.setColumnStretch(3, 1)
        bluetooth_column.addLayout(bluetooth_grid)
        profile_row = QHBoxLayout()
        self.bluetooth_alias = QLineEdit(self._preferences.profile.alias)
        self.bluetooth_alias.setMaxLength(64)
        self.bluetooth_alias.setMaximumWidth(190)
        self.bluetooth_alias.setToolTip("本地显示名称，不修改蓝牙模块名称；连接仍以 MAC 为准。")
        self.auto_connect_checkbox = QCheckBox("启动时自动连接一次")
        self.auto_connect_checkbox.setChecked(self._preferences.auto_connect)
        self.auto_connect_checkbox.setToolTip("启动后按保存的 MAC 尝试一次；失败或掉线后请手动连接。修改后请保存。")
        self.save_profile_button = QPushButton("保存设备配置")
        profile_row.addWidget(QLabel("本地别名"))
        profile_row.addWidget(self.bluetooth_alias)
        profile_row.addWidget(self.auto_connect_checkbox)
        profile_row.addStretch()
        profile_row.addWidget(self.save_profile_button)
        bluetooth_column.addLayout(profile_row)
        self.profile_label = QLabel()
        self.profile_label.setWordWrap(True)
        self.profile_label.setObjectName("muted")
        self._update_profile_summary()
        bluetooth_column.addWidget(self.profile_label)
        self.bluetooth_scan_label = QLabel("尚未扫描")
        self.bluetooth_scan_label.setWordWrap(True)
        self.bluetooth_scan_label.setObjectName("muted")
        bluetooth_column.addWidget(self.bluetooth_scan_label)
        self.bluetooth_note = QLabel("首次配对请在 Windows 设置完成；缓存设备不一定在线。USART2 波特率由 MCU 与蓝牙模块匹配。")
        self.bluetooth_note.setObjectName("muted")
        self.bluetooth_note.setWordWrap(True)
        self.bluetooth_note.setToolTip("电脑直接 RFCOMM 连接不需要设置波特率；当前源码的 USART2 为 230400 / 8N1，须核对实物。")
        bluetooth_column.addWidget(self.bluetooth_note)
        column.addWidget(self.bluetooth_panel)
        self.kind_combo.currentIndexChanged.connect(self._kind_selected)
        self.protocol_combo.currentIndexChanged.connect(self._protocol_selected)
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.record_button.clicked.connect(self._toggle_recording)
        self.scan_button.clicked.connect(self._scan_bluetooth)
        self.cancel_scan_button.clicked.connect(self._cancel_bluetooth_scan)
        self.bluetooth_device_combo.currentIndexChanged.connect(self._bluetooth_device_selected)
        self.bluetooth_filter.textChanged.connect(self._update_bluetooth_devices)
        self.bluetooth_filter.textChanged.connect(self._connection_field_edited)
        self.bluetooth_address.textChanged.connect(self._connection_field_edited)
        self.rfcomm_channel.valueChanged.connect(self._connection_field_edited)
        self.bluetooth_alias.textChanged.connect(self._connection_field_edited)
        self.auto_connect_checkbox.toggled.connect(self._connection_field_edited)
        self.save_profile_button.clicked.connect(self._save_device_profile)
        return box

    def _metric_card(self, name: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("metricCard")
        column = QVBoxLayout(card)
        column.setContentsMargins(12, 8, 12, 8)
        label = QLabel(name)
        label.setObjectName("muted")
        value = QLabel("—")
        value.setObjectName("metric")
        column.addWidget(label)
        column.addWidget(value)
        return card, value

    def _build_data_panel(self) -> QGroupBox:
        box = QGroupBox("01  数据监测")
        column = QVBoxLayout(box)
        row = QHBoxLayout()
        left_card, self.left_value = self._metric_card("左轮速度幅值 · m/s")
        right_card, self.right_value = self._metric_card("右轮速度幅值 · m/s")
        self.left_speed_caption = left_card.layout().itemAt(0).widget()
        self.right_speed_caption = right_card.layout().itemAt(0).widget()
        row.addWidget(left_card)
        row.addWidget(right_card)
        column.addLayout(row)
        grid = QGridLayout()
        self.battery_value = QLabel("—")
        self.connection_value = QLabel("未连接")
        self.connection_value.setWordWrap(True)
        self.telemetry_value = QLabel("无有效数据")
        self.frequency_value = QLabel("—")
        self.last_frame_value = QLabel("—")
        self.counter_value = QLabel("RX 0 B / TX 0 B / 0 帧")
        for i, (label, value) in enumerate((
            ("估算电量", self.battery_value), ("连接状态", self.connection_value),
            ("遥测状态", self.telemetry_value), ("有效帧频率", self.frequency_value),
            ("最后有效数据", self.last_frame_value), ("通信累计", self.counter_value),
        )):
            caption = QLabel(label)
            caption.setMinimumHeight(caption.fontMetrics().height() + 4)
            value.setMinimumHeight(value.fontMetrics().height() + 4)
            grid.addWidget(caption, i, 0)
            grid.addWidget(value, i, 1)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(1, 1)
        column.addLayout(grid)
        unavailable = self.device_state_value = QLabel("PWM · IMU · 姿态角 · 编码器计数：未提供")
        unavailable.setObjectName("muted")
        unavailable.setWordWrap(True)
        note = self.telemetry_note = QLabel("当前协议只反馈速度幅值，不能判断前进或后退；电量由固件估算。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        column.addWidget(unavailable)
        column.addWidget(note)
        column.addStretch()
        return box

    def _build_send_panel(self) -> QGroupBox:
        box = QGroupBox("02  手动发送")
        column = QVBoxLayout(box)
        row = QHBoxLayout()
        self.send_format = QComboBox()
        self.send_format.addItems(["文本 UTF-8", "HEX"])
        self.line_ending = QComboBox()
        self.line_ending.addItem("不追加换行", "")
        self.line_ending.addItem("追加 CRLF", "\r\n")
        self.line_ending.addItem("追加 LF", "\n")
        row.addWidget(self.send_format)
        row.addWidget(self.line_ending)
        self.send_button = QPushButton("单次发送")
        self.send_button.setObjectName("primary")
        row.addWidget(self.send_button)
        column.addLayout(row)
        self.send_editor = QPlainTextEdit()
        self.send_editor.setPlaceholderText("输入调试内容；HEX示例：48 45 4C 4C 4F")
        self.send_editor.setMaximumHeight(80)
        self.send_editor.document().setMaximumBlockCount(100)
        column.addWidget(self.send_editor)
        self.send_result = QLabel("写出完成仅表示通信发送，MCU 接受与执行需协议确认。")
        self.send_result.setObjectName("muted")
        self.send_result.setWordWrap(True)
        column.addWidget(self.send_result)
        future = QHBoxLayout()
        self.tool_buttons: dict[str, QPushButton] = {}
        for specification in self.tool_registry.tools():
            button = QPushButton(specification.label)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, tool_id=specification.id: self._open_tool(tool_id))
            self.tool_buttons[specification.id] = button
            future.addWidget(button)
        self.pid_button = self.tool_buttons.get("pid")
        column.addLayout(future)
        self.tool_reason_label = QLabel()
        self.tool_reason_label.setWordWrap(True)
        self.tool_reason_label.setObjectName("muted")
        column.addWidget(self.tool_reason_label)
        self.send_button.clicked.connect(self._send)
        self.send_format.currentIndexChanged.connect(lambda: self.line_ending.setEnabled(self.send_format.currentIndex() == 0))
        return box

    def _build_plot_panel(self) -> QGroupBox:
        box = QGroupBox("03  实时曲线")
        column = QVBoxLayout(box)
        row = QHBoxLayout()
        self.left_channel = QCheckBox("左轮速度幅值")
        self.right_channel = QCheckBox("右轮速度幅值")
        self.left_channel.setChecked(True)
        self.right_channel.setChecked(True)
        self.plot_pause = QCheckBox("暂停曲线")
        self.clear_plot_button = QPushButton("清空曲线")
        row.addWidget(self.left_channel)
        row.addWidget(self.right_channel)
        row.addStretch()
        row.addWidget(self.plot_pause)
        row.addWidget(self.clear_plot_button)
        column.addLayout(row)
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background="w")
        self.plot.setMinimumHeight(200)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("left", "速度幅值", units="m/s")
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.plot.setLabel("bottom", "相对当前时间", units="s")
        self.plot.setXRange(-60, 0, padding=0)
        self.plot.setYRange(0, 0.3, padding=0.08)
        self.plot.enableAutoRange(axis="y", enable=True)
        self.plot.addLegend(offset=(15, 10))
        self.plot.getAxis("left").setPen(pg.mkPen("#728699"))
        self.plot.getAxis("bottom").setPen(pg.mkPen("#728699"))
        self.plot.getAxis("left").setTextPen(pg.mkPen("#52697e"))
        self.plot.getAxis("bottom").setTextPen(pg.mkPen("#52697e"))
        self.left_curve = self.plot.plot([], [], name="左轮", pen=pg.mkPen("#277b86", width=2))
        self.right_curve = self.plot.plot([], [], name="右轮", pen=pg.mkPen("#e49b45", width=2))
        column.addWidget(self.plot, 1)
        tip = QLabel("显示最近60秒 · 暂停仅冻结曲线，接收与录制继续 · 鼠标可缩放查看")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        column.addWidget(tip)
        self.left_channel.toggled.connect(lambda checked: self.left_curve.setVisible(checked))
        self.right_channel.toggled.connect(lambda checked: self.right_curve.setVisible(checked))
        self.plot_pause.toggled.connect(self._plot_pause_changed)
        self.clear_plot_button.clicked.connect(self._clear_plot)
        return box

    def _build_log_panel(self) -> QGroupBox:
        box = QGroupBox("04  通信与事件日志")
        column = QVBoxLayout(box)
        row = QHBoxLayout()
        self.log_format = QComboBox()
        self.log_format.addItems(["文本", "HEX"])
        self.log_pause = QCheckBox("暂停日志显示")
        self.log_hint = QLabel("最近1000条；完整数据请开始录制")
        self.log_hint.setObjectName("muted")
        self.clear_log_button = QPushButton("清空")
        self.save_log_button = QPushButton("保存当前日志")
        row.addWidget(self.log_format)
        row.addWidget(self.log_pause)
        row.addWidget(self.log_hint)
        row.addStretch()
        row.addWidget(self.clear_log_button)
        row.addWidget(self.save_log_button)
        column.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.document().setMaximumBlockCount(1000)
        column.addWidget(self.log_view)
        self.log_format.currentIndexChanged.connect(self._rebuild_logs)
        self.log_pause.toggled.connect(self._log_pause_changed)
        self.clear_log_button.clicked.connect(self.controller.clear_logs)
        self.save_log_button.clicked.connect(self._save_logs)
        return box

    def refresh_ports(self) -> None:
        previous = self.port_combo.currentText().split(" · ", 1)[0].strip()
        self.port_combo.clear()
        try:
            ports = list_serial_ports()
            for port, description in ports:
                self.port_combo.addItem(f"{port} · {description}", port)
            if previous:
                index = self.port_combo.findData(previous)
                if index >= 0:
                    self.port_combo.setCurrentIndex(index)
                else:
                    self.port_combo.setEditText(previous)
            self.port_combo.setPlaceholderText("未发现串口，可手动填写")
        except Exception as exc:
            self.controller.add_log(f"串口枚举失败：{exc}", "ERROR")

    def _connection_field_edited(self, *_args) -> None:
        self.connection_interacted.emit()

    def _kind_selected(self, *_args) -> None:
        self.connection_interacted.emit()
        kind = self.kind_combo.currentData()
        if kind == "simulation" and self._last_connection_kind != "simulation":
            self._real_protocol = self.protocol_combo.currentData()
            self.protocol_combo.setCurrentIndex(self.protocol_combo.findData("LEGACY_APP"))
        elif kind != "simulation" and self._last_connection_kind == "simulation":
            self.protocol_combo.setCurrentIndex(self.protocol_combo.findData(self._real_protocol))
        self._last_connection_kind = kind
        self._connection_kind_changed()
        self.protocol_combo.setEnabled(self.controller.worker is None
                                       and not self.controller.bluetooth_scanning
                                       and not self._closing and kind != "simulation")
        self.refresh_state()

    def _protocol_selected(self, *_args) -> None:
        self.connection_interacted.emit()
        if self.kind_combo.currentData() != "simulation":
            self._real_protocol = self.protocol_combo.currentData()
        self.refresh_state()

    def _profile_device_name(self) -> str:
        address = self.bluetooth_address.text().strip().replace("-", "").replace(":", "").upper()
        for name, mac in self.controller.bluetooth_devices:
            if mac.replace(":", "").upper() == address:
                return name
        profile = self._preferences.profile
        return profile.device_name if profile.address.replace(":", "") == address else ""

    def current_preferences(self) -> AppPreferences:
        """Build validated preferences without saving or connecting a device."""
        protocol = self._real_protocol if self.kind_combo.currentData() == "simulation" else self.protocol_combo.currentData()
        profile = BluetoothProfile(alias=self.bluetooth_alias.text(),
                                   address=self.bluetooth_address.text(),
                                   channel=self.rfcomm_channel.value(), protocol=protocol,
                                   device_name=self._profile_device_name())
        return AppPreferences(profile=profile, auto_connect=self.auto_connect_checkbox.isChecked())

    def _update_profile_summary(self) -> None:
        profile = self._preferences.profile
        auto = "启动尝试一次" if self._preferences.auto_connect else "启动不连接"
        self.profile_label.setText(f"设备配置：{profile.alias} · {profile.address} · "
                                   f"RFCOMM {profile.channel} · {profile.protocol} · {auto}")

    def _save_device_profile(self) -> None:
        self.connection_interacted.emit()
        try:
            preferences = self.current_preferences()
            if self._preferences_store is None:
                self.controller.add_log("未配置设备配置存储，设置尚未保存。", "WARNING")
                return
            self._preferences_store.save(preferences)
        except (OSError, ValueError, TypeError) as exc:
            self.controller.add_log(f"设备配置保存失败：{exc}", "ERROR")
            return
        self._preferences = preferences
        self.bluetooth_alias.setText(preferences.profile.alias)
        self.bluetooth_address.setText(preferences.profile.address)
        self._update_profile_summary()
        self.controller.add_log("设备配置已保存；下次启动按此配置处理自动连接，当前不会发起连接。")

    def _connection_kind_changed(self) -> None:
        real = self.kind_combo.currentData() == "serial"
        bluetooth = self.kind_combo.currentData() == "bluetooth_spp"
        idle = self.controller.worker is None
        scanning = getattr(self.controller, "bluetooth_scanning", False)
        self.serial_fields.setVisible(real)
        self.bluetooth_panel.setVisible(bluetooth)
        self.protocol_label.setVisible(True)
        for widget in (self.port_combo, self.baud_combo, self.refresh_button):
            widget.setEnabled(real and idle and not scanning and not self._closing)
        for widget in (self.bluetooth_device_combo, self.bluetooth_filter,
                       self.bluetooth_address, self.rfcomm_channel, self.bluetooth_alias):
            widget.setEnabled(bluetooth and idle and not self._closing)
        self.auto_connect_checkbox.setEnabled(bluetooth and not self._closing)
        self.save_profile_button.setEnabled(bluetooth and idle and not scanning and not self._closing)
        self.scan_button.setEnabled(bluetooth and idle and not scanning and not self._closing)
        self.cancel_scan_button.setEnabled(bluetooth and scanning and not self._closing)

    def _scan_bluetooth(self) -> None:
        self.connection_interacted.emit()
        if self._closing or self.controller.worker is not None:
            return
        self.controller.scan_bluetooth()
        self.refresh_state()

    def _cancel_bluetooth_scan(self) -> None:
        self.connection_interacted.emit()
        self.controller.cancel_bluetooth_scan()
        self.refresh_state()

    def _update_bluetooth_devices(self, *_args) -> None:
        devices = tuple(getattr(self.controller, "bluetooth_devices", ())[:128])
        name_filter = self.bluetooth_filter.text().strip().casefold()
        address_filter = name_filter.replace("-", ":")
        snapshot = (devices, name_filter)
        if snapshot == self._bluetooth_devices_snapshot:
            return
        self._bluetooth_devices_snapshot = snapshot
        previous = self.bluetooth_device_combo.currentData()
        self.bluetooth_device_combo.blockSignals(True)
        try:
            self.bluetooth_device_combo.clear()
            for name, address in devices:
                if name_filter and name_filter not in name.casefold() and address_filter not in address.casefold():
                    continue
                self.bluetooth_device_combo.addItem(f"{name or '未命名设备'} · {address}", address)
            index = self.bluetooth_device_combo.findData(previous) if previous else -1
            self.bluetooth_device_combo.setCurrentIndex(index)
        finally:
            self.bluetooth_device_combo.blockSignals(False)

    def _bluetooth_device_selected(self, index: int) -> None:
        address = self.bluetooth_device_combo.itemData(index)
        if address:
            self.bluetooth_address.setText(address)

    def _connect(self) -> None:
        self.connection_interacted.emit()
        if self._closing:
            return
        if getattr(self.controller, "bluetooth_scanning", False):
            self.controller.add_log("请等待扫描结束，或取消扫描后再连接。", "WARNING")
            return
        kind = self.kind_combo.currentData()
        if kind == "bluetooth_spp":
            try:
                config = self.current_preferences().profile.to_connection_config()
            except (ValueError, TypeError) as exc:
                self.controller.add_log(str(exc), "ERROR")
                return
            self.controller.connect_device(config)
            return
        baud = 230400
        if kind == "serial":
            try:
                baud = int(self.baud_combo.currentText())
                if not 1 <= baud <= 4000000:
                    raise ValueError
            except ValueError:
                self.controller.add_log("请输入1至4000000之间的整数波特率", "ERROR")
                return
        # Editable QComboBox.currentData can retain an old selected COM port.
        port = self.port_combo.currentText().split(" · ", 1)[0].strip()
        self.controller.connect_device(ConnectionConfig(
            kind=kind, port=port if kind == "serial" else "", baudrate=baud,
            rfcomm_channel=self.rfcomm_channel.value(), protocol=self.protocol_combo.currentData(),
        ))

    def _disconnect(self) -> None:
        self.connection_interacted.emit()
        self.controller.disconnect_device()

    def _stop_project(self):
        result = self.controller.motion.stop()
        self.controller.add_log(result.message, "INFO" if result.success else "WARNING", request_id=result.request_id or "")

    def _open_tool(self, tool_id: str) -> None:
        if self._closing:
            return
        existing = self._tool_windows.get(tool_id)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        try:
            panel = self.tool_registry.create_panel(tool_id, self.controller)
            if not isinstance(panel, QWidget):
                raise TypeError("功能面板必须返回 QWidget")
            if isinstance(panel, QDialog):
                dialog = panel
                dialog.setParent(self, Qt.WindowType.Dialog)
            else:
                dialog = QDialog(self)
                QVBoxLayout(dialog).addWidget(panel)
            dialog.setWindowTitle(self.tool_registry.get(tool_id).label)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._tool_windows[tool_id] = dialog
            dialog.destroyed.connect(lambda: self._tool_windows.pop(tool_id, None))
            dialog.show()
        except Exception as exc:
            self.controller.add_log(f"功能面板无法打开：{exc}", "ERROR")

    def _refresh_tools(self) -> None:
        capabilities = getattr(self.controller, "capabilities", None)
        for tool_id, button in self.tool_buttons.items():
            if capabilities is None:
                button.setEnabled(False)
                button.setToolTip("等待设备能力接口；当前协议没有参数与控制命令。")
                continue
            availability = self.tool_registry.availability(tool_id, capabilities)
            button.setEnabled(availability.enabled and not self._closing)
            button.setToolTip(availability.reason)
        if self.pid_button is not None:
            self.tool_reason_label.setText("PID调参：" + self.pid_button.toolTip())

    def _send(self) -> None:
        accepted = self.controller.send_manual(self.send_editor.toPlainText(), self.send_format.currentIndex() == 1,
                                               self.line_ending.currentData())
        self.send_result.setText("已加入发送队列；实际写入结果见日志。" if accepted else "未发送，请查看日志中的原因。")

    def _toggle_recording(self) -> None:
        if self.controller.recorder.is_running and not self.controller.recorder.is_active:
            return
        if self.controller.recorder.is_running:
            self.controller.stop_recording()
        else:
            self.controller.start_recording()

    def _append_log(self, entry) -> None:
        if not self.log_pause.isChecked():
            self.log_view.appendPlainText(entry.format(self.log_format.currentIndex() == 1))
        if entry.level in ("WARNING", "ERROR"):
            self.statusBar().showMessage(entry.message)
        elif entry.direction == "TX":
            self.send_result.setText(entry.message)

    def _rebuild_logs(self) -> None:
        if self.log_pause.isChecked():
            return
        self.log_view.setPlainText("\n".join(entry.format(self.log_format.currentIndex() == 1) for entry in self.controller.logs))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _log_pause_changed(self, paused: bool) -> None:
        self.log_hint.setText("显示已暂停；接收与文件录制继续" if paused else "最近1000条；完整数据请开始录制")
        if not paused:
            self._rebuild_logs()

    def _plot_pause_changed(self, paused: bool) -> None:
        if not paused:
            self._last_plot_frame = -1
            self.refresh_state()

    def _clear_plot(self) -> None:
        self.controller.clear_history()
        self.left_curve.setData([], [])
        self.right_curve.setData([], [])
        self._last_plot_frame = -1

    def _save_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存当前日志", str(self.controller.data_directory / "当前日志.log"), "日志文件 (*.log);;文本文件 (*.txt)")
        if path:
            self.controller.save_visible_logs(Path(path), self.log_format.currentIndex() == 1)

    def refresh_state(self) -> None:
        host = self.controller
        if host.automation_service is not None and hasattr(self, "automation_status_value"):
            from .automation_panel import progress_text
            self.automation_status_value.setText(progress_text(host.automation_service))
        idle = host.worker is None
        scanning = getattr(host, "bluetooth_scanning", False)
        self.kind_combo.setEnabled(idle and not scanning and not self._closing)
        self.protocol_combo.setEnabled(idle and not scanning and not self._closing
                                       and self.kind_combo.currentData() != "simulation")
        self.stop_button.setEnabled(host.connected and host.config.protocol == "PROJECT_V1" and not self._closing)
        self.protocol_label.setText(host.protocol_status if not idle else "新固件请选择 PROJECT_V1；模拟设备请选择 LEGACY_APP")
        self._connection_kind_changed()
        self._update_bluetooth_devices()
        self.bluetooth_scan_label.setText(getattr(host, "bluetooth_scan_status", "尚未扫描"))
        self.connect_button.setEnabled(idle and not scanning and not self._closing)
        self.disconnect_button.setEnabled(host.worker is not None and host.connection_status != "正在断开" and not self._closing)
        self.send_button.setEnabled(host.connected and host.capabilities.raw_send and not self._closing)
        self.send_button.setToolTip("" if host.capabilities.raw_send else host.capabilities.reason_for("raw_send"))
        saving = host.recorder.is_running and not host.recorder.is_active
        self.record_button.setEnabled((host.connected or host.recorder.is_running) and not saving and not self._closing and not host.control_owner)
        self.record_button.setText("正在保存" if saving else "停止录制" if host.recorder.is_active else "开始录制")
        self.connection_value.setText(host.connection_status)
        self.telemetry_value.setText(host.telemetry_status)
        good = host.telemetry_status == "接收正常"
        self.telemetry_value.setStyleSheet("color: #277b66;" if good else "color: #ad7130;")
        self.source_badge.setText("PROJECT_V1 假设备 · 纯软件" if host.source == "SIMULATION_PROJECT_V1" else
                                 "模拟数据" if host.source == "SIMULATOR" and host.worker is not None else
                                 "蓝牙 SPP" if host.source == "BLUETOOTH_SPP" and host.worker is not None else
                                 "真实串口" if host.connected else "未连接")
        self.source_badge.setToolTip("Classic Bluetooth RFCOMM/SPP · 真实无线数据" if host.source == "BLUETOOTH_SPP" else "")
        self._refresh_tools()
        self.frequency_value.setText(f"{host.receive_frequency:.1f} Hz")
        self.counter_value.setText(f"RX {host.bytes_received:,} B / TX {host.bytes_sent:,} B / {host.frames_received:,} 帧")
        sample = host.latest
        active_protocol = (host.config.protocol if host.worker is not None and host.config is not None
                           else self.protocol_combo.currentData())
        signed = sample.signed_left_speed_mps is not None if sample is not None else active_protocol == "PROJECT_V1"
        if sample is None:
            self.left_value.setText("—")
            self.right_value.setText("—")
            self.battery_value.setText("—")
            self.last_frame_value.setText("—")
            if signed:
                self.device_state_value.setText("等待 PROJECT_V1 握手和有效 STATE；PWM / 编码器 / tick 尚未提供")
                self.telemetry_note.setText("PROJECT_V1 使用有符号轮速（m/s）；等待 CAPS 握手和有效 STATE，尚无有效测量。")
            else:
                self.device_state_value.setText("PWM · IMU · 姿态角 · 编码器计数：旧协议未提供")
                self.telemetry_note.setText("LEGACY_APP 只反馈速度幅值，不能判断前进或后退；电量由固件估算。")
        else:
            self.left_value.setText(f"{sample.signed_left_speed_mps:+.3f}" if signed else f"{sample.left_speed_abs_mps:.2f}")
            self.right_value.setText(f"{sample.signed_right_speed_mps:+.3f}" if signed else f"{sample.right_speed_abs_mps:.2f}")
            if signed:
                from .project_panels import state_description
                self.device_state_value.setText(state_description(host) + f"\n电压 {sample.battery_mv} mV · RX/TX 丢失 {sample.rx_dropped}/{sample.tx_dropped} · 参数 revision {sample.parameter_revision}")
                self.telemetry_note.setText("PROJECT_V1 显示有符号轮速（m/s），正号沿用框架向前；实车极性待核对。")
            else:
                self.device_state_value.setText("PWM · IMU · 姿态角 · 编码器计数：旧协议未提供")
                self.telemetry_note.setText("LEGACY_APP 只反馈速度幅值，不能判断前进或后退；电量由固件估算。")
            self.battery_value.setText(f"{sample.battery_percent_raw}%" + (" · 越界" if sample.quality else ""))
            self.battery_value.setStyleSheet("color: #a04d36;" if sample.quality else "")
            age = max(0.0, time.monotonic() - sample.received_monotonic)
            self.last_frame_value.setText(f"{sample.received_utc[11:23]} UTC · {age:.1f}秒前")
        self.left_speed_caption.setText("左轮有符号轮速 · m/s" if signed else "左轮速度幅值 · m/s")
        self.right_speed_caption.setText("右轮有符号轮速 · m/s" if signed else "右轮速度幅值 · m/s")
        for checkbox, prefix in ((self.left_channel, "左轮"), (self.right_channel, "右轮")):
            checkbox.setText(prefix + ("有符号轮速" if signed else "速度幅值"))
        self.plot.setLabel("left", "有符号轮速" if signed else "速度幅值", units="m/s")
        self.record_button.setToolTip(host.recording_status + (f"\n{host.recorder.directory}" if host.recorder.directory else ""))
        self.recording_status_label.setText(("● 录制中 · 不完整" if host.recording_incomplete else "● 录制中") if host.recorder.is_active else host.recording_status)
        self.recording_status_label.setToolTip(str(host.recorder.directory or ""))
        self.statusBar().setToolTip(host.recording_status)
        if host.recorder.is_active:
            self.record_button.setStyleSheet("color: #a3483b; font-weight: 600;")
        else:
            self.record_button.setStyleSheet("")
        now = time.monotonic()
        if not self.plot_pause.isChecked() and now - self._last_plot_update >= 0.05:
            # Fixed time axis also exposes gaps instead of extending the last sample.
            samples = tuple(host.history.samples)
            if samples:
                x = np.fromiter((sample.received_monotonic - now for sample in samples), dtype=float)
                left = np.fromiter((sample.signed_left_speed_mps if sample.signed_left_speed_mps is not None else sample.left_speed_abs_mps for sample in samples), dtype=float)
                right = np.fromiter((sample.signed_right_speed_mps if sample.signed_right_speed_mps is not None else sample.right_speed_abs_mps for sample in samples), dtype=float)
                if len(x) > 1:
                    gaps = np.flatnonzero(np.diff(x) > 1.0) + 1
                    x = np.insert(x, gaps, np.nan)
                    left = np.insert(left, gaps, np.nan)
                    right = np.insert(right, gaps, np.nan)
                self.left_curve.setData(x, left, connect="finite")
                self.right_curve.setData(x, right, connect="finite")
            else:
                self.left_curve.setData([], [])
                self.right_curve.setData([], [])
            self._last_plot_update = now

    def _show_automation(self):
        from .automation_panel import AutomationPanel
        panel = self._tool_windows.get("automation")
        if panel is None:
            panel = AutomationPanel(self.controller.automation_service, self)
            self._tool_windows["automation"] = panel
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def closeEvent(self, event) -> None:
        if not self._closing and not self._close_ready:
            self.connection_interacted.emit()
        if self._close_ready or self.controller.shutdown():
            self._close_retry_timer.stop()
            event.accept()
        else:
            self._closing = True
            self.statusBar().showMessage("正在关闭：后台通信与录制保存完成后将自动退出。")
            self.refresh_state()
            if not self._close_retry_timer.isActive():
                self._close_retry_timer.start()
            event.ignore()

    def _retry_close(self) -> None:
        if self.controller.shutdown():
            self._close_ready = True
            self._close_retry_timer.stop()
            self.close()
