"""V2 real HostController -> actual C protocol/capture (no device I/O)."""
import ctypes as c
from pathlib import Path
import sys
import tempfile
import time
import math
import json
root=Path(__file__).resolve().parents[1]
dll_path=sys.argv[1];ready=bool(int(sys.argv[2]))
sys.path[:0]=[str(root.parent/'上位机'),str(root.parent/'上位机/tests')]
import car_host
from PySide6.QtCore import QCoreApplication
from car_host.controller import HostController
from car_host.transport import ConnectionConfig
from car_host.speed_control_models import SpeedConfig
from car_host.motor_models import MotorModel
from car_host.speed_control_analysis import design_pair
from test_application_extensions import Link
app=QCoreApplication.instance() or QCoreApplication([])
dll=c.CDLL(str((root/dll_path).resolve()))
dll.test_rx.argtypes=(c.c_void_p,c.c_uint)
dll.test_tick.argtypes=(c.c_uint32,c.c_int)
dll.test_encoder.argtypes=(c.c_uint32,c.c_uint32,c.c_int32,c.c_int32)
dll.test_drain.argtypes=(c.c_void_p,c.c_uint);dll.test_drain.restype=c.c_uint
dll.test_reset()
clock=[time.monotonic()]
real_monotonic=time.monotonic
time.monotonic=lambda:clock[0]
try:
 with tempfile.TemporaryDirectory(prefix='v2-native-bridge-') as directory:
    host=HostController(Path(directory),worker_factory=Link);host.timer.stop()
    assert host.connect_device(ConnectionConfig(port='SOFTWARE_ONLY',protocol='PROJECT_V1'))
    host.poll();sent_index=0;tick=90
    plant=[0.,0.];remainders=[0.,0.]
    def pump(ms=10):
        global sent_index,tick
        tick+=ms;clock[0]+=ms/1000
        while sent_index<len(host.worker.sent):
            raw=host.worker.sent[sent_index][0];sent_index+=1
            for byte in raw:dll.test_rx(bytes([byte]),1)
        # Independent held-input motor + integer encoder fixture around actual C PI.
        left=c.c_int();right=c.c_int();dll.test_output(c.byref(left),c.byref(right))
        counts=[];scale=.066*math.pi/1560
        for i,(K,tau) in enumerate(MotorModel.default().wheels):
            steady=K*(left.value if i==0 else right.value);decay=math.exp(-ms/1000/tau)
            distance=steady*ms/1000+(plant[i]-steady)*tau*(1-decay)
            plant[i]=steady+(plant[i]-steady)*decay
            total=remainders[i]+distance/scale;count=math.trunc(total)
            remainders[i]=total-count;counts.append(count)
        dll.test_sensor_counts(*counts)
        dll.test_encoder(tick,ms,*counts);dll.test_tick(tick,1)
        buf=c.create_string_buffer(4096);n=dll.test_drain(buf,len(buf));raw=buf.raw[:n]
        for offset in range(0,n,23):host.worker.emit('rx',raw[offset:offset+23])
        host.poll();app.processEvents()
    def confirmed(method):
        result=method();assert result.success,result.message
        for _ in range(8):
            pump()
            if host.command_results[result.request_id].status=='accepted':break
        assert host.command_results[result.request_id].status=='accepted',host.command_results[result.request_id]
        while not host.project_state_fresh:pump()
    pump()
    service=host.speed_control
    confirmed(service.query_capabilities)
    assert service.caps['version']==2 and service.caps['algorithm_ready']==ready
    assert service.caps['target_filter']==ready
    if ready:
        cfg=design_pair()[0]
        service.context=dict(model=MotorModel.default().snapshot(),parameter_source='performance',
                             design_spec=dict(mp_ratio=.05,design_ts_s=.2))
        confirmed(lambda:service.apply_config(cfg))
        assert service.config.pack()==cfg.pack()
        confirmed(service.arm)
        closed=[dict(left=100,right=100,duration_ms=1500),dict(left=150,right=150,duration_ms=1500)]
        confirmed(lambda:service.run_step(closed))
        for _ in range(330):pump()
        confirmed(service.read_log_info)
        for _ in range(8):pump()
        assert service.meta and service.meta['segments']==closed
        assert service.download().success
        for _ in range(420):
            pump()
            if service.complete and not service.download_running:break
        assert service.complete and service.saved_directory,service.status
        summary=json.loads((service.saved_directory/'summary.json').read_text(encoding='utf-8'))
        assert summary['info']['kind']==1
        assert summary['design_context']['prediction']['design_spec']['mp_ratio']==.05
        # Protocol test double deliberately fails to model PI; production must analyze.
        if 'speed_core_fixture' not in dll_path:
            assert summary['analysis']['valid'],summary['analysis']
            assert summary['analysis']['comparison']['status']=='理论与实测对比'
            active=[sample for sample in service.samples.values() if sample.flags&2]
            first=next(sample for sample in active if not sample.flags&512)
            switched=next(sample for sample in active if sample.flags&512)
            assert 0<first.filtered_left<.1
            assert .1<switched.filtered_left<.15
            assert summary['predictions']['left']['target_filter']
            print('PASS: actual C PI closed step + quantized plant, complete export, comparison:',summary['analysis']['comparison']['status'])
    else:
        assert not service.arm().success
    confirmed(lambda:service.arm(True))
    stages=[dict(left=4000,right=4000,duration_ms=1500),dict(left=5000,right=5000,duration_ms=1500)]
    confirmed(lambda:service.run_step(stages,True))
    for _ in range(330):pump()
    confirmed(service.read_log_info)
    for _ in range(8):pump()
    assert service.meta and service.meta['segments']==stages
    assert service.info['kind']==2 and not host.latest.armed
    assert service.download().success
    for _ in range(420):
        pump()
        if service.complete and not service.download_running:break
    assert service.complete,service.status
    assert service.saved_directory.exists()
    assert service.info['count']>320
    host.disconnect_device();host.poll();host.shutdown()
    print(f'PASS: real v2 Host/C bridge; algorithm_ready={ready}, binary64/config, two stages, {len(service.samples)} samples, CSV/JSON.')
finally:time.monotonic=real_monotonic

