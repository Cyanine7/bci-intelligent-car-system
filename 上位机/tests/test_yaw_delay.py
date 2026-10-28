"""必要检查：可编辑Tv、连续裕度与实际采样下的时延敏感性。"""
from dataclasses import replace
import math
import pytest
from car_host.motor_models import MotorModel
from car_host.yaw_analysis import design,predict,stability_margin


def test_manual_tv_formulas_and_validation():
    baseline,ctx=design()
    assert stability_margin(baseline,ctx)['extra_delay_margin_s']==pytest.approx(.0308557,rel=1e-4)
    manual,first=design(tv_override=dict(order=1,tau_v_s=.08),gain_scale=.5)
    wn=4*(1.5+math.sqrt(1.5**2-1))/.7
    assert manual.kp==pytest.approx(.5*.16*(3*wn*.08-1))
    assert manual.ki==pytest.approx(.5*.16*.08*wn**2)
    assert first['tv_denominator']==[.08,1.]
    _,second=design(tv_override=dict(order=2,a=45.,b=900.))
    assert second['tv_denominator']==[1.,45.,900.]
    assert second['tau_v_s']!=ctx['tau_v_s']
    for override in (dict(order=1,tau_v_s=0),dict(order=2,a=-1,b=900),dict(order=1,tau_v_s=float('nan'))):
        with pytest.raises(ValueError):design(tv_override=override)
    with pytest.raises(ValueError):design(gain_scale=1.1)


def test_measured_delay_hypothesis_and_reduced_gain_prediction():
    original,_=design();reduced=replace(original,kp=original.kp*.5,ki=original.ki*.5)
    stages=[dict(v=.1,target=0.,duration_ms=2000),dict(v=.1,target=.3,duration_ms=2000)]
    uncorrected=predict(original,MotorModel.default('负载'),stages,feedback_delay_s=.03)
    corrected=predict(reduced,MotorModel.default('负载'),stages,feedback_delay_s=.03)
    assert uncorrected['10ms离散双轮']['metrics']['overshoot_percent']<15
    assert uncorrected['含时延离散双轮']['metrics']['overshoot_percent']>80
    assert uncorrected['含时延离散双轮']['metrics']['settling_time_s'] is None
    stable=corrected['含时延离散双轮']['metrics']
    assert stable['overshoot_percent']<15 and stable['settling_time_s']<1.1
    manual=predict(reduced,MotorModel.default('负载'),stages,tv_override=dict(order=1,tau_v_s=.15),feedback_delay_s=.03)
    assert manual['当前Tv']['omega']==manual['一阶等效']['omega']
    assert manual['双轮独立']==corrected['双轮独立']  # 手动Tv不伪造原左右轮模型。
