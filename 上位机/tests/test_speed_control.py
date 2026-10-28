import math
import json
import struct
from dataclasses import replace
import pytest
from car_host.motor_models import MotorModel,MotorModelStore,DEFAULTS
from car_host.speed_control_models import *
from car_host.speed_control_analysis import (conclude_pi,design_pair,predict,ReferenceController,
    analyze,DEFAULT_MP,step_range,rolling_deadzone,pole_match_pi)
from car_host.controller import HostController
from car_host.project_fake import FAKE_CONFIG

def segments(a,b,first=1500,second=1500,right=True):
    return [dict(left=a,right=a if right else 0,duration_ms=first),
            dict(left=b,right=b if right else 0,duration_ms=second)]

@pytest.mark.parametrize('condition',['空载','负载'])
def test_default_design_and_incremental_reference(condition):
    cfg,rows=design_pair(condition)
    for i,row in enumerate(rows):
        assert row['ki_step']==pytest.approx(row['ki']*.01)
        sim=predict(row['K'],row['tau'],cfg,i)
        assert sim['discrete'][0][4]==int(row['ki']*.01*.1)
        expected=conclude_pi(DEFAULT_MP,.2,row['K'],row['tau'])
        assert cfg.gains[i]==(expected['kp'],expected['ki'])

@pytest.mark.parametrize('mp,ts,K,tau',[(0,.2,1e-4,.1),(1,.2,1e-4,.1),(.1,0,1e-4,.1),
    (.1,.2,float('nan'),.1),(.1,.2,-1,.1),(.1,.2,1e-4,0)])
def test_bad_design_rejected(mp,ts,K,tau):
    with pytest.raises(ValueError):conclude_pi(mp,ts,K,tau)

def test_design_matches_user_formula_and_uses_editable_per_wheel_model():
    row=pole_match_pi(.1,.2,.000065,.095)
    log=math.log(.1);xi=math.sqrt(log**2/(math.pi**2+log**2));wn=4/(xi*.2)
    assert row['kp']==pytest.approx((2*xi*wn*.095-1)/.000065)
    assert row['ki']==pytest.approx(.095*wn**2/.000065)
    model=replace(MotorModel.default(),k_left=.00008,tau_right=.08)
    cfg,rows=design_pair(model=model,mp_ratio=.1)
    sim=predict(model.k_left,model.tau_left,cfg)
    assert rows[0]['kp']==pytest.approx((8*model.tau_left/.2-1)/model.k_left)
    a=pole_match_pi(.05,.2,model.k_left,model.tau_left)
    assert a['kp']==pytest.approx(rows[0]['kp']) and a['ki']!=rows[0]['ki']

def test_incremental_fractional_accumulation_clip_return_and_segment_continuity():
    c=ReferenceController(2,0.,1.,10)
    for _ in range(20):c.step(1,0,.02)
    assert c.previous_output==pytest.approx(.4)
    c=ReferenceController(2,100,2000,100,target_filter=False)
    c.step(1,0,.01);assert c.previous_output==100
    assert c.step(1,.9,.01)[2]==12 # clipped state used, no hidden windup
    previous=c.previous_output
    f,raw,p,_,_=c.step(.8,.9,.01)
    assert raw==pytest.approx(previous-20-2)
    c.step(0,1,.01);assert c.previous_output==c.previous_error==c.filtered==0

def test_binary64_wire_bounds_and_step_fields():
    cfg=SpeedConfig(2,16500,19995.842514629873,702884.1611204201,44048.72133689123,1193376.705723456)
    assert len(cfg.pack())==36 and SAMPLE_STRUCT.size==52
    assert SpeedConfig.unpack(CONFIG_STRUCT.unpack(cfg.pack())).pack()==cfg.pack()
    assert CAPS_STRUCT.size==40 and REPLY_LAYOUTS[SPEED_INFO].size==60
    assert len(encode_speed_command('speed_pwm',dict(segments=segments(0,16500),deadline_tick=3100)))==20
    for values in (segments(0,16501),segments(1,-1),segments(0,1,1501,1499),segments(1,1)):
        with pytest.raises(ValueError):encode_speed_command('speed_pwm',dict(segments=values,deadline_tick=3100))
    with pytest.raises(ValueError):replace(cfg,pwm_limit=16501).pack()

def test_model_store_defaults_roundtrip_and_corruption(tmp_path):
    store=MotorModelStore(tmp_path/'models.json')
    model=replace(MotorModel.default('负载'),name='11.5V',reference_voltage=11.5,k_left=.00007123456789012345)
    store.save(model);read=MotorModelStore(store.path)
    assert read.profiles['负载/11.5V'].snapshot()==model.snapshot()
    assert struct.pack('<d',read.profiles['负载/11.5V'].k_left)==struct.pack('<d',model.k_left)
    with pytest.raises(ValueError):store.save(replace(model,name='默认'))
    store.path.write_text('broken',encoding='utf-8');read.load()
    assert read.warning and not read.profiles and DEFAULTS['负载'][0]!=model.k_left
    with pytest.raises(ValueError):replace(model,tau_left=0).validate()

def test_range_and_deadzone_evidence():
    cfg,_=design_pair('负载')
    assert 0<step_range(cfg,0)['recommended_delta'][1]<=1.18
    assert rolling_deadzone([(1000,.04),(2000,.1)]) is None
    fit=rolling_deadzone([(1000,.04),(2000,.1),(3000,.16)])
    assert fit['pwm_estimate']==pytest.approx(1000/3)

class Clock:
    def __init__(self):
        import time
        self.now=time.monotonic()
    def __call__(self):return self.now

@pytest.fixture
def speed_host(qapp,tmp_path,monkeypatch):
    clock=Clock();monkeypatch.setattr('car_host.controller.time.monotonic',clock)
    def factory(config):
        from car_host.project_fake import ProjectFakeWorker
        return ProjectFakeWorker(config,clock=clock)
    host=HostController(tmp_path,worker_factory=factory);host.timer.stop()
    assert host.connect_device(FAKE_CONFIG);host.poll()
    def advance(ms=50):
        clock.now+=ms/1000
        host.poll()
        qapp.processEvents()
        host.poll() # finish any bounded receive callback that yielded for fairness
    advance()
    yield host,advance,clock
    host.disconnect_device();host.poll();host.shutdown()

def confirm(h,advance,method):
    r=method();assert r.success,r.message
    advance()
    assert h.command_results[r.request_id].status=='accepted',h.command_results[r.request_id]
    return r

def acquire(h,advance,pwm=False,values=None):
    s=h.speed_control
    confirm(h,advance,s.query_capabilities)
    if not pwm:confirm(h,advance,lambda:s.apply_config(design_pair()[0]))
    confirm(h,advance,lambda:s.arm(pwm))
    r=confirm(h,advance,lambda:s.run_step(values or segments(0,100),pwm))
    for _ in range(65):advance()
    assert not h.latest.armed and h.latest.pwm_left==h.latest.pwm_right==0
    confirm(h,advance,s.read_log_info)
    advance() # metadata reply
    assert s.meta and s.info['status']==3
    assert s.download().success
    for _ in range(55):advance()
    assert s.complete,s.status
    return r


def test_five_point_plot_and_analysis_use_identical_speed_samples(speed_host,qapp):
    from car_host.speed_control_panel import SpeedControlPanel
    from car_host.speed_design import response_metrics
    h,advance,_=speed_host;s=h.speed_control
    acquire(h,advance,values=segments(100,200))
    samples=[s.samples[i] for i in sorted(s.samples)]
    before=[v.pack() for v in samples]
    p=SpeedControlPanel(h);p.preview_timer.stop();p.refresh()
    try:
        result=analyze(s.info,s.samples,s.caps,s.meta,dict(mp_ratio=.1,design_ts_s=.2))
        for side,line in zip(('left','right'),p.estimated_lines):
            times,values=line.getData()
            expected=[sum(getattr(v,'measured_'+side) for v in samples[i-4:i+1])/5 for i in range(4,len(samples))]
            assert list(values)==pytest.approx(expected)
            assert list(times)==pytest.approx([(((v.tick-s.meta['switch_tick']+0x80000000)&0xffffffff)-0x80000000)/1000 for v in samples[4:]])
            active=[i for i,v in enumerate(samples[4:]) if v.flags&ACTIVE and v.flags&SECOND]
            row=result['wheels'][side]
            metrics=response_metrics([times[i] for i in active],[values[i] for i in active],row['v0'],row['r1'])
            assert row['smoothed']['overshoot_percent']==pytest.approx(metrics['overshoot_percent'])
            assert row['smoothed']['settling_time_s']==metrics['settling_time_s']
            assert result['comparison']['wheels'][side]['overshoot_percent']==pytest.approx(metrics['overshoot_percent'])
            assert [v['velocity_mps'] for v in result['comparison']['wheels'][side]['estimates']]==pytest.approx([values[i] for i in active])
        assert '五点平滑' in p.metrics.toPlainText()
        assert [v.pack() for v in samples]==before
        # 相同速度在实时缓冲区也绘制五点平均，无须理论设计或下载完成。
        s.samples={};s.info=None;s.meta=None;s.live=[(s.active_run,v) for v in samples]
        p._plot_token=None;p.refresh()
        assert list(p.estimated_lines[0].getData()[1])==pytest.approx([sum(v.measured_left for v in samples[i-4:i+1])/5 for i in range(4,len(samples))])
    finally:p.close()

@pytest.mark.parametrize('pwm',[False,True])
def test_full_three_second_capture_identity_export_and_analysis(speed_host,pwm):
    h,advance,_=speed_host;s=h.speed_control
    assert len(h.worker.sent)==1
    s.context={'model':MotorModel.default().snapshot(),'parameter_source':'manual',
               'design_spec':dict(mp_ratio=.05,design_ts_s=.2)}
    run=acquire(h,advance,pwm,segments(4000,5000) if pwm else segments(100,150))
    assert s.info['run']==h.command_metadata[run.request_id]['seq']
    assert 320<=len(s.samples)<=384 and any(x.flags&SECOND for x in s.samples.values())
    assert max(abs(x.pwm_left) for x in s.samples.values())<=16500
    report=json.loads((s.saved_directory/'summary.json').read_text(encoding='utf-8'))
    assert report['complete'] and report['schema_version']==2
    assert report['design_context']['prediction']['model']['condition']=='空载'
    if not pwm:
        assert report['analysis']['comparison']['theory']==dict(mp_percent=5.,ts_s=.2,band_ratio=.02)
        assert 'discrete_settling_time_s' in report['predictions']['left']
    result=analyze(s.info,s.samples,s.caps,s.meta)
    assert result['valid'],result
    if pwm:
        for side in ('left','right'):
            row=result['wheels'][side]
            assert row['K']==pytest.approx(DEFAULTS['空载'][0 if side=='left' else 2],rel=.02)
            assert row['tau']==pytest.approx(.058 if side=='left' else .057,abs=.012)

def test_unimplemented_closed_refusal_open_available_and_unsupported(speed_host):
    h,advance,_=speed_host;s=h.speed_control;h.worker.faults['speed_unimplemented']=True
    confirm(h,advance,s.query_capabilities)
    before=len(h.worker.sent)
    assert not s.arm().success and not s.apply_config(design_pair()[0]).success
    assert len(h.worker.sent)==before
    confirm(h,advance,lambda:s.arm(True))
    assert h.latest.control_mode==4
    h.motion.stop();advance()
    h.worker.faults['speed_unsupported']=True;r=s.query_capabilities();advance()
    assert h.command_results[r.request_id].status=='rejected' and s.caps is None

@pytest.mark.parametrize('a,b',[(5000,4000),(-4000,-5000),(-5000,-4000)])
def test_open_identification_supports_decreasing_and_negative_steps(speed_host,a,b):
    h,advance,_=speed_host
    acquire(h,advance,True,segments(a,b))
    s=h.speed_control
    result=analyze(s.info,s.samples,s.caps,s.meta)
    assert result['valid']
    for i,side in enumerate(('left','right')):
        row=result['wheels'][side]
        assert row['K']==pytest.approx(DEFAULTS['空载'][i*2],rel=.02)
        assert row['tau']==pytest.approx(DEFAULTS['空载'][i*2+1],abs=.012)

def test_stop_cancels_second_stage_and_download_batches(speed_host):
    h,advance,_=speed_host;s=h.speed_control
    confirm(h,advance,s.query_capabilities);confirm(h,advance,lambda:s.arm(True))
    confirm(h,advance,lambda:s.run_step(segments(1000,16500),True))
    assert h.motion.stop().success;advance()
    for _ in range(5):advance()
    confirm(h,advance,s.read_log_info);advance()
    assert s.info['stop_reason']==1 and s.meta['segment']==0
    assert s.download().success;h.motion.stop();advance();before=len(h.worker.sent)
    for _ in range(5):advance()
    assert not s.download_running and len(h.worker.sent)==before

def test_missing_sample_timeout_does_not_complete_or_resend(speed_host):
    h,advance,_=speed_host;s=h.speed_control
    confirm(h,advance,s.query_capabilities);confirm(h,advance,lambda:s.arm(True))
    confirm(h,advance,lambda:s.run_step(segments(4000,5000),True))
    for _ in range(65):advance()
    confirm(h,advance,s.read_log_info);advance()
    h.worker.faults['speed_missing_index']=3
    assert s.download().success;advance();sent=len(h.worker.sent)
    for _ in range(45):advance()
    assert not s.download_running and not s.complete and len(h.worker.sent)==sent

def test_gain_context_and_prediction_snapshot_do_not_follow_edits(speed_host):
    h,advance,_=speed_host;s=h.speed_control
    confirm(h,advance,s.query_capabilities)
    s.context={'model':MotorModel.default().snapshot(),'parameter_source':'performance'}
    cfg,_=design_pair();confirm(h,advance,lambda:s.apply_config(cfg))
    assert s.config.pack()==cfg.pack()
    s.context['model']['k_left']=.00009
    confirm(h,advance,lambda:s.arm())
    confirm(h,advance,lambda:s.run_step(segments(0,100)))
    frozen=next(iter(s._contexts.values()))
    s.context['model']['k_left']=.0001
    assert frozen['prediction']['model']['k_left']==.00009
    assert frozen['tuning']['model']['k_left']==DEFAULTS['空载'][0]


def test_gui_fixed_pi_full_charge_and_frozen_acceptance(speed_host,qapp):
    from car_host.speed_control_panel import SpeedControlPanel
    h,advance,_=speed_host
    confirm(h,advance,h.speed_control.query_capabilities)
    p=SpeedControlPanel(h)
    assert p.algorithm.count()==1 and p.algorithm.currentText()=='增量 PI + 目标前置滤波'
    assert p.condition.currentText()=='负载'
    assert p._model().wheels==((7.20687e-5,.102),(7.21277e-5,.098))
    assert 'LD_2s2s' in p._model().source
    p.model_boxes[0].setValue(.00008);p._restore()
    assert p._model().k_left==7.20687e-5
    p.mp.setValue(8);p.settling.setValue(.3);p._calculate()
    confirm(h,advance,p._apply);confirm(h,advance,h.speed_control.arm)
    p.targets[0][0].setValue(100);p.targets[1][0].setValue(150)
    confirm(h,advance,p._run)
    frozen=next(iter(h.speed_control._contexts.values()))
    p.mp.setValue(2);p.settling.setValue(.8)
    assert frozen['prediction']['design_spec']['mp_ratio']==.08
    assert frozen['prediction']['design_spec']['design_ts_s']==.3
    h.motion.stop();advance();p.close()

def test_gui_model_drafts_precision_staleness_and_zero_motion(speed_host,qapp):
    from car_host.speed_control_panel import SpeedControlPanel
    h,advance,_=speed_host;h.worker.faults['speed_unimplemented']=True
    confirm(h,advance,h.speed_control.query_capabilities)
    before=len(h.worker.sent);p=SpeedControlPanel(h)
    assert all(b.value()==0 for row in p.targets for b in row)
    assert [b.value() for b in p.durations]==[1500,1500]
    assert p.limit.value()==16500 and p.settling.value()==.2
    assert not p.buttons['arm'].isEnabled()
    p.condition.setCurrentText('空载')
    p.model_boxes[0].setValue(.00008123456789012345)
    p.condition.setCurrentText('负载');p.model_boxes[0].setValue(.000075)
    p.condition.setCurrentText('空载')
    assert p.model_boxes[0].value()==.00008123456789012345
    assert p._design_stale
    p._calculate();expected=p._draft()
    p.entry_method.setCurrentIndex(0);p.model_boxes[1].setValue(.07);p._safe_preview()
    assert p._draft()==expected
    p.mp.setValue(10);p._calculate()
    assert p._draft().kp_left>0 and p._draft().ki_left>0
    p.model_name.setText('custom');p.reference_voltage.setText('11.5');p._save_model()
    assert '空载/custom' in p.model_store.profiles
    assert len(h.worker.sent)==before
    p.mode.setCurrentIndex(1)
    assert p.buttons['arm'].isEnabled() and all(b.value()==0 for row in p.targets for b in row)
    p.close()


def test_recalculated_model_cannot_execute_previous_ram_parameters(speed_host,qapp):
    from car_host.speed_control_panel import SpeedControlPanel
    h,advance,_=speed_host;s=h.speed_control
    confirm(h,advance,s.query_capabilities);p=SpeedControlPanel(h)
    confirm(h,advance,p._apply);confirm(h,advance,s.arm)
    p.targets[1][0].setValue(100)
    p.model_boxes[0].setValue(.0001);p._calculate();p.refresh()
    before=len(h.worker.sent)
    assert not p.buttons['run'].isEnabled()
    with pytest.raises(ValueError,match='RAM不一致'):p._run()
    rejected=s.run_step(segments(0,100),expected_config=p._draft())
    assert not rejected.success and len(h.worker.sent)==before
    confirm(h,advance,h.motion.stop)
    confirm(h,advance,p._apply)
    assert s.config.pack()==p._draft().pack()
    confirm(h,advance,s.arm);confirm(h,advance,p._run)
    assert s._contexts[s.active_run]['executed_config']==s.config.as_dict()
    h.motion.stop();advance();p.close()


def test_new_accepted_run_clears_previous_measurement_and_invalidates_ui_cache(speed_host,qapp):
    from car_host.speed_control_panel import SpeedControlPanel
    h,advance,_=speed_host;s=h.speed_control
    acquire(h,advance,False,segments(0,100))
    old=s.info['run'];generation=s.record_generation
    assert s.samples and s.saved_directory
    p=SpeedControlPanel(h);p.refresh()
    assert f'run={old}' in p.metrics.toPlainText()
    confirm(h,advance,s.arm);confirm(h,advance,lambda:s.run_step(segments(0,200)))
    assert s.active_run!=old and s.record_generation>generation
    assert not s.samples and s.info is None and s.meta is None and s.saved_directory is None
    p.refresh()
    assert f'run={s.active_run}' in p.metrics.toPlainText()
    assert '等待本次记录' in p.metrics.toPlainText()
    h.motion.stop();advance();p.close()


def test_old_unfiltered_firmware_cannot_be_used_for_new_closed_loop(speed_host,qapp):
    from car_host.speed_control_panel import SpeedControlPanel
    h,advance,_=speed_host;s=h.speed_control
    h.worker.faults['speed_no_target_filter']=True
    confirm(h,advance,s.query_capabilities)
    assert s.caps['algorithm_ready'] and not s.caps['target_filter']
    before=len(h.worker.sent)
    assert not s.apply_config(design_pair()[0]).success and not s.arm().success
    p=SpeedControlPanel(h)
    assert not p.buttons['apply'].isEnabled() and not p.buttons['arm'].isEnabled()
    assert '滤波' in p.execution_label.text()
    assert len(h.worker.sent)==before
    # 老固件的PWM实验仍按其已有能力使用，不把滤波门控扩到开环。
    confirm(h,advance,lambda:s.arm(True));h.motion.stop();advance();p.close()


def test_filtered_reference_continuity_variable_dt_and_immediate_zero_stop():
    c=ReferenceController(2,100.,1000.,16500)
    previous_measured=0.;expected=0.
    for target,measured,dt in ((.1,0.,.01),(.1,.02,.02),(.2,.04,.01),(.2,.07,.03)):
        expected+=-100*(measured-previous_measured)+1000*dt*(target-measured)
        filtered,raw,_,saturated,_=c.step(target,measured,dt)
        assert raw==pytest.approx(expected,abs=1e-12) and not saturated
        assert 0<filtered<target
        previous_measured=measured
    assert c.step(0.,.1,.01)==(0.,0.,0,False,False)
    assert c.filtered==c.previous_error==c.previous_output==0.

