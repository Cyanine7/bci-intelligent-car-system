"""Necessary keyboard lifecycle and leased-control failure checks; fake link only."""
from dataclasses import replace
from pathlib import Path
import struct
import pytest
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from car_host.continuous_models import KeyboardState, OMEGA_LIMIT
from car_host.yaw_models import YawConfig
from car_host.preferences import AppPreferences
from test_speed_control import speed_host, confirm


def prepare(h,advance):
    s=h.continuous_control;s.timer.stop()
    confirm(h,advance,s.query_capabilities)
    confirm(h,advance,h.yaw_control.query_capabilities)
    confirm(h,advance,lambda:h.yaw_control.apply_config(YawConfig()))
    confirm(h,advance,h.yaw_control.calibrate)
    for _ in range(45):advance()
    assert s.ready
    return s


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
        for _ in range(4):advance()
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
            assert p.pad.height()>=200 and p.feedback.geometry().bottom()<p.height()
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
