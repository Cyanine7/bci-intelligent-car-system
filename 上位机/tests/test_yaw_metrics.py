"""独立已知响应核对正反向超调、静差、tr/ts及缺样边界。"""
from dataclasses import asdict
import pytest
from car_host.yaw_models import YawSample,YawConfig
from car_host.yaw_metrics import analyze_capture,describe_analysis


@pytest.mark.parametrize('sign',[1,-1])
def test_known_response_metrics_and_incomplete_record(sign):
    wave=[0,.06,.15,.27,.33,.31,.302,.3,.3,.3]
    samples={}
    for i,tick in enumerate(range(900,2200,10)):
        second=1500<=tick<2000;active=1000<=tick<2000
        flags=(2 if active else 1 if tick<1000 else 1024)|32|64|(512 if second else 0)
        omega=sign*(wave[min((tick-1500)//10,len(wave)-1)] if second else 0.)
        fields={name:0 for name in YawSample.__dataclass_fields__}
        fields.update(tick=tick,dt=10,flags=flags,gyro_tick=tick,gyro_seq=i+1,v=.1,
                      target=sign*.3 if second else 0.,omega=omega,raw_gyro=omega,battery_mv=12000)
        samples[i]=YawSample(**fields)
    info=dict(run=7,revision=3,start=1000,end=2000,count=len(samples),status=3,reason=2,kind=1,segment=1)
    meta=dict(run=7,start=1000,switch=1500,end=2000,kind=1,segment=1,
              segments=[dict(v=.1,target=0.,duration_ms=500),dict(v=.1,target=sign*.3,duration_ms=500)])
    context=dict(context=dict(design=dict(zeta=1.5,design_ts_s=.7)))
    result=analyze_capture(info,meta,samples,True,context,asdict(YawConfig()))
    raw=result['stages'][1]['raw'];smooth=result['five_point']
    assert raw['overshoot_percent']==pytest.approx(10.)
    assert raw['rise_time_s']==pytest.approx(.02) and raw['settling_time_s']==pytest.approx(.06)
    assert smooth['overshoot_percent']==pytest.approx(2.8)
    assert smooth['rise_time_s']==pytest.approx(.04) and smooth['settling_time_s']==pytest.approx(.09)
    assert result['steady_error_rad_s']==pytest.approx(0,abs=1e-12)
    assert result['stages'][0]['raw']['overshoot_percent'] is None
    assert '目标没有阶跃' in result['stages'][0]['raw']['reason']
    assert '超调 Mp=2.8%' in describe_analysis(result)
    missing=dict(samples);missing.pop(70)
    incomplete=analyze_capture(info,meta,missing,False,context)
    assert incomplete['five_point']['settling_time_s'] is None and incomplete['rise_time_s'] is None
    assert '记录不完整' in describe_analysis(incomplete)
