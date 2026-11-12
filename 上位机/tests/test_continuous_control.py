"""Necessary keyboard lifecycle and leased-control failure checks; fake link only."""
from dataclasses import replace
from pathlib import Path
import struct
import pytest
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from car_host.continuous_models import KeyboardState, DiscreteDrivingState, DrivingEvent, OMEGA_LIMIT
from car_host.yaw_models import YawConfig
from car_host.preferences import AppPreferences
from test_speed_control import speed_host, confirm


def prepare(h,advance,mode='legacy'):
    s=h.continuous_control;s.timer.stop()
    s.set_mode(mode)
    confirm(h,advance,s.query_capabilities)
    confirm(h,advance,h.yaw_control.query_capabilities)
    confirm(h,advance,lambda:h.yaw_control.apply_config(YawConfig()))
    confirm(h,advance,h.yaw_control.calibrate)
    for _ in range(45):advance()
    assert s.ready
    return s


def test_discrete_axes_events_and_independent_amplitudes():
    for drive in (-1,0,1):
        for turn in (-1,0,1):
            k=DiscreteDrivingState(drive=drive,turn=turn)
            assert k.target()==pytest.approx((drive*.1,turn*.3*(-1 if drive<0 else 1)))
    k=DiscreteDrivingState();k.apply(['up','left']);k.apply(['up'])
    assert (k.drive,k.turn)==(1,1)
    k.apply(['right']);assert k.turn==0
    k.apply(['right']);assert k.turn==-1
    k.apply(['down','up']);assert k.drive==0
    k.apply(['w','d']);assert k.target()==pytest.approx((0.,-.4))
    k.base_v=k.speed_limit;k.base_omega=k.omega_limit
    k.apply(['w','d']);k.apply(['s','a'])
    assert k.base_v==pytest.approx(1.13) and k.base_omega==pytest.approx(3.65)
    k.base_v=0;k.apply(['down']);assert k.drive==-1 and k.target()[0]==0
    k.apply(['w']);assert k.target()[0]==pytest.approx(-.05)
    k.clear();assert k.target()==(0.,0.)


def tap(s,key):
    s.press(key);s.release(key)


def test_discrete_default_scheduler_dedup_stop_and_mode_switch(speed_host):
    h,advance,clock=speed_host
    assert h.continuous_control.mode=='discrete'
    s=prepare(h,advance,'discrete');confirm(h,advance,s.arm)
    tap(s,'up');tap(s,'left')
    for _ in range(12):advance()
    assert s.running and s.keys.target()==pytest.approx((.1,.3))
    assert s.latest['requested_omega']==pytest.approx(.3)
    sent_before_change=sum(m['command']=='continuous_target' for m in h.command_metadata.values())
    event=DrivingEvent('brain-test',1,clock.now,'w')
    s.apply_events([event]);s.apply_events([event])
    assert s.keys.base_v==pytest.approx(.15)
    s.apply_events([DrivingEvent('brain-test',2,clock.now,'w')])
    assert s.keys.base_v==pytest.approx(.2)
    s.apply_events([DrivingEvent('brain-test',3,clock.now-2,'down')])
    assert s.keys.drive==1
    s.press('left');s.press('right');s.flush_input()
    assert s.keys.turn==0
    s.release('left');s.release('right')
    for _ in range(50):advance()
    targets=[m for m in h.command_metadata.values() if m['command']=='continuous_target']
    intervals=[b['queued_monotonic']-a['queued_monotonic'] for a,b in zip(targets,targets[1:])]
    assert len(intervals)>=2 and intervals==pytest.approx([.5]*len(intervals))
    changed=targets[sent_before_change]['values']
    assert (changed['v'],changed['omega'])==pytest.approx((.2,0.))
    s.set_mode('legacy');advance()
    assert not s.engaged and not h.latest.armed and s.keys.target()==(0.,0.)
    assert (h.data_directory/'continuous_events.jsonl').exists()
    assert s.last_stop['reason'].startswith('切换驾驶模式')
    confirm(h,advance,s.arm);assert s.keys.target()==(0.,0.)
    s.press('up');advance();s.release('up');advance(10)
    assert h.worker.continuous.current==[0.,0.]


@pytest.mark.parametrize('delay',[300,500,600])
def test_discrete_ack_jitter_and_deadline(speed_host,delay):
    h,advance,_=speed_host;s=prepare(h,advance,'discrete');confirm(h,advance,s.arm)
    h.worker.faults['ack_delay_ms']=delay
    tap(s,'up')
    for _ in range(12):advance()
    tap(s,'left');tap(s,'right');tap(s,'right');tap(s,'w');tap(s,'w')
    assert sum(p['command']=='continuous_target' for p in h._project_pending.values())==1
    for _ in range(11):advance()
    assert s.engaged==(delay<600),s.status
    if delay<600:
        assert s.latest['requested_v']==pytest.approx(.2)
        assert s.latest['requested_omega']==pytest.approx(-.3)
        targets=[m['values'] for m in h.command_metadata.values() if m['command']=='continuous_target']
        assert all(t['omega']<=0 for t in targets) # 未发送等待期间的历史左转
    if delay==600:
        assert s.last_stop['code']=='ack_timeout'
        count=len(h.worker.sent);h.worker.faults.clear()
        for _ in range(25):advance()
        assert len(h.worker.sent)==count and not h.latest.armed


@pytest.mark.parametrize('stream',['continuous','state'])
def test_discrete_single_stream_gap_drop_and_recovery(speed_host,stream,monkeypatch):
    h,advance,_=speed_host;s=prepare(h,advance,'discrete');confirm(h,advance,s.arm)
    tap(s,'up')
    for _ in range(12):advance()
    # A counter increase alone must not end control if MCU remains enabled.
    h.latest=replace(h.latest,rx_dropped=h.latest.rx_dropped+1)
    s.poll();assert s.running and s.drop_delta==(1,0)
    if stream=='continuous':
        h.worker.faults['continuous_stale']=True
    else:
        saved=h.worker._state;monkeypatch.setattr(h.worker,'_state',lambda now:None)
    for _ in range(15):advance() # 750ms gap, beyond old limits
    assert s.running,s.status
    if stream=='continuous':h.worker.faults.clear()
    else:monkeypatch.setattr(h.worker,'_state',saved)
    advance();assert s.running
    if stream=='continuous':h.worker.faults['continuous_stale']=True
    else:monkeypatch.setattr(h.worker,'_state',lambda now:None)
    for _ in range(22):advance()
    assert not s.engaged and s.last_stop['code']==('telemetry_timeout' if stream=='continuous' else 'state_timeout')


def test_discrete_budget_grace_hard_overflow_and_capture_time(speed_host,monkeypatch):
    h,advance,clock=speed_host;s=prepare(h,advance,'discrete');confirm(h,advance,s.arm)
    tap(s,'up')
    for _ in range(12):advance()
    count=len(h.worker.sent)
    for _ in range(8):
        clock.now+=.05;h.worker.advance();h.pump_io()
        h.io_backlog_exceeded=True;s.poll()
    assert s.running and len(h.worker.sent)==count
    h.io_backlog_exceeded=False;s.poll();assert s.running
    for _ in range(12):
        clock.now+=.05;h.worker.advance();h.pump_io();h.io_backlog_exceeded=True;s.poll()
    assert not s.engaged and s.last_stop['code']=='backlog'
    advance();confirm(h,advance,s.arm);tap(s,'up');advance()
    monkeypatch.setattr(h.worker,'take_overflow_count',lambda:1)
    h.pump_io();assert not s.engaged and s.last_stop['code']=='overflow'


def test_v1_continuous_caps_require_firmware_upgrade(speed_host):
    h,advance,_=speed_host;s=h.continuous_control
    h.worker.faults['continuous_version']=1
    r=s.query_capabilities();advance()
    assert h.command_results[r.request_id].status=='rejected'
    assert '需更新' in s.status and s.caps is None and not s.arm().success


def test_discrete_qt_latch_repeat_suppression_and_focus_stop(speed_host,qapp):
    from car_host.ui import MainWindow
    h,advance,_=speed_host;s=prepare(h,advance,'discrete')
    window=MainWindow(h,preferences=replace(AppPreferences(),auto_connect=False))
    p=window.continuous_panel
    window.workspace_tabs.setCurrentWidget(p);window.show();window.activateWindow();qapp.processEvents()
    try:
        p.buttons['arm'].click();advance();assert s.running and p.mode.currentData()=='discrete'
        QTest.keyClick(p.pad,Qt.Key.Key_Up);QTest.keyClick(p.pad,Qt.Key.Key_Left)
        QTest.keyClick(p.pad,Qt.Key.Key_W);QTest.keyClick(p.pad,Qt.Key.Key_D)
        for _ in range(12):advance()
        p.refresh()
        assert not s.keys.held and s.keys.target()==pytest.approx((.15,.4))
        assert p.base_v.value()==pytest.approx(.15) and '前进 / 左转' in p.targets.text()
        repeat=QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_W,Qt.KeyboardModifier.NoModifier,'',True)
        qapp.sendEvent(p.pad,repeat);assert s.keys.base_v==pytest.approx(.15)
        QTest.keyClick(p.pad,Qt.Key.Key_Right)
        for _ in range(15):advance()
        assert s.keys.turn==0 and h.worker.continuous.current[1]==0 and s.running
        for width,height in ((1000,720),(1320,900)):
            window.resize(width,height);qapp.processEvents();p.refresh()
            p.scroll_area.ensureWidgetVisible(p.stop_diagnostic);qapp.processEvents()
            assert window.grab().save(str(Path(__file__).resolve().parents[1]/f'docs/assets/discrete_{width}.png'))
        p.mode.setCurrentIndex(1);advance()
        assert not s.engaged and not h.latest.armed and s.mode=='legacy'
        p.mode.setCurrentIndex(0);advance();p.refresh();p.buttons['arm'].click();advance()
        assert s.running and s.keys.target()==(0.,0.)
        QTest.keyClick(p.pad,Qt.Key.Key_Up);advance()
        p.buttons['status'].setFocus();advance()
        assert not s.engaged and not h.latest.armed and s.keys.target()==(0.,0.)
    finally:
        p.timer.stop();window.close();qapp.processEvents()


def test_continuous_capture_age_and_replayed_frames_cannot_refresh(speed_host):
    from car_host.project_protocol import ProjectEvent
    from car_host.continuous_models import STATE
    h,advance,clock=speed_host;s=prepare(h,advance,'discrete');confirm(h,advance,s.arm)
    tap(s,'up')
    for _ in range(12):advance()
    values=[s.latest[n] for n in ('tick','accepted_seq','revision','deadline','requested_v','requested_omega',
                                'v','omega','measured_omega','left_ref','right_ref','pwm_left','pwm_right','gyro_age','flags')]
    received=s.latest['received']
    s.diagnostic(ProjectEvent(0x9a,h.adapter.session,s._last_report_seq,(STATE.pack(*values),),clock.now))
    assert s.latest['received']==received
    values[0]+=10
    s.diagnostic(ProjectEvent(0x9a,h.adapter.session,s._last_report_seq+1,(STATE.pack(*values),),clock.now-1.1))
    assert s.latest['received']==pytest.approx(clock.now-1.1)
    s.poll();assert not s.engaged and s.last_stop['code']=='telemetry_timeout'


def test_vector_combinations_scaling_and_no_repeat_burst():
    for held,expected in (
            (('up','left'),(.1,.3)),(('up','right'),(.1,-.3)),
            (('down','left'),(-.1,-.3)),(('down','right'),(-.1,.3)),
            (('left',),(0.,.3)),(('right',),(0.,-.3)),
            (('up','down','left'),(0.,.3)),(('down','left','right'),(-.1,0.))):
        assert KeyboardState(held=set(held)).target()==pytest.approx(expected)
    k=KeyboardState();k.press('down',0);k.press('left',0)
    assert k.target()==pytest.approx((-.1,-.3))
    k.press('z',0);assert k.target()==pytest.approx((-.11,-.33))
    k.poll(.2);assert k.multiplier==pytest.approx(1.21)
    k.poll(5);assert k.multiplier==pytest.approx(1.331)  # one step, no event-loop catch-up
    k.press('x',5);before=k.multiplier;k.poll(6);assert k.multiplier==before
    k.press('up',6);assert k.target()[0]==0
    k.multiplier=10;k.release('up',6)
    v,w=k.target();assert w==-3. and v==pytest.approx(-1.)
    k.clear();assert k.target()==(0.,0.)
    k.press('z',7);assert k.target()==(0.,0.)


def test_old_firmware_caps_cannot_enable_or_send_motion(speed_host):
    h,advance,_=speed_host;s=h.continuous_control
    h.worker.faults['continuous_unsupported']=True
    result=s.query_capabilities();assert result.success
    advance()
    assert h.command_results[result.request_id].status=='rejected'
    assert s.caps is None and not h.adapter.continuous_negotiated and not s.arm().success
    before=len(h.worker.sent);s.press('up')
    for _ in range(20):advance()
    assert len(h.worker.sent)==before and not h.latest.armed


def test_turn_speed_decreases_from_actual_limit_without_hidden_multiplier():
    for direction in ('left','right'):
        keys=KeyboardState(base_omega=3.,multiplier=10.)
        keys.press(direction,0.)
        assert abs(keys.target()[1])==pytest.approx(OMEGA_LIMIT)
        keys.press('x',0.)
        assert abs(keys.target()[1])==pytest.approx(OMEGA_LIMIT*.9)
        keys.release('x',0.)
        keys.press('z',0.)
        assert abs(keys.target()[1])==pytest.approx(OMEGA_LIMIT*.99)
        keys.poll(.2);keys.poll(.4)
        assert abs(keys.target()[1])==pytest.approx(OMEGA_LIMIT)
        keys.release('z',.4);keys.press('x',.4)
        assert abs(keys.target()[1])==pytest.approx(OMEGA_LIMIT*.9)


def test_legacy_caps_still_limits_targets_to_one(speed_host):
    h,advance,_=speed_host;h.worker.faults['continuous_omega_limit']=1.
    s=prepare(h,advance);confirm(h,advance,s.arm)
    s.press('left')
    for _ in range(30):
        s.press('z');s.release('z')
    for _ in range(12):advance()
    assert s.caps['omega_limit']==1. and s.latest['requested_omega']==pytest.approx(1.)
    assert not h.send_command('continuous_target',dict(v=0.,omega=1.1)).success
    s.press('x');s.release('x')
    for _ in range(12):advance()
    assert s.latest['requested_omega']==pytest.approx(.9)


def test_qt_keyboard_release_focus_tab_stop_and_layout(speed_host,qapp):
    from car_host.ui import MainWindow
    h,advance,_=speed_host;s=prepare(h,advance)
    window=MainWindow(h,preferences=replace(AppPreferences(),auto_connect=False))
    window.workspace_tabs.setCurrentWidget(window.continuous_panel)
    window.show();window.activateWindow();qapp.processEvents()
    p=window.continuous_panel;p.timer.stop();p.refresh()
    try:
        p.buttons['arm'].click();advance();assert s.running and p.pad.hasFocus(),s.status
        QTest.keyPress(p.pad,Qt.Key.Key_Up);QTest.keyPress(p.pad,Qt.Key.Key_Left)
        for _ in range(12):advance()
        assert s.latest['requested_v']==pytest.approx(.1) and s.latest['requested_omega']==pytest.approx(.3)
        QTest.keyPress(p.pad,Qt.Key.Key_Z);assert s.keys.multiplier==pytest.approx(1.1)
        for _ in range(5):advance()
        assert s.keys.multiplier==pytest.approx(1.21)
        QTest.keyRelease(p.pad,Qt.Key.Key_Z)
        for _ in range(15):
            QTest.keyPress(p.pad,Qt.Key.Key_Z);QTest.keyRelease(p.pad,Qt.Key.Key_Z)
        for _ in range(30):advance()
        expected=.3*s.keys.multiplier
        assert p.base_w.value()==.3 and p.base_w.maximum()==OMEGA_LIMIT
        assert s.latest['requested_omega']==pytest.approx(expected) and s.latest['omega']>1.
        QTest.keyPress(p.pad,Qt.Key.Key_X);QTest.keyRelease(p.pad,Qt.Key.Key_X)
        for _ in range(14):advance()
        assert s.latest['requested_omega']==pytest.approx(expected*.9)
        repeated=QKeyEvent(QEvent.Type.KeyRelease,Qt.Key.Key_Up,Qt.KeyboardModifier.NoModifier,'',True)
        qapp.sendEvent(p.pad,repeated);assert 'up' in s.keys.held
        QTest.keyRelease(p.pad,Qt.Key.Key_Left);advance(10)
        assert s.latest['requested_omega']==0 or h.worker.continuous.requested[1]==0
        assert h.worker.continuous.current[1]==0
        QTest.keyRelease(p.pad,Qt.Key.Key_Up);advance(10)
        assert h.worker.pwm_left==h.worker.pwm_right==0 and s.running
        QTest.keyPress(p.pad,Qt.Key.Key_Down)
        QTest.keyPress(p.pad,Qt.Key.Key_Left)
        for _ in range(12):advance()
        assert h.worker.continuous.requested[0]<0 and h.worker.continuous.requested[1]<0
        QTest.keyRelease(p.pad,Qt.Key.Key_Left)
        QTest.keyPress(p.pad,Qt.Key.Key_Right)
        for _ in range(12):advance()
        assert h.worker.continuous.requested[0]<0 and h.worker.continuous.requested[1]>0
        QTest.keyRelease(p.pad,Qt.Key.Key_Right)
        QTest.keyPress(p.pad,Qt.Key.Key_Space);advance()
        assert not h.latest.armed and not s.engaged and not s.keys.held
        QTest.keyRelease(p.pad,Qt.Key.Key_Down);QTest.keyRelease(p.pad,Qt.Key.Key_Space)
        advance();p.refresh();p.buttons['arm'].click();advance();assert s.running
        assert s.keys.target()==(0.,0.)
        QTest.keyPress(p.pad,Qt.Key.Key_Up);p.buttons['status'].setFocus();advance()
        assert not s.engaged and not h.latest.armed
        advance();p.refresh();p.buttons['arm'].click();advance();assert s.running
        window.workspace_tabs.setCurrentWidget(window.speed_page);advance()
        assert not s.engaged and not h.latest.armed and not s.keys.held
        window.workspace_tabs.setCurrentWidget(p)
        advance();p.refresh();p.buttons['arm'].click();advance();assert s.running
        qapp.sendEvent(window,QEvent(QEvent.Type.WindowDeactivate));advance()
        assert not s.engaged and not h.latest.armed and not s.timer.isActive()
        for width,height in ((1000,720),(1320,900)):
            window.resize(width,height);qapp.processEvents();p.refresh()
            assert window.width()==width and window.height()==height
            assert p.pad.height()>=200
            p.scroll_area.ensureWidgetVisible(p.feedback)
            qapp.processEvents()
            bottom = p.feedback.mapTo(p.scroll_area.viewport(), p.feedback.rect().bottomRight())
            assert 0 <= bottom.y() < p.scroll_area.viewport().height()
            assert window.grab().save(str(Path(__file__).resolve().parents[1]/f'docs/assets/continuous_{width}.png'))
        window.connection_shortcut.click()
        assert window.workspace_tabs.tabText(window.workspace_tabs.currentIndex())=='设备与调试'
    finally:
        p.timer.stop();window.close();qapp.processEvents()


def test_2hz_long_run_bounded_history_and_priority_zero(speed_host):
    h,advance,_=speed_host;s=prepare(h,advance);confirm(h,advance,s.arm)
    s.press('up')
    for _ in range(1500):advance()
    assert s.running and s.latest['requested_v']==pytest.approx(.1),s.status
    targets=[m for m in h.command_metadata.values() if m['command']=='continuous_target']
    assert all(b['queued_monotonic']-a['queued_monotonic']==pytest.approx(.5) for a,b in zip(targets,targets[1:]))
    assert len(h.command_metadata)<=128 and len(s.live)<=600
    h.worker.faults['ack_delay_ms']=150
    for _ in range(10):advance()
    s.release('up');advance(10)
    assert h.worker.pwm_left==h.worker.pwm_right==0
    for _ in range(4):advance()
    assert s.running and s.keys.target()==(0.,0.),s.status
    s.cancel();advance();count=len(h.worker.sent)
    for _ in range(20):advance()
    assert not h.latest.armed and len(h.worker.sent)==count


@pytest.mark.parametrize('fault',['missingack','yaw_stale_gyro','continuous_stale','stale'])
def test_fault_never_replays_or_rearms(speed_host,fault):
    h,advance,_=speed_host;s=prepare(h,advance);confirm(h,advance,s.arm)
    s.press('up')
    for _ in range(12):advance()
    h.worker.faults[fault]=29 if fault=='missingack' else True
    for _ in range(15):advance()
    assert not s.engaged and not h.worker.armed,s.status
    count=len(h.worker.sent);h.worker.faults.clear()
    for _ in range(15):advance()
    assert len(h.worker.sent)==count and not h.latest.armed and not s.keys.held
