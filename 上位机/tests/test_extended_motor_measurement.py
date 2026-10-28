"""Extended CAPS and real recording boundaries on injected links only."""
import csv
import time

from car_host.measurement_analysis import verify_raw_recording
from car_host.project_protocol import CAPS, CAPS_LAYOUT, HELLO, ARM, GET_PROFILE_CAPS, PROFILE_CAPS, PROFILE_CAPS_LAYOUT, PROFILE_COMMAND_LAYOUT
from test_application_extensions import spin
from test_project_v1 import project_host, handshake, state_frame, reply, ack, update_state


def test_old_caps_reject_extended_motion_without_transmission(project_host):
    host=project_host
    handshake(host)
    assert host.motion.arm_pwm().success
    ack(host,ARM)
    update_state(host,tick=1020,mode=1,armed=1)
    before=len(host.worker.sent)
    assert not host.motion.set_pwm(10000,0,2000).success
    assert len(host.worker.sent)==before


def test_extended_caps_accept_exact_upper_boundary(project_host):
    host=project_host
    reply(host,CAPS,CAPS_LAYOUT.pack(15,10000,1180,2000,100,20,1),seq=1)
    ack(host,HELLO,seq=1)
    update_state(host)
    assert host.motion.query_profile_caps().success
    reply(host,PROFILE_CAPS,PROFILE_CAPS_LAYOUT.pack(3,1,2,10000,2000,100,20))
    ack(host,GET_PROFILE_CAPS)
    assert host.motion.arm_pwm().success
    ack(host,ARM)
    update_state(host,tick=1020,mode=1,armed=1)
    result=host.motion.set_pwm_profile([dict(left_pwm=9000,right_pwm=0,duration_ms=1000),
        dict(left_pwm=10000,right_pwm=0,duration_ms=1000)])
    assert result.success
    assert PROFILE_COMMAND_LAYOUT.unpack(host.worker.sent[-1][0][14:-2])[3:9]==(9000,0,10000,0,1000,2000)


def test_recording_starts_and_stops_at_real_frame_boundaries(project_host,qapp):
    host=project_host
    handshake(host)
    first=state_frame(host.adapter,tick=1050)
    host.worker.emit('rx',first[:20]);host.poll()
    assert host.start_recording()
    directory=host.recorder.directory
    host.worker.emit('rx',first[20:]);host.poll()
    assert not host._recording_rx_started
    second=state_frame(host.adapter,tick=1100)
    host.worker.emit('rx',second);host.poll()
    assert host._recording_rx_started
    third=state_frame(host.adapter,tick=1150)
    host.worker.emit('rx',third[:20]);host.poll()
    assert not host.stop_recording()
    assert host.recorder.is_active
    host.worker.emit('rx',third[20:]);host.poll()
    spin(qapp,lambda: not host.recorder.is_running)
    verified=verify_raw_recording(directory,[])
    assert verified['verified'],verified
    assert verified['csv_state_count']==2
    assert not host.recording_incomplete


def test_recording_boundary_timeout_preserves_failure(project_host,qapp):
    host=project_host
    handshake(host)
    assert host.start_recording()
    host.worker.emit('rx',state_frame(host.adapter,tick=1050)[:20]);host.poll()
    assert not host.stop_recording()
    host._recording_boundary_stop_deadline=time.monotonic()-1
    host.poll()
    spin(qapp,lambda: not host.recorder.is_running)
    assert host.recording_incomplete
    assert not verify_raw_recording(host.recorder.directory,[])['verified']
