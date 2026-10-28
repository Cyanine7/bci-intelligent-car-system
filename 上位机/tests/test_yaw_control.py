"""必要核对：课程数值、时间边界与假设备完整界面流程。"""
from pathlib import Path
from dataclasses import replace
import json
import pytest
from car_host.yaw_analysis import design
from car_host.yaw_models import YawConfig,validate_segments
from test_speed_control import speed_host,confirm

def test_course_calculation_and_duration_boundaries():
    cfg,ctx=design()
    assert ctx['tau_v_s']==pytest.approx(.05982522275724787)
    assert cfg.kp==pytest.approx(.269598536858538) and cfg.ki==pytest.approx(2.1422925162150044)
    assert ctx['tv_denominator']==pytest.approx([1,40.00033235384933,839.9067892799715])
    assert YawConfig.unpack(cfg.pack())==cfg
    assert validate_segments([dict(v=0,target=.3,duration_ms=5000),dict(v=0,target=-.3,duration_ms=5000)])
    with pytest.raises(ValueError):validate_segments([dict(v=.1,target=0,duration_ms=5010),dict(v=.1,target=.3,duration_ms=4990)])

def test_calibration_finishes_without_manual_status_read_and_keeps_arm_ready(speed_host,qapp):
    from car_host.yaw_panel import YawPanel
    h,advance,_=speed_host;s=h.yaw_control;p=YawPanel(h);p.timer.stop()
    try:
        confirm(h,advance,s.query_capabilities)
        confirm(h,advance,lambda:s.apply_config(p.config()))
        p.refresh();p.buttons['calibrate'].click();advance()
        p.refresh();assert not p.buttons['arm'].isEnabled()
        # 按用户实际操作，只等校准完成，不手动读取状态。
        for _ in range(65):advance()
        p.refresh();assert s.imu['calibration']==2 and p.buttons['arm'].isEnabled()
        for _ in range(60):advance()
        p.refresh();assert p.buttons['arm'].isEnabled()
        # STATE仍新鲜而角速度遥测中断时，按钮与服务应同时拒绝使能。
        report=h.worker.yaw.report;h.worker.yaw.report=lambda now:None
        for _ in range(15):advance()
        p.refresh();assert h.project_state_fresh and not p.buttons['arm'].isEnabled()
        assert not s.arm().success
        h.worker.yaw.report=report;advance();p.refresh();assert p.buttons['arm'].isEnabled()
        assert not h.latest.armed  # 自动跟踪校准不自动使能。
        p.buttons['arm'].click();advance();p.refresh();assert h.latest.armed and h.latest.control_mode==5
    finally:p.timer.stop();p.close();qapp.processEvents()

def test_calibration_status_timeout_does_not_retry_or_infer_completion_from_telemetry(speed_host):
    h,advance,_=speed_host;s=h.yaw_control
    confirm(h,advance,s.query_capabilities);confirm(h,advance,lambda:s.apply_config(YawConfig()))
    confirm(h,advance,s.calibrate);h.worker.faults['missingack']=23
    for _ in range(85):advance()
    count=sum(m['command']=='yaw_status' for m in h.command_metadata.values())
    assert count==1 and not s.arm().success
    for _ in range(40):advance()
    assert sum(m['command']=='yaw_status' for m in h.command_metadata.values())==count
    assert not h.latest.armed
    h.worker.faults.pop('missingack');confirm(h,advance,s.read_status)
    assert s.imu['calibration']==2

def test_fake_full_capture_ui_gate_and_export(speed_host,qapp):
    from car_host.ui import MainWindow
    from car_host.preferences import AppPreferences
    h,advance,clock=speed_host;s=h.yaw_control
    window=MainWindow(h,preferences=replace(AppPreferences(),auto_connect=False));window.show()
    window.workspace_tabs.setCurrentIndex(2);qapp.processEvents();p=window.yaw_panel;p.timer.stop()
    assets=Path(__file__).resolve().parents[1]/'docs/assets';assets.mkdir(parents=True,exist_ok=True)
    try:
        assert window.workspace_tabs.tabText(2)=='角速度环实验'
        assert [d.value() for v,t,d in p.stage_boxes]==[2000,2000]
        assert [d.maximum() for v,t,d in p.stage_boxes]==[5000,5000]
        p.refresh();assert not p.buttons['apply'].isEnabled() and not p.buttons['run'].isEnabled()
        assert not s.arm().success
        confirm(h,advance,s.query_capabilities);p.refresh();assert p.buttons['apply'].isEnabled()
        # 手动Tv与固定预测时延随本轮冻结并用于停车后重算。
        p.tv_source.setCurrentIndex(2);p.tv_tau.setValue(.08);p.calculate()
        cfg=p.config();confirm(h,advance,lambda:s.apply_config(cfg));assert s.config.pack()==cfg.pack()
        confirm(h,advance,s.calibrate)
        for _ in range(43):advance()
        confirm(h,advance,s.read_status);assert s.imu['calibration']==2
        p.refresh();assert p.buttons['arm'].isEnabled()
        p.kp.setValue(cfg.kp+.001);p.refresh();assert not p.buttons['arm'].isEnabled()
        p.kp.setValue(cfg.kp)
        p.preview();assert set(p.predictions)=={'一阶等效','当前Tv','双轮独立','10ms离散双轮','含时延离散双轮'}
        confirm(h,advance,s.arm)
        stages=[dict(v=.1,target=0,duration_ms=5000),dict(v=.1,target=.3,duration_ms=5000)]
        confirm(h,advance,lambda:s.run_step(stages,1,cfg))
        p.refresh();assert not p.tv_source.isEnabled()
        for _ in range(206):advance()
        assert not h.latest.armed
        for _ in range(180):
            advance()
            if not s.result_fetching:break
        assert s.complete and s.saved_directory and not s.result_fetching,s.status
        assert s.info['count']>1000 and s.info['count']<=1040
        summary=json.loads((s.saved_directory/'summary.json').read_text(encoding='utf-8'))
        assert summary['complete'] and summary['source']=='SIMULATION_PROJECT_V1'
        assert summary['context']['context']['prediction_options']==dict(tv_override=dict(order=1,tau_v_s=.08),feedback_delay_s=.005)
        assert summary['predictions']['含时延离散双轮']['feedback_delay_s']==.005
        assert summary['analysis']['theory']['gain_scale']==1.
        assert 'five_point' in summary['analysis']
        p.refresh()
        assert [curve.name() for curve in p.plots[0].listDataItems()]==['角速度目标 / 开环几何值','五点平滑']
        assert all(plot.minimumHeight()==400 for plot in p.plots)
        for word in ('超调 Mp=','静差 ess=','调节时间 ts（2%）=','上升时间 tr','原始IMU','第2段'):
            assert word in p.metrics.toPlainText()
        assert summary['analysis']['stages'][1]['five_point']['settling_time_s'] is not None
        # 下一次默认2s+2s实验不能沿用上一次结果，结束后同样自动出指标。
        previous_run=s.info['run'];previous_directory=s.saved_directory
        confirm(h,advance,s.arm)
        stages=[dict(v=.1,target=0,duration_ms=2000),dict(v=.1,target=.3,duration_ms=2000)]
        confirm(h,advance,lambda:s.run_step(stages,1,cfg));p.refresh()
        assert not s.samples and s.info is None and '正在运行' in p.metrics.toPlainText()
        for _ in range(180):
            advance()
            if not s.result_fetching:break
        assert s.complete and s.info['run']!=previous_run and s.saved_directory!=previous_directory
        p.refresh();assert f'run={s.info["run"]}' in p.metrics.toPlainText()
        before=[x.pack() for x in s.samples.values()];p.refresh()
        assert [x.pack() for x in s.samples.values()]==before
        for width,height in ((1000,720),(1320,900)):
            window.resize(width,height)
            for tab,name in ((1,'outer_pi'),(2,'controls'),(3,'curves')):
                p.tabs.setCurrentIndex(tab);qapp.processEvents()
                assert window.grab().save(str(assets/f'yaw_{name}_{width}.png'))
        window.connection_shortcut.click();assert window.workspace_tabs.currentIndex()==3
        h.worker.faults['yaw_missing_index']=0
        assert s.download().success
        for _ in range(55):advance()
        assert not s.download_running and not s.complete and set(s.samples)==set(range(1,8))
        partial=s.export();assert not json.loads((partial/'summary.json').read_text(encoding='utf-8'))['complete']
    finally:
        p.timer.stop();window.close();qapp.processEvents()


def test_fixed_direction_course_gain_manual_tv_and_ram_copy(speed_host,qapp):
    from car_host.yaw_panel import YawPanel
    h,advance,_=speed_host;p=YawPanel(h);p.timer.stop()
    try:
        original,_=design()
        assert p.config().kp==pytest.approx(original.kp)
        assert p.config().ki==pytest.approx(original.ki)
        assert p.config().sign==1
        assert not hasattr(p,'delay_correction') and 'rollback' not in p.buttons
        confirm(h,advance,h.yaw_control.query_capabilities)
        actual=replace(p.config(),sign=-1,kp=.19,ki=1.37)
        confirm(h,advance,lambda:h.yaw_control.apply_config(actual))
        p.copy_config()
        assert p.config()==replace(actual,sign=1)
        assert not p.match()  # RAM方向不符时必须重新应用固定方向。
        p.tv_source.setCurrentIndex(1);p.tv_a.setValue(45.);p.tv_b.setValue(900.);p.calculate()
        assert p.design_context['tv_denominator']==[1.,45.,900.]
        assert '900 / (s² + 45 s + 900)' in p.tv_text.text()
        p.tv_source.setCurrentIndex(2);p.tv_tau.setValue(.09);p.calculate()
        assert p.design_context['tv_denominator']==[.09,1.] and '手动一阶' in p.tv_text.text()
        p.tv_tau.setValue(-.1)
        with pytest.raises(ValueError):p.calculate()
        assert p._stale and not p.match()
        p.tv_source.setCurrentIndex(0);p.calculate()
        assert p.design_context['tau_v_s']==pytest.approx(.05982522275724787)
    finally:p.close();qapp.processEvents()
