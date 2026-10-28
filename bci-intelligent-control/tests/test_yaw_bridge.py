"""实际Host/C角速度联调：量化编码器与独立电机、陀螺仪模型，无实物访问。"""
import ctypes as c
from dataclasses import replace
from pathlib import Path
import json
import math
import sys
import time
from uuid import uuid4

root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root.parent/'上位机'),str(root.parent/'上位机/tests')]
import car_host
from PySide6.QtCore import QCoreApplication
from car_host.controller import HostController
from car_host.transport import ConnectionConfig
from car_host.motor_models import MotorModel
from car_host.yaw_analysis import design
from car_host.yaw_models import YawConfig,validate_segments
from test_application_extensions import Link

app=QCoreApplication.instance() or QCoreApplication([])
dll=c.CDLL(str((root/sys.argv[1]).resolve()))
dll.test_rx.argtypes=(c.c_void_p,c.c_uint);dll.test_tick.argtypes=(c.c_uint32,c.c_int)
dll.test_encoder.argtypes=(c.c_uint32,c.c_uint32,c.c_int32,c.c_int32)
dll.test_gyro.argtypes=(c.c_float,c.c_uint32,c.c_uint32,c.c_int)
dll.test_drain.argtypes=(c.c_void_p,c.c_uint);dll.test_drain.restype=c.c_uint
dll.test_reset();dll.test_sensor_counts(0,0)
clock=[time.monotonic()];original=time.monotonic;time.monotonic=lambda:clock[0]
directory=root.parent/'上位机/.qa'/('yaw_native_'+uuid4().hex[:8]);directory.mkdir(parents=True)
h=HostController(directory,worker_factory=Link);h.timer.stop()
sent=0;tick=90;plant=[0.,0.];remainders=[0.,0.];gyro_seq=0;gyro_tick=0;stale=False

def pump():
    global sent,tick,gyro_seq,gyro_tick
    tick=(tick+10)&0xffffffff;clock[0]+=.01
    while sent<len(h.worker.sent):
        raw=h.worker.sent[sent][0];sent+=1;dll.test_rx(raw,len(raw))
    left=c.c_int();right=c.c_int();dll.test_output(c.byref(left),c.byref(right));counts=[]
    for i,(K,tau) in enumerate(MotorModel.default('负载').wheels):
        steady=K*(right.value if i else left.value);decay=math.exp(-.01/tau)
        distance=steady*.01+(plant[i]-steady)*tau*(1-decay)
        plant[i]=steady+(plant[i]-steady)*decay
        total=remainders[i]+distance/(.066*math.pi/1560);count=math.trunc(total);remainders[i]=total-count;counts.append(count)
    if not stale and ((tick-gyro_tick)&0xffffffff)>=20:
        gyro_seq+=1;gyro_tick=tick
        dll.test_gyro((plant[1]-plant[0])/.16+.015,gyro_seq,gyro_tick,1)
    dll.test_sensor_counts(*counts);dll.test_encoder(tick,10,*counts);dll.test_tick(tick,1)
    buf=c.create_string_buffer(8192);n=dll.test_drain(buf,len(buf))
    for offset in range(0,n,19):h.worker.emit('rx',buf.raw[offset:min(offset+19,n)])
    h.poll();app.processEvents()

def confirmed(method):
    result=method();assert result.success,(result,h.yaw_control.status)
    for _ in range(80):
        pump();state=h.command_results[result.request_id]
        if state.status in ('accepted','rejected','unknown'):break
    assert state.status=='accepted',(state,[x.message for x in list(h.logs)[-8:]])
    while not h.project_state_fresh:pump()
    return result

def finish_record():
    for _ in range(2800):
        pump()
        if not h.yaw_control.result_fetching:break
    assert h.yaw_control.complete and h.yaw_control.saved_directory,h.yaw_control.status

try:
    assert h.connect_device(ConnectionConfig(port='SOFTWARE_ONLY',protocol='PROJECT_V1'))
    h.poll();pump()
    while not h.project_state_fresh:pump()
    s=h.yaw_control;confirmed(s.query_capabilities)
    assert s.caps['max_segment_ms']==5000 and s.caps['max_total_ms']==10000 and s.caps['capacity']==1040
    cfg,ctx=design();s.context=dict(design=ctx,model=MotorModel.default('负载').snapshot())
    confirmed(lambda:s.apply_config(cfg));assert s.config.pack()==cfg.pack()
    # 主机拒绝非法草稿，保留已确认RAM，不绕过服务发送运动。
    assert not h.send_command('yaw_apply',dict(s.run_context,**{**cfg.__dict__,'ki':-1})).success
    assert s.config.pack()==cfg.pack()
    confirmed(s.calibrate)
    for _ in range(210):pump()
    confirmed(s.read_status);assert s.imu['calibration']==2 and abs(s.imu['bias']-.015)<1e-6
    confirmed(s.arm)
    for bad in (5001,5010):
        try:validate_segments([dict(v=.1,target=0,duration_ms=bad),dict(v=.1,target=.3,duration_ms=5000)])
        except ValueError:pass
        else:raise AssertionError('超5秒单段未拒绝')
    stages=[dict(v=.1,target=0,duration_ms=5000),dict(v=.1,target=.3,duration_ms=5000)]
    confirmed(lambda:s.run_step(stages,1,cfg))
    for _ in range(1030):pump()
    assert not h.latest.armed and h.latest.pwm_left==h.latest.pwm_right==0
    finish_record()
    assert s.info['segment']==1 and ((s.meta['switch']-s.meta['start'])&0xffffffff)==5000
    assert 9850<=((s.meta['end']-s.meta['start'])&0xffffffff)<=10000,s.meta
    assert s.meta['end']==s.meta['deadline']
    assert 1020<=s.info['count']<=1040,s.info
    assert s.complete and s.saved_directory,(s.status,s.info,len(s.samples))
    summary=json.loads((s.saved_directory/'summary.json').read_text(encoding='utf-8'))
    assert summary['complete'] and summary['run_config']==json.loads(json.dumps(asdict_cfg:=s.run_context['config']))
    measured=summary['analysis']['stages'][1]['five_point']
    assert measured['overshoot_percent'] is not None and measured['rise_time_s'] is not None
    if measured['settling_time_s'] is None:assert '观察窗内' in measured['reason']
    assert any(x.right_ref>x.left_ref for x in s.samples.values() if x.flags&512)
    assert max(abs(x.pwm_left) for x in s.samples.values())<=cfg.pwm_limit
    print('PASS: actual Host/C 5s+5s, 100Hz,',len(s.samples),'samples, automatic read/metrics/CSV/JSON:',s.saved_directory)
    confirmed(s.read_status);confirmed(s.arm)
    stages=[dict(v=0,target=.3,duration_ms=2000),dict(v=0,target=-.3,duration_ms=2000)]
    confirmed(lambda:s.run_step(stages,1,cfg))
    for _ in range(12):pump()
    confirmed(lambda:h.motion.stop())
    for _ in range(220):pump()
    finish_record()
    assert s.info['segment']==0 and s.info['reason']==1
    print('PASS: inplace counter-rotation and STOP cancels later stage')
    confirmed(s.read_status);confirmed(s.arm)
    confirmed(lambda:s.run_step(stages,1,cfg));stale=True
    for _ in range(35):pump()
    assert not h.latest.armed and h.latest.pwm_left==h.latest.pwm_right==0
    finish_record();assert s.info['reason']==6 and s.info['segment']==0
    print('PASS: independent gyro age >60ms stops real production C')
finally:
    h.disconnect_device();h.poll();h.shutdown();time.monotonic=original
