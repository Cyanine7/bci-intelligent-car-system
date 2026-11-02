"""Actual production Host/C bridge, deterministic 100 Hz sensors, no hardware."""
import ctypes as c
from pathlib import Path
import struct
import sys
import time
from uuid import uuid4

root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root.parent/'上位机'),str(root.parent/'上位机/tests')]
import car_host
from PySide6.QtCore import QCoreApplication
from car_host.controller import HostController
from car_host.transport import ConnectionConfig
from car_host.yaw_models import YawConfig
from car_host.continuous_models import TARGET, LEASE_MS
from car_host.speed_control_analysis import ReferenceController
from test_application_extensions import Link

app=QCoreApplication.instance() or QCoreApplication([])
dll=c.CDLL(str((root/(sys.argv[1] if len(sys.argv)>1 else 'tests/yaw_core_fixture.dll')).resolve()))
dll.test_rx.argtypes=(c.c_void_p,c.c_uint);dll.test_tick.argtypes=(c.c_uint32,c.c_int)
dll.test_encoder.argtypes=(c.c_uint32,c.c_uint32,c.c_int32,c.c_int32)
dll.test_gyro.argtypes=(c.c_float,c.c_uint32,c.c_uint32,c.c_int)
dll.test_drain.argtypes=(c.c_void_p,c.c_uint);dll.test_drain.restype=c.c_uint
dll.test_reset();dll.test_sensor_counts(0,0)
original=time.monotonic;clock=[original()];time.monotonic=lambda:clock[0]
directory=root.parent/'上位机/.qa'/('continuous_native_'+uuid4().hex[:8]);directory.mkdir(parents=True)
h=HostController(directory,worker_factory=Link);h.timer.stop();h.continuous_control.timer.stop()
tick=90;sent=0;gyro_tick=0;gyro_seq=0;stale=False;local_allowed=True;drop_ack=False
reference_enabled=False;requested=[0.,0.];current=[0.,0.];error=output=0.;checked=0
refs=[];last_outputs=(0,0)
cfg=YawConfig()


def reset_reference():
    global requested,current,error,output,refs
    requested=[0.,0.];current=[0.,0.];error=output=0.
    refs=[ReferenceController(2,kp,ki,cfg.pwm_limit) for kp,ki in
          ((cfg.kp_left,cfg.ki_left),(cfg.kp_right,cfg.ki_right))]


def pump():
    global tick,sent,gyro_tick,gyro_seq,error,output,checked,last_outputs
    tick=(tick+10)&0xffffffff;clock[0]+=.01
    while sent<len(h.worker.sent):
        raw=h.worker.sent[sent][0];sent+=1
        if reference_enabled:
            if raw[3]==29:
                _,_,_,_,_,v,w=TARGET.unpack(raw[14:-2]);requested[:]=[v,w]
                for i,value in enumerate(requested):
                    if value==0:current[i]=0.
                if not v and not w:
                    error=output=0.
                    for ref in refs:ref.reset()
            elif raw[3]==4:reset_reference()
        dll.test_rx(raw,len(raw))
    if not stale:
        gyro_tick=tick;gyro_seq+=1;dll.test_gyro(.015,gyro_seq,gyro_tick,1)
    # Fixed zero encoder/gyro feedback isolates and checks every real PI recurrence.
    dll.test_sensor_counts(0,0);dll.test_encoder(tick,10,0,0);dll.test_tick(tick,int(local_allowed))
    left=c.c_int();right=c.c_int();dll.test_output(c.byref(left),c.byref(right));last_outputs=(left.value,right.value)
    if reference_enabled:
        for i,step in enumerate((.005,.02)):
            d=requested[i]-current[i];current[i]+=max(-step,min(step,d))
        v,w=current;e=w
        raw=output+cfg.kp*(e-error)+cfg.ki*e*.01
        limit=min(cfg.delta_limit,2*(1.18-abs(v)));delta=max(-limit,min(limit,raw))
        if v==0 and w==0:delta=e=0.
        error=e;output=delta
        expected=tuple(ref.step(v+(delta/2 if i else -delta/2),0.,.01)[2] for i,ref in enumerate(refs))
        assert last_outputs==expected,(tick,last_outputs,expected,current)
        checked+=1
    buf=c.create_string_buffer(8192);n=dll.test_drain(buf,len(buf));offset=0
    while offset<n:
        size=16+struct.unpack_from('<H',buf.raw,offset+4)[0];frame=buf.raw[offset:offset+size];offset+=size
        if drop_ack and frame[3]==0x82 and frame[14]==29:continue
        # Deliberately fragment all wire packets at non-field boundaries.
        for start in range(0,len(frame),19):h.worker.emit('rx',frame[start:start+19])
    h.poll();app.processEvents()


def confirmed(method):
    r=method();assert r.success,r
    for _ in range(80):
        pump();result=h.command_results[r.request_id]
        if result.status in ('accepted','rejected','unknown'):break
    assert result.status=='accepted',(result,h.continuous_control.status)
    while not h.project_state_fresh:pump()
    return r


def arm():
    confirmed(h.yaw_control.read_status)
    confirmed(h.continuous_control.arm)
    assert h.continuous_control.running,h.continuous_control.status


try:
    assert h.connect_device(ConnectionConfig(port='SOFTWARE_ONLY',protocol='PROJECT_V1'))
    h.poll();pump()
    while not h.project_state_fresh:pump()
    s=h.continuous_control;y=h.yaw_control
    confirmed(s.query_capabilities);confirmed(y.query_capabilities)
    confirmed(lambda:y.apply_config(cfg));confirmed(y.calibrate)
    for _ in range(215):pump()
    confirmed(y.read_status);assert y.imu_ready
    reset_reference();s.keys.base_omega=1.2;arm();reference_enabled=True;s.press('up');s.press('left')
    for _ in range(1300):pump()
    assert s.running and checked>=1300 and abs(s.latest['requested_omega']-1.2)<1e-6,s.status
    target_metadata=[m for m in h.command_metadata.values() if m['command']=='continuous_target']
    assert all(abs(b['queued_monotonic']-a['queued_monotonic']-.5)<1e-6 for a,b in zip(target_metadata,target_metadata[1:]))
    assert len(s.live)<=600 and y.info is None and not y.samples
    for _ in range(16):s.press('z');s.release('z')
    for _ in range(100):pump()
    assert s.running and abs(s.latest['requested_omega']-3.75)<1e-6
    s.press('x');s.release('x')
    for _ in range(75):pump()
    assert abs(s.latest['requested_omega']-3.375)<1e-6
    s.release('up');pump();assert s.latest['v']==0 or current[0]==0
    s.press('down')
    for _ in range(400):pump()
    assert current[0]<0 and current[1]<0 and s.latest['requested_omega']<0 and s.running
    s.release('left');s.release('down');pump();assert last_outputs==(0,0)
    for _ in range(5):pump()
    assert last_outputs==(0,0) and s.running
    print('PASS: production Host/C 2Hz, targets 1.2/3.75/3.375rad/s, every 100Hz PI/filter recurrence, ramp, reverse and immediate zero;',checked,'ticks')

    s.cancel();reference_enabled=False
    for _ in range(10):pump()
    arm();s.press('up')
    for _ in range(70):pump()
    assert s.running
    # Simulate host process loss: no STOP and no further target packets.
    s.engaged=False;s.keys.clear();last_deadline=s.latest['deadline'];before=len(h.worker.sent)
    for _ in range(LEASE_MS//10+10):
        pump()
        if not h.latest.armed:break
    assert not h.latest.armed and last_outputs==(0,0) and h.latest.stop_reason==2
    assert ((h.latest.device_time_ms-last_deadline)&0xffffffff)<200 and len(h.worker.sent)==before
    print('PASS: real MCU absolute deadline stops without host assistance')

    arm();s.press('up');drop_ack=True
    for _ in range(100):pump()
    assert not s.engaged and not h.latest.armed and last_outputs==(0,0),s.status
    after=len(h.worker.sent)
    for _ in range(80):pump()
    assert len(h.worker.sent)==after
    drop_ack=False
    print('PASS: lost target ACK triggers STOP, no retries or later motion')

    arm();s.press('left')
    for _ in range(70):pump()
    stale=True
    for _ in range(15):
        pump()
        if not h.latest.armed:break
    assert not h.latest.armed and last_outputs==(0,0) and h.latest.stop_reason==6
    stale=False
    for _ in range(20):pump()
    arm();s.press('up')
    for _ in range(60):pump()
    local_allowed=False
    for _ in range(10):
        pump()
        if not h.latest.armed:break
    assert not h.latest.armed and last_outputs==(0,0) and h.latest.stop_reason==3
    local_allowed=True
    for _ in range(20):pump()
    arm();s.press('up')
    for _ in range(60):pump()
    s.cancel();confirmed(lambda:h.motion.stop());count=len(h.worker.sent)
    for _ in range(150):pump()
    assert len(h.worker.sent)==count and not h.latest.armed and last_outputs==(0,0)
    print('PASS: gyro >60ms, local disable and explicit STOP; no old-key restart')
    (directory/'result.txt').write_text(f'PASS: 2Hz, lease {LEASE_MS}ms; {checked} exact production PI ticks; no hardware\n',encoding='utf-8')
    print('Evidence:',directory)
finally:
    h.disconnect_device();h.poll();h.shutdown();time.monotonic=original
