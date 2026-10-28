"""Four-second profiles remain bounded by the actual device CAPS."""
import pytest
from car_host.experiment_models import build_plan, validate_against_caps
from car_host.project_protocol import (CAPS,CAPS_LAYOUT,HELLO,ARM,GET_PROFILE_CAPS,
    PROFILE_CAPS,PROFILE_CAPS_LAYOUT,PROFILE_COMMAND_LAYOUT,validate_pwm_segments)
from test_project_v1 import project_host,reply,ack,update_state

SEGMENTS=[dict(left_pwm=4000,right_pwm=4000,duration_ms=2000),
          dict(left_pwm=10000,right_pwm=10000,duration_ms=2000)]

def test_four_second_frozen_plan_and_lower_device_limit():
    p=build_plan(dict(schema_version=2,name='四秒',suite=dict(suite_id='four_seconds',total_entered_budget=1),
        stages=[dict(stage_id='fit',name='fit',steps=[dict(segments=SEGMENTS)])]))
    limits=dict(pwm_limit=10000,max_duration_ms=4000,profile_version=1,
        profile_max_segments=2,profile_pwm_limit=10000,profile_max_total_ms=4000,
        profile_control_hz=100,profile_state_hz=20)
    validate_against_caps(p,limits)
    with pytest.raises(ValueError):validate_against_caps(p,{**limits,'profile_max_total_ms':2000})
    with pytest.raises(ValueError):validate_pwm_segments([SEGMENTS[0],{**SEGMENTS[1],'duration_ms':2001}])

@pytest.mark.parametrize('device_limit',[2000,4000])
def test_four_seconds_only_transmitted_with_actual_caps(project_host,device_limit):
    host=project_host
    reply(host,CAPS,CAPS_LAYOUT.pack(15,10000,1180,device_limit,100,20,1),seq=1)
    ack(host,HELLO,seq=1);update_state(host)
    assert host.motion.query_profile_caps().success
    reply(host,PROFILE_CAPS,PROFILE_CAPS_LAYOUT.pack(3,1,2,10000,device_limit,100,20))
    ack(host,GET_PROFILE_CAPS)
    assert host.motion.arm_pwm().success
    ack(host,ARM);update_state(host,tick=1020,mode=1,armed=1)
    before=len(host.worker.sent)
    result=host.motion.set_pwm_profile(SEGMENTS)
    assert result.success==(device_limit==4000)
    if device_limit==2000:assert len(host.worker.sent)==before
    else:assert PROFILE_COMMAND_LAYOUT.unpack(host.worker.sent[-1][0][14:-2])[7:9]==(2000,4000)
