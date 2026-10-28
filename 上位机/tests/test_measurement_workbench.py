"""Native workbench edits and runs share the frozen service; no device access."""

import json
import time

from car_host.automation_panel import AutomationPanel
from car_host.project_protocol import GET_PROFILE_CAPS, SET_PWM_PROFILE, STOP
from test_automation import automation, fake_host, command_kinds, draft, drive


def test_table_edits_invalidate_frozen_plan_and_update_exportable_draft(automation):
    host, clock, service = automation
    panel = AutomationPanel(service)
    try:
        panel.budget_edit.setText("10")
        panel.set_draft(draft(repetitions=1))
        panel.preview_draft()
        old_id = service._plan["plan_id"]
        assert panel.table.rowCount() == 1
        panel.draft_table.item(0, 4).setText("1300")
        assert service._plan is None and not panel.run_button.isEnabled()
        assert panel._draft()["stages"][0]["steps"][0]["left_pwm"] == 1300
        panel.preview_draft()
        assert service._plan["plan_id"] != old_id
        assert panel.table.item(0, 2).text() == "1300"
        panel.draft_table.item(0, 4).setText("invalid")
        panel.preview_draft()
        assert service._plan is None and "无效" in panel.error_label.text()
        panel.draft_table.item(0, 4).setText("1250")
        panel.preview_draft()
        assert service._plan["stages"][0]["trials"][0]["left_pwm"] == 1250
    finally:
        panel.close()


def test_profile_template_table_preparation_queries_only_explicit_profile_preview(automation):
    host, clock, service = automation
    panel = AutomationPanel(service)
    try:
        panel.set_draft(dict(schema_version=2, name="分段编辑", suite=dict(suite_id="edit", total_entered_budget=3),
            stages=[dict(stage_id="one", name="一", steps=[dict(segments=[
                dict(left_pwm=None, right_pwm=0, duration_ms=400),
                dict(left_pwm=None, right_pwm=0, duration_ms=600)], repetitions=1, control_type="candidate")])]))
        panel.preview_draft()
        assert service._plan is None and GET_PROFILE_CAPS not in command_kinds(host.worker)
        panel.draft_table.item(0, 4).setText("1400")
        panel.draft_table.item(0, 7).setText("1000")
        panel.preview_draft()
        assert service._plan is not None
        assert command_kinds(host.worker).count(GET_PROFILE_CAPS) == 1
        assert not host.latest.armed and SET_PWM_PROFILE not in command_kinds(host.worker)
        host.poll()
        host.poll()
        assert host.capabilities.pwm_profile
    finally:
        panel.close()


def test_native_single_stage_records_exact_authorization_and_health_decisions(automation):
    host, clock, service = automation
    panel = AutomationPanel(service)
    try:
        panel.budget_edit.setText("4")
        panel.set_draft(draft(repetitions=1))
        panel.preview_draft()
        words = "  软件测试：批准此冻结版本并已就绪。  "
        panel.approval_edit.setText(words)
        panel.ready_check.setChecked(True)
        panel.run_stage()
        assert service._run["owner_kind"] == "local_ui" and not service._run["whole_plan"]
        service.client_lost("local_ui")
        drive(automation, lambda: not service.active, heartbeat=False)
        assert service.status()["phase"] == "completed", service.status()
        directory = host.recorder.directory
        context = json.loads((directory / "context.json").read_text(encoding="utf-8"))
        assert context["approval_text"] == words
        assert context["approval_scope"]["planned_trials"] == 1
        assert context["approval_scope"]["stage_ids"] == ["stage1"]
        trace = [json.loads(x) for x in (directory / "receive_trace.jsonl").read_text(encoding="utf-8").splitlines()]
        decisions = [x for x in trace if x.get("event") == "health_check"]
        assert decisions and all(x["state_age_ms"] < 500 and not x["problem"] for x in decisions)
        panel.show_results()
        assert '"finished": true' in panel.results_view.toPlainText()
        panel.recompute()
        deadline = time.perf_counter() + 2
        while panel._recompute_thread.is_alive() and time.perf_counter() < deadline:
            time.sleep(.002)
        panel.refresh()
        assert "独立校核通过" in panel.error_label.text()
        assert json.loads((directory / "recompute.json").read_text(encoding="utf-8"))["verified"]
    finally:
        panel.close()


def test_closing_native_workbench_cancels_all_following_trials(automation):
    host, clock, service = automation
    panel = AutomationPanel(service)
    panel.budget_edit.setText("8")
    panel.set_draft(draft(repetitions=3))
    panel.preview_draft()
    panel.approval_edit.setText("软件测试批准整表，已就绪")
    panel.ready_check.setChecked(True)
    panel.run_plan()
    drive(automation, lambda: service.status()["phase"] == "moving", heartbeat=False)
    panel.close()
    drive(automation, lambda: not service.active, heartbeat=False)
    assert service.status()["phase"] == "aborted"
    assert service.status()["entered_trials"] == 1
    assert command_kinds(host.worker).count(STOP) == 1
