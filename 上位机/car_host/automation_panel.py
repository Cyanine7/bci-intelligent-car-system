"""Editable bounded plans, frozen previews and local runs through the service."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QHeaderView, QHBoxLayout, QPlainTextEdit,
    QLineEdit, QCheckBox, QComboBox, QFileDialog, QTabWidget)

from .experiment_models import get_templates, plan_to_draft


PHASE_NAMES = {
    "idle": "等待冻结实验表", "reading_parameters": "读取实际 RAM 参数",
    "journal_ready": "准备实验记录", "initial_settle": "等待初始零反馈",
    "arm_wait": "等待使能确认", "pwm_wait": "等待 PWM 确认", "moving": "有限实验运行中",
    "trial_stop_wait": "等待主动停止确认", "trial_settle": "等待连续 1 秒零反馈",
    "resting": "时间块间有界静置（不等于冷机）",
    "stopping": "已取消后续实验，正在请求停止", "finalizing": "保存原始记录",
    "reporting": "生成实验报告", "completed": "冻结实验已完成",
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
        self.setWindowTitle("电机测量工作台")
        self.resize(1050, 820)
        layout = QVBoxLayout(self)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        note = QLabel("填写实际任务授权并预览冻结表后，运行整表；已有完整测量授权可沿用，不扩展其边界。\n"
                      "每次零反馈持续 1 秒仅为软件继续条件，物理轮向与停稳另行记录。")
        note.setWordWrap(True)
        layout.addWidget(note)
        toolbar = QHBoxLayout()
        self.template_combo = QComboBox()
        self.template_combo.setPlaceholderText("选择草稿模板")
        self._templates = get_templates()
        for template in self._templates:
            self.template_combo.addItem(template["name"])
        self.template_button = QPushButton("加载草稿模板")
        self.template_button.clicked.connect(self.load_template)
        self.import_button = QPushButton("导入JSON")
        self.import_button.clicked.connect(self.import_draft)
        self.export_button = QPushButton("导出草稿")
        self.export_button.clicked.connect(self.export_draft)
        self.sync_table_button = QPushButton("从JSON更新编辑表格")
        self.sync_table_button.clicked.connect(self.sync_table)
        for widget in (self.template_combo, self.template_button, self.import_button, self.export_button, self.sync_table_button):
            toolbar.addWidget(widget)
        layout.addLayout(toolbar)
        options = QHBoxLayout()
        options.addWidget(QLabel("套件ID"))
        self.suite_edit = QLineEdit("measurement_" + uuid4().hex[:8])
        options.addWidget(self.suite_edit)
        options.addWidget(QLabel("总进入预算（必须填写）"))
        self.budget_edit = QLineEdit()
        self.budget_edit.setPlaceholderText("1..4096，非默认运动数量")
        options.addWidget(self.budget_edit)
        layout.addLayout(options)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("编辑JSON草稿：阶段/时间块、静置、左右PWM、两段时长、重复、对照类型")
        self.editor.setMinimumHeight(150)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.editor, "JSON草稿")
        self.draft_table = QTableWidget(0, 14)
        self.draft_table.setHorizontalHeaderLabels(["阶段ID", "阶段名称", "时间块", "静置ms",
            "首段左PWM", "首段右PWM", "首段/固定ms", "第二段左PWM", "第二段右PWM", "第二段ms",
            "重复", "STOP ms", "对照类型", "说明"])
        self.draft_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.draft_table.itemChanged.connect(self.edit_table)
        self.tabs.addTab(self.draft_table, "表格编辑")
        self.results_view = QPlainTextEdit()
        self.results_view.setReadOnly(True)
        layout.addWidget(self.tabs, 1)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        actions = QHBoxLayout()
        self.preview_button = QPushButton("校验并预览冻结表")
        self.preview_button.clicked.connect(self.preview_draft)
        self.query_caps_button = QPushButton("查询实际分段能力（不使能）")
        self.query_caps_button.clicked.connect(self.query_caps)
        self.run_button = QPushButton("运行冻结整表")
        self.run_button.clicked.connect(self.run_plan)
        self.stage_combo = QComboBox()
        self.stage_button = QPushButton("运行选中阶段")
        self.stage_button.clicked.connect(self.run_stage)
        for widget in (self.preview_button, self.query_caps_button, self.stage_combo, self.stage_button, self.run_button):
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.approval_edit = QLineEdit()
        self.approval_edit.setPlaceholderText("记录人的实际授权原话；已有任务授权可沿用，不得虚构确认")
        layout.addWidget(self.approval_edit)
        self.ready_check = QCheckBox("现场已架空、准备完成，并在实际授权范围内")
        layout.addWidget(self.ready_check)
        self.plan_label = QLabel("尚未收到实验表")
        self.plan_label.setWordWrap(True)
        layout.addWidget(self.plan_label)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(["阶段/时间块", "试次", "左 PWM", "右 PWM", "总时长 ms", "STOP ms", "说明", "两段 PWM/时长", "对照", "静置 ms"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.tabs.addTab(self.table, "冻结顺序预览")
        self.tabs.addTab(self.results_view, "结果与校核")
        results_actions = QHBoxLayout()
        self.health_button = QPushButton("通信健康")
        self.health_button.clicked.connect(self.show_health)
        self.results_button = QPushButton("查看运行结果")
        self.results_button.clicked.connect(self.show_results)
        self.recompute_button = QPushButton("独立复算校核")
        self.recompute_button.clicked.connect(self.recompute)
        self.makeup_button = QPushButton("生成补测草稿")
        self.makeup_button.clicked.connect(self.makeup)
        self.refine_side = QComboBox()
        for label, value in (("左正向", ("left", 1)), ("左反向", ("left", -1)),
                             ("右正向", ("right", 1)), ("右反向", ("right", -1))):
            self.refine_side.addItem(label, value)
        self.refine_button = QPushButton("生成加密草稿")
        self.refine_button.clicked.connect(self.refine)
        for widget in (self.health_button, self.results_button, self.recompute_button, self.makeup_button, self.refine_side, self.refine_button):
            results_actions.addWidget(widget)
        layout.addLayout(results_actions)
        self.stop_button = QPushButton("STOP · 取消后续实验并停止")
        self.stop_button.clicked.connect(lambda: service.stop("实验窗口人工 STOP"))
        layout.addWidget(self.stop_button)
        self.artifact_label = QLabel()
        self.artifact_label.setWordWrap(True)
        self.artifact_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.artifact_label)
        self._last_plan_id = None
        self._loading_editor = False
        self._table_draft = None
        self._table_rows = []
        self._table_error = ""
        self.editor.textChanged.connect(self.invalidate)
        self.budget_edit.textChanged.connect(self.invalidate)
        self.suite_edit.textChanged.connect(self.invalidate)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(100)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        service.changed.connect(self.refresh)
        self.refresh()

    def invalidate(self):
        if not self._loading_editor:
            self._table_error = ""
            self.service.invalidate_plan()
            self.error_label.setText("草稿已改变；旧冻结版本失效，须重新校验预览。")
            self.refresh()

    def set_draft(self, draft):
        self._loading_editor = True
        try:
            self.editor.setPlainText(json.dumps(draft, ensure_ascii=False, indent=2))
            if draft.get("suite"):
                self.suite_edit.setText(str(draft["suite"].get("suite_id", "")))
                value = draft["suite"].get("total_entered_budget")
                self.budget_edit.setText(str(value) if type(value) is int else "")
        finally:
            self._loading_editor = False
        self.invalidate()
        self.sync_table()
        self.template_combo.setCurrentIndex(-1)

    def sync_table(self):
        try:
            draft = json.loads(self.editor.toPlainText())
            if "digest" in draft:
                draft = plan_to_draft(draft)
            stages = draft.get("stages")
            if not isinstance(stages, list) or not 1 <= len(stages) <= 16:
                raise ValueError("编辑表格需要1..16阶段")
            rows = [(i, j) for i, s in enumerate(stages) for j, _ in enumerate(s["steps"])]
            if len(rows) > 128:
                raise ValueError("编辑表格最多128步骤")
            self._table_draft, self._table_rows = deepcopy(draft), rows
            self._table_error = ""
            self._populate_table()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self._table_error = str(exc)
            self.error_label.setText("编辑表格：" + str(exc))

    def _populate_table(self):
        self.draft_table.blockSignals(True)
        try:
            self.draft_table.setRowCount(len(self._table_rows))
            for row, (si, ti) in enumerate(self._table_rows):
                stage = self._table_draft["stages"][si]
                step = stage["steps"][ti]
                segments = step.get("segments")
                first = segments[0] if segments else step
                second = segments[1] if segments else {}
                values = [stage["stage_id"], stage["name"], stage.get("block_id", stage["stage_id"]),
                    stage.get("rest_before_ms", 0), first.get("left_pwm"), first.get("right_pwm"), first.get("duration_ms"),
                    second.get("left_pwm"), second.get("right_pwm"), second.get("duration_ms"), step.get("repetitions", 1),
                    step.get("stop_after_ms"), step.get("control_type", "candidate" if segments else "fixed_pwm"), step.get("label", "")]
                for col, value in enumerate(values):
                    self.draft_table.setItem(row, col, QTableWidgetItem("" if value is None else str(value)))
        finally:
            self.draft_table.blockSignals(False)

    def edit_table(self, item):
        # Every attempted edit invalidates approval, including invalid text.
        self.invalidate()
        try:
            si, ti = self._table_rows[item.row()]
            stage, col, value = self._table_draft["stages"][si], item.column(), item.text()
            step = stage["steps"][ti]
            if col in (3, 4, 5, 6, 7, 8, 9, 10, 11):
                value = int(value) if value.strip() else None
            if col < 4:
                stage[("stage_id", "name", "block_id", "rest_before_ms")[col]] = value
            elif col < 10:
                index, key = (0 if col < 7 else 1), ("left_pwm", "right_pwm", "duration_ms")[(col - 4) % 3]
                if index == 1 and "segments" not in step:
                    step["segments"] = [{k: step.pop(k, None) for k in ("left_pwm", "right_pwm", "duration_ms")},
                                        dict(left_pwm=None, right_pwm=None, duration_ms=None)]
                    step["control_type"] = "candidate"
                (step["segments"][index] if "segments" in step else step)[key] = value
            else:
                step[("repetitions", "stop_after_ms", "control_type", "label")[col - 10]] = value
            self._table_error = ""
            self._loading_editor = True
            self.editor.setPlainText(json.dumps(self._table_draft, ensure_ascii=False, indent=2))
            self._loading_editor = False
            if col < 4:
                self._populate_table()
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            self._loading_editor = False
            self._table_error = "表格输入无效：" + str(exc)
            self.error_label.setText(self._table_error)

    def load_template(self):
        index = self.template_combo.currentIndex()
        if index < 0:
            self.error_label.setText("先选择草稿模板")
            return
        self.set_draft(self._templates[index])
        self.template_combo.setCurrentIndex(index)

    def _draft(self):
        if self._table_error:
            raise ValueError(self._table_error)
        if len(self.editor.toPlainText().encode("utf-8")) > 65536:
            raise ValueError("草稿超过64KiB")
        draft = json.loads(self.editor.toPlainText())
        if not isinstance(draft, dict):
            raise ValueError("草稿必须为JSON对象")
        if "digest" in draft:
            draft = plan_to_draft(draft)
        budget = self.budget_edit.text().strip()
        if not budget.isascii() or not budget.isdigit() or not 1 <= int(budget) <= 4096:
            raise ValueError("必须明确填写1..4096的套件总进入预算")
        draft["schema_version"] = 2
        draft["suite"] = dict(suite_id=self.suite_edit.text().strip(), total_entered_budget=int(budget))
        return draft

    def preview_draft(self):
        try:
            result = self.service.dispatch_local("preview_plan", {"plan": self._draft()})
            self.error_label.setText(result.get("error", "已冻结；实际能力和健康条件将在运行前复核。"))
            if "error" not in result:
                self.tabs.setCurrentWidget(self.table)
        except (ValueError, TypeError, KeyError) as exc:
            self.error_label.setText(str(exc))
        self.refresh()

    def query_caps(self):
        result = self.service.host.send_command("query_profile_caps")
        self.error_label.setText(result.message)

    def run_plan(self):
        self._start_local("start_plan")

    def run_stage(self):
        self._start_local("start_stage")

    def _start_local(self, method):
        plan = self.service._plan
        if not plan:
            self.error_label.setText("草稿须先校验预览")
            return
        result = self.service.dispatch_local(method, dict(plan_id=plan["plan_id"], stage_id=self.stage_combo.currentData(),
            plan_digest=plan["digest"], approval_text=self.approval_edit.text(),
            ready_confirmed=self.ready_check.isChecked()))
        self.error_label.setText(result.get("error", result.get("message", "")))
        self.refresh()

    def show_health(self):
        from .health_panel import CommunicationHealthPanel
        panel = CommunicationHealthPanel(self.service.host, self)
        panel.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        panel.show()

    def show_results(self):
        result = self.service.dispatch_local("get_results", {})
        self.results_view.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        self.tabs.setCurrentWidget(self.results_view)

    def recompute(self):
        from .measurement_analysis import recompute_run
        if self.service.active:
            self.error_label.setText("等待原始记录与报告保存完成再复算")
            return
        directory = self.service.host.recorder.directory
        if directory is None:
            self.error_label.setText("尚无运行记录")
            return
        try:
            # Offline analysis can be large; it never runs in the RX/UI callback.
            import threading
            self.recompute_button.setEnabled(False)
            self._recompute_result = None
            def work():
                try:
                    result = recompute_run(directory)
                except Exception as exc:
                    result = {"verified": False, "error": str(exc)}
                self._recompute_result = result
            self._recompute_thread = threading.Thread(target=work, daemon=True)
            self._recompute_thread.start()
            self.error_label.setText("正在后台独立解码与复算")
        except (OSError, ValueError) as exc:
            self.error_label.setText(str(exc))

    def makeup(self):
        from .measurement_suite import makeup_draft, read_suite
        s = self.service
        if s.active or not s._run or not s._journal:
            self.error_label.setText("等待原始记录与报告保存后生成补测草稿")
            return
        try:
            run = s._run
            ledger = read_suite(run["suite_directory"]) if run.get("suite_directory") else None
            result = makeup_draft(run["plan"], run["run_id"], run["trials"],
                                  ledger["remaining_budget"] if ledger else 0)
            self.results_view.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("draft"):
                self.set_draft(result["draft"])
                self.tabs.setCurrentWidget(self.draft_table)
            else:
                self.tabs.setCurrentWidget(self.results_view)
            self.error_label.setText(result["reason"])
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.error_label.setText(str(exc))

    def refine(self):
        side, direction = self.refine_side.currentData()
        result = self.service.dispatch_local("suggest_refinement", dict(side=side, direction=direction))
        self.results_view.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("draft"):
            self.set_draft(result["draft"])
            self.tabs.setCurrentWidget(self.draft_table)
        else:
            self.tabs.setCurrentWidget(self.results_view)
        self.error_label.setText(result.get("error", result.get("reason", "")))

    def import_draft(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入有界测量草稿", "", "JSON (*.json)")
        if not path:
            return
        try:
            file = Path(path)
            if file.stat().st_size > 65536:
                raise ValueError("导入文件超过64KiB")
            draft = json.loads(file.read_text(encoding="utf-8-sig"))
            if "digest" in draft:
                draft = plan_to_draft(draft)
            self.set_draft(draft)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.error_label.setText(str(exc))

    def export_draft(self):
        try:
            draft = self._draft()
        except (ValueError, TypeError, KeyError) as exc:
            self.error_label.setText(str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存测量草稿", "measurement-draft.json", "JSON (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.error_label.setText("草稿已保存；保存不等于冻结或运动。")
            except OSError as exc:
                self.error_label.setText(str(exc))

    def closeEvent(self, event):
        state = self.service.status()
        if state["active"] and state.get("owner_kind") == "local_ui":
            self.service.stop("关闭本地测量窗口")
        super().closeEvent(event)

    def refresh(self):
        s = self.service
        self.status_label.setText(progress_text(s))
        self.stop_button.setEnabled(s.host.connected or s.active)
        for widget in (self.editor, self.template_button, self.import_button, self.template_combo,
                       self.budget_edit, self.suite_edit, self.preview_button, self.approval_edit, self.ready_check,
                       self.draft_table, self.sync_table_button, self.stage_combo):
            widget.setEnabled(not s.active)
        self.run_button.setEnabled(not s.active and s._plan is not None and not s._plan_used)
        self.stage_button.setEnabled(self.run_button.isEnabled())
        self.refine_button.setEnabled(not s.active and s._journal is not None)
        self.makeup_button.setEnabled(not s.active and s._journal is not None)
        pending = getattr(self, "_recompute_thread", None)
        recomputing = pending is not None and pending.is_alive()
        self.recompute_button.setEnabled(not s.active and s._journal is not None and not recomputing)
        if getattr(self, "_recompute_result", None) is not None:
            self.results_view.setPlainText(json.dumps(self._recompute_result, ensure_ascii=False, indent=2))
            self.tabs.setCurrentWidget(self.results_view)
            self.error_label.setText("独立校核通过" if self._recompute_result.get("verified") else "校核失败；保留原始数据与差异，结论需复查")
            self._recompute_result = None
        self.query_caps_button.setEnabled(not s.active and s.host.connected and s.host.project_state_fresh
            and not s.host.latest.armed and not s.host.project_request_pending)
        plan = s._run["plan"] if s.active and s._run else s._plan
        if plan and plan["plan_id"] != self._last_plan_id:
            self._last_plan_id = plan["plan_id"]
            self.stage_combo.clear()
            for stage in plan["stages"]:
                self.stage_combo.addItem(stage["name"], stage["stage_id"])
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
                    segments = trial.get("segments", [])
                    description = " → ".join(f"{x['left_pwm']}/{x['right_pwm']} × {x['duration_ms']} ms" for x in segments)
                    self.table.setItem(row, 7, QTableWidgetItem(description or "固定PWM"))
                    self.table.setItem(row, 8, QTableWidgetItem(str(trial.get("control_type", "fixed_pwm"))))
                    self.table.setItem(row, 9, QTableWidgetItem(str(stage.get("rest_before_ms", 0))))
                    row += 1
        elif plan is None:
            self._last_plan_id = None
            self.table.setRowCount(0)
            self.plan_label.setText("尚未冻结实验表")
        if s._journal:
            snap = s._journal.snapshot()
            self.artifact_label.setText("记录目录：" + str(s.host.recorder.directory or "") +
                                       ("\n报告已保存：" + snap.get("artifacts", {}).get("report", "")
                                        if snap.get("finished") and not snap.get("failure") else ""))
