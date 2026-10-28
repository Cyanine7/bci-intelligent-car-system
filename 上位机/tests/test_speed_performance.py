"""Performance requirements, independent recurrence and count uncertainty."""
import math
from dataclasses import replace
import pytest
from car_host.motor_models import MotorModel
from car_host.speed_control_analysis import design_pair,predict,pole_match_pi,conclude_pi
from car_host.speed_control_models import SpeedConfig,SpeedSample
from car_host.speed_assessment import assess_capture
from car_host.speed_design import sampled_response


@pytest.mark.parametrize('mp,ts,tau',[(.05,.2,.059),(.1,.3,.096),(.02,.4,.059),(.08,.15,.057),(.02,.3,.059),(.2,.2,.096)])
def test_editable_theory_values_use_exact_formula_without_response_fitting(mp,ts,tau):
    model=replace(MotorModel.default(),tau_left=tau,tau_right=tau)
    cfg,_=design_pair(model=model,mp_ratio=mp,settling_s=ts)
    zeta=-math.log(mp)/math.sqrt(math.pi**2+math.log(mp)**2);wn=4/(zeta*ts)
    for (K,tau),(kp,ki) in zip(model.wheels,cfg.gains):
        assert kp==pytest.approx((2*zeta*wn*tau-1)/K,rel=1e-14)
        assert ki==pytest.approx(tau*wn**2/K,rel=1e-14)


def test_full_prediction_may_differ_from_theory_and_never_replaces_gains():
    model=MotorModel.default();cfg,_=design_pair(model=model)
    old=pole_match_pi(.05,.2,model.k_left,model.tau_left)
    assert cfg.kp_left==old['kp'] and cfg.ki_left==old['ki']
    old_cfg=replace(cfg,kp_left=old['kp'],ki_left=old['ki'])
    assert predict(model.k_left,model.tau_left,old_cfg,target_filter=False)['discrete_overshoot_percent']>8
    filtered=predict(model.k_left,model.tau_left,cfg)
    assert filtered['ideal_overshoot_percent']==pytest.approx(5.,abs=.01)
    with pytest.raises(ValueError,match='Kp为负'):design_pair(settling_s=1.)


@pytest.mark.parametrize('algorithm',[0,1,3,255])
def test_unsupported_algorithms_rejected_before_encoding(algorithm):
    with pytest.raises(ValueError):SpeedConfig(algorithm,16500,100,1000,100,1000).pack()


def test_full_charge_model_source_and_history_kept():
    m=MotorModel.default()
    assert m.k_left==7.646220324939004e-5 and m.tau_left==.059
    assert m.reference_voltage==12.6 and 'NL_full' in m.source
    assert MotorModel.historical_no_load().k_left==6.601372255427784e-5
    assert 'NL_full' not in MotorModel.default('负载').source


def capture(velocity,seconds=1.5,scale=.0001):
    # Derive integer counts by quantizing cumulative displacement, preserving totals.
    result=dict(wheels={k:dict(v0=0.,r0=0.,r1=.1,status='闭环分析完成') for k in ('left','right')})
    samples=[];distance=0.;previous=0
    for n in range(1,round(seconds/.01)+1):
        distance+=velocity(n*.01)*.01
        total=round(distance/scale);count=total-previous;previous=total
        samples.append(SpeedSample(n*10,10,514,100,100,.1,.1,count*scale/.01,count*scale/.01,0.,0.,0,0,12600,0,count,count))
    caps=dict(diameter_um=scale*1000/math.pi*1e6,counts_per_turn=1000)
    meta=dict(switch_tick=0,segments=[dict(left=0,right=0),dict(left=100,right=100)])
    return result,samples,caps,meta


def test_five_point_mean_uses_measured_values_keeps_time_and_restarts_after_gap():
    from car_host.speed_smoothing import smooth_speed
    _,samples,_,_=capture(lambda t:t,seconds=.1)
    before=[s.pack() for s in samples]
    points=smooth_speed(samples,'left',0,.0001)
    assert len(points)==6
    for i,p in enumerate(points,4):
        assert p['velocity_mps']==pytest.approx(sum(s.measured_left for s in samples[i-4:i+1])/5)
        assert p['time_s']==samples[i].tick/1000
        assert p['delay_s']==pytest.approx(.02)
        assert p['count_uncertainty_mps']==pytest.approx(.0001/.05)
    assert [s.pack() for s in samples]==before
    # 包含tick回绕和缺点：不得沿用缺口前四点，也不补零或插值。
    shifted=[replace(s,tick=(s.tick-50+(10 if i>=5 else 0))&0xffffffff) for i,s in enumerate(samples)]
    assert [p['index'] for p in smooth_speed(shifted,'left',0)]==[4,9]


def test_count_window_compares_signed_differences_not_upper_limits():
    # 固定五点窗下，此用例需足够细的计数分辨率才能给出ts确定上界。
    args=capture(lambda t:.1,scale=.00001)
    report=assess_capture(*args,dict(mp_ratio=.05,design_ts_s=.2))
    assert report['status']=='理论与实测对比'
    assert report['wheels']['left']['overshoot_comparison']['status']=='实测低于理论值'
    assert report['wheels']['left']['settling_time_s_interval'][1]<=.2
    delayed=capture(lambda t:0 if t<.5 else .1,scale=.00001)
    assert assess_capture(*delayed,dict(mp_ratio=.05,design_ts_s=.2))['wheels']['left']['settling_comparison']['status']=='实测高于理论值'
    row=assess_capture(*delayed,dict(mp_ratio=.05,design_ts_s=.5))['wheels']['left']
    lo,hi=row['settling_time_s_interval']
    assert lo<.5<hi and row['settling_comparison']['status']=='测量区间包含理论值'


def test_count_window_reports_short_observation_and_keeps_raw_samples():
    args=capture(lambda t:.1,seconds=.04)
    before=[s.pack() for s in args[1]]
    r=assess_capture(*args,dict(mp_ratio=.05,design_ts_s=.2))
    assert r['wheels']['left']['status']=='无法比较'
    assert [s.pack() for s in args[1]]==before
    assert '瞬时' in r['scope']


@pytest.mark.parametrize('target_filter',[False,True])
def test_sampled_response_matches_independent_unclipped_loop(target_filter):
    K,tau,kp,ki=8e-5,.059,14000.,450000.
    times,values,_=sampled_response(K,tau,kp,ki,.01,.5,target_filter=target_filter)
    v=u=previous=measured=previous_measured=0.
    for expected in values:
        assert measured==pytest.approx(expected,abs=1e-12)
        error=1-measured
        u+=(-kp*(measured-previous_measured) if target_filter else kp*(error-previous))+ki*.01*error
        previous=error;previous_measured=measured
        measured=v*tau*(1-math.exp(-.01/tau))/.01+K*u*(1-tau*(1-math.exp(-.01/tau))/.01)
        v=math.exp(-.01/tau)*v+K*(1-math.exp(-.01/tau))*u


def test_amplitude_and_clipping_change_prediction_but_not_formula_gains():
    cfg,rows=design_pair(target=.1,pwm_limit=1)
    normal,_=design_pair(target=.2,pwm_limit=16500)
    assert cfg.gains==normal.gains
    assert rows[0]['predicted_integer_metrics']['settling_time_s'] is None


@pytest.mark.parametrize('reverse',[False,True])
def test_window_overshoot_failure_has_target_and_direction_consistent_basis(reverse):
    args=capture(lambda t:(.07 if reverse else .13) if t<.5 else .1)
    if reverse:
        for row in args[0]['wheels'].values():row.update(v0=.2,r0=.2)
        args[3]['segments'][0]=dict(left=200,right=200)
    report=assess_capture(*args,dict(mp_ratio=.05,design_ts_s=1.))
    assert report['wheels']['left']['overshoot_comparison']['status']=='实测高于理论值'
    assert report['wheels']['left']['overshoot_difference_pp']>0


def test_same_quantized_peak_can_have_different_short_window_overshoot():
    scale=.066*math.pi/1560
    def make(prefix):
        args=capture(lambda t:.1,scale=scale)
        counts=prefix+[7,8]*(len(args[1])//2)
        samples=[replace(s,encoder_left=c,encoder_right=c,measured_left=c*scale/.01,
                         measured_right=c*scale/.01) for s,c in zip(args[1],counts)]
        assert max(s.encoder_left for s in samples)==8
        return assess_capture(args[0],samples,args[2],args[3],dict(mp_ratio=.05,design_ts_s=.2))
    a=make([0,1,2,4,6,8,8,7,8,7])
    b=make([0,0,1,2,3,4,6,7,8,8,8,8,8])
    assert a['wheels']['left']['overshoot_percent']!=b['wheels']['left']['overshoot_percent']
    assert a['wheels']['left']['overshoot_window_s']==b['wheels']['left']['overshoot_window_s']==.05


def test_prefilter_continuous_response_matches_standard_second_order():
    mp,ts=.05,.2
    model=MotorModel.default();cfg,rows=design_pair(model=model,mp_ratio=mp,settling_s=ts)
    sim=predict(model.k_left,model.tau_left,cfg,target=.1,duration=1.)
    zeta=rows[0]['zeta'];wn=rows[0]['wn'];wd=wn*math.sqrt(1-zeta*zeta)
    for t,v,_ in sim['continuous']:
        expected=.1*(1-math.exp(-zeta*wn*t)*(math.cos(wd*t)+zeta/math.sqrt(1-zeta*zeta)*math.sin(wd*t)))
        assert v==pytest.approx(expected,abs=3e-9)
    assert sim['ideal_overshoot_percent']==pytest.approx(mp*100,abs=.01)
