"""批准实验表与阶段进度的只读窗口；STOP 始终通过协调层。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QHeaderView)


PHASE_NAMES = {
    "idle": "等待实验表与阶段批准", "reading_parameters": "读取实际 RAM 参数",
    "journal_ready": "准备实验记录", "initial_settle": "等待初始零反馈",
    "arm_wait": "等待使能确认", "pwm_wait": "等待 PWM 确认", "moving": "有限实验运行中",
    "trial_stop_wait": "等待主动停止确认", "trial_settle": "等待连续 1 秒零反馈",
    "stopping": "已取消后续实验，正在请求停止", "finalizing": "保存原始记录",
    "reporting": "生成实验报告", "completed": "本阶段已完成，等待下一次批准",
    "aborted": "本阶段已中止", "failed": "本阶段记录失败",
}


def progress_text(service):
    state = service.status()
    prefix = "纯软件假设备 · " if state["demo"] else ""
    text = prefix + PHASE_NAMES.get(state["phase"], state["phase"])
    if state["run_id"]:
        text += f" · {state['completed_trials']}/{state['trial_count']} 次"
        if state["current_trial_id"]:
            text += " · 当前 " + state["current_trial_id"]
    if state["reason"]:
        text += "\n" + state["reason"]
    return text


class AutomationPanel(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("分阶段开环实验")
        self.resize(850, 580)
        layout = QVBoxLayout(self)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        note = QLabel("实验表在聊天中逐阶段批准。运行时请在旁观察，发现异常可立即 STOP。\n"
                      "每次零反馈持续 1 秒仅为软件继续条件，物理轮向与停稳另行记录。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.plan_label = QLabel("尚未收到实验表")
        self.plan_label.setWordWrap(True)
        layout.addWidget(self.plan_label)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["阶段", "试次", "左 PWM", "右 PWM", "时长 ms", "STOP ms", "说明"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.stop_button = QPushButton("STOP · 取消后续实验并停止")
        self.stop_button.clicked.connect(lambda: service.stop("实验窗口人工 STOP"))
        layout.addWidget(self.stop_button)
        self.artifact_label = QLabel()
        self.artifact_label.setWordWrap(True)
        self.artifact_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.artifact_label)
        self._last_plan_id = None
        service.host.changed.connect(self.refresh)
        service.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        s = self.service
        self.status_label.setText(progress_text(s))
        self.stop_button.setEnabled(s.host.connected or s.active)
        plan = s._run["plan"] if s.active and s._run else s._plan
        if plan and plan["plan_id"] != self._last_plan_id:
            self._last_plan_id = plan["plan_id"]
            self.plan_label.setText(f"{plan['name']} · {plan['trial_count']} 次\n"
                                    f"批准版本 {plan['plan_id']}\n摘要 {plan['digest']}")
            self.table.setRowCount(plan["trial_count"])
            row = 0
            for stage in plan["stages"]:
                for trial in stage["trials"]:
                    values = [stage["name"], trial["trial_id"], trial["left_pwm"], trial["right_pwm"],
                              trial["duration_ms"], trial.get("stop_after_ms") or "到期", trial["label"]]
                    for col, value in enumerate(values):
                        self.table.setItem(row, col, QTableWidgetItem(str(value)))
                    row += 1
        if s._journal:
            snap = s._journal.snapshot()
            self.artifact_label.setText("记录目录：" + str(s.host.recorder.directory or "") +
                                       ("\n报告已保存：" + snap.get("artifacts", {}).get("report", "")
                                        if snap.get("finished") and not snap.get("failure") else ""))
