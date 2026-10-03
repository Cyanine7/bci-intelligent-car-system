"""Host services exchange real wire bytes with the actual native MCU C core.

Run after tests/run_tests.ps1. This test opens no UART or Bluetooth adapter.
"""
import csv
import ctypes as c
from pathlib import Path
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root.parent/'上位机'))
sys.path.insert(0,str(root.parent/'上位机/tests'))
import car_host  # Qt compatibility setup must precede Qt imports.
from PySide6.QtCore import QCoreApplication
from car_host.controller import HostController
from car_host.project_protocol import PROJECT_V1, PARAMETER_KEYS
from car_host.transport import ConnectionConfig
from test_application_extensions import Link

app = QCoreApplication.instance() or QCoreApplication([])
dll = c.CDLL(str(root/'tests/project_core_fixture.dll'))
dll.test_rx.argtypes=(c.c_void_p,c.c_uint)
dll.test_tick.argtypes=(c.c_uint32,c.c_int)
dll.test_drain.argtypes=(c.c_void_p,c.c_uint)
dll.test_drain.restype=c.c_uint
dll.test_output.argtypes=(c.POINTER(c.c_int),c.POINTER(c.c_int))
dll.test_reset()

with tempfile.TemporaryDirectory(prefix='bci-bridge-') as directory:
    host=HostController(Path(directory),worker_factory=Link)
    host.timer.stop()
    assert host.connect_device(ConnectionConfig(port='SOFTWARE_ONLY',protocol=PROJECT_V1))
    host.poll()
    sent_index=0

    def pump(tick, allowed=1):
        global sent_index
        while sent_index<len(host.worker.sent):
            raw=host.worker.sent[sent_index][0]; sent_index+=1
            # Deliberately split each real host command into single IRQ bytes.
            for byte in raw: dll.test_rx(bytes([byte]),1)
        dll.test_tick(tick,allowed)
        b=c.create_string_buffer(4096)
        count=dll.test_drain(b,len(b))
        raw=b.raw[:count]
        # Deliberately split MCU output across transport RX events.
        for i in range(0,len(raw),7): host.worker.emit('rx',raw[i:i+7])
        host.poll()

    def output():
        a,b=c.c_int(),c.c_int();dll.test_output(c.byref(a),c.byref(b))
        return a.value,b.value

    pump(100)
    assert host.protocol_status=='PROJECT_V1 握手完成'
    assert host.capabilities.motion and host.capabilities.open_loop
    assert host.latest.device_time_ms==100 and host.latest.encoder_right==-13
    assert not host.send_manual('reset',False)
    arm=host.motion.arm_pwm();assert arm.success
    pump(110);pump(150)
    assert host.latest.armed and host.latest.control_mode==1
    pulse=host.motion.set_pwm(1000,-1000,200);assert pulse.success
    pump(160)
    assert host.command_results[pulse.request_id].status=='accepted'
    assert output()==(1000,-1000)
    assert not host.motion.set_pwm(1000,-1000,200).success
    pump(200);pump(350)
    assert output()==(0,0) and not host.latest.armed and host.latest.stop_reason==2

    parameters=dict(zip(PARAMETER_KEYS,(100000,10000,100000,10000)))
    result=host.parameters.apply_parameters(parameters);assert result.success
    pump(360);pump(400)
    assert host.actual_parameters==dict(parameters,revision=1)
    assert '确认' in host.parameter_status
    arm=host.motion.arm_speed();assert arm.success
    pump(410);pump(450)
    assert host.latest.control_mode==2 and host.latest.armed
    assert host.start_recording()
    pulse=host.motion.set_speed(100,-100,100);assert pulse.success
    pump(460);pump(500)
    assert output()[0]>0 and output()[1]<0
    stop=host.motion.stop();assert stop.success
    pump(510);pump(550)
    assert output()==(0,0) and not host.latest.armed and host.latest.stop_reason==1
    assert host.latest.device_last_seq==host.adapter.seq
    host.stop_recording()
    deadline=time.monotonic()+3
    while host.recorder.is_running and time.monotonic()<deadline:
        app.processEvents();host.poll();time.sleep(.005)
    assert not host.recorder.is_running
    csv_path=next(Path(directory).rglob('telemetry.csv'))
    with csv_path.open(encoding='utf-8-sig',newline='') as stream: rows=list(csv.DictReader(stream))
    assert len(rows)==2
    assert all(row['protocol']==PROJECT_V1 and row['connection_id']==host.connection_id for row in rows)
    assert rows[-1]['armed']=='False' and rows[-1]['device_last_seq']==str(host.adapter.seq)
    assert host.shutdown()
print('PASS: actual HostController <-> actual MCU C core: handshake, PWM expiry, PI RAM readback, speed, STOP, CSV recording.')
