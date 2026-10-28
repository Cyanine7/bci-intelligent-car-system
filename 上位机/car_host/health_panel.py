"""Read-only receive timing and software stop evidence; never sends commands."""

import json
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog, QLabel, QPlainTextEdit, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QHeaderView)


class CommunicationHealthPanel(QDialog):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.setWindowTitle("通信健康与软件停止观测")
        self.resize(880, 660)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        note = QLabel("最近60秒的本机接收观测。采集年龄用于状态时效，查询往返时间另计。\n"
                      "停止时刻分别表示软件失能、PWM归零和编码器零反馈；机械停稳须现场核对。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.stops = QTableWidget(0, 8)
        self.stops.setHorizontalHeaderLabels(["请求", "排队单调时间s", "ACK处理 ms", "首失能/PWM0 ms", "首零反馈 ms",
                                              "连续零反馈 ms", "命令序号", "观测状态"])
        self.stops.setToolTip("所有ms相对排队时间。ACK列为Host处理确认时刻，反馈列为底层采集时刻；不替代机械停稳。")
        self.stops.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stops.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.stops.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.stops, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(QLabel("各层延迟、窗口覆盖与丢弃诊断"))
        layout.addWidget(self.details, 2)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        snapshot = self.host.communication_health()
        sample = self.host.latest
        age = f"{max(0, (time.monotonic() - sample.received_monotonic) * 1000):.1f} ms" if sample else "无有效STATE"
        self.summary.setText(f"{self.host.connection_status} · STATE采集年龄 {age}\n"
            f"{self.host.profile_status} · " + ("接收积压超预算" if snapshot["io_backlog_exceeded"] else "本轮接收无超预算积压"))
        stops = snapshot.get("stops", [])
        self.stops.setRowCount(len(stops))
        for row, stop in enumerate(stops):
            start = stop["queued_monotonic"]
            def elapsed(key):
                value = stop.get(key)
                return f"{(value - start) * 1000:.1f}" if value is not None else "未观测"
            status = "已连续零反馈" if stop.get("continuous_zero_monotonic") is not None else "未观测充分"
            if stop.get("observation_status") == "superseded":
                status = "后续动作已结束此观测"
            values = [stop["request_id"][:8], f"{start:.6f}", elapsed("ack_accepted_monotonic"),
                elapsed("first_disarmed_monotonic"), elapsed("first_zero_monotonic"),
                elapsed("continuous_zero_monotonic"), str(stop["seq"]), status]
            for col, value in enumerate(values):
                self.stops.setItem(row, col, QTableWidgetItem(value))
        details = {key: value for key, value in snapshot.items() if key not in ("stops", "last_batch")}
        self.details.setPlainText(json.dumps(details, ensure_ascii=False, indent=2))
