"""Offscreen manual-count UI/recording QA with an injected byte link only."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
import sys
import json
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tests"))
from car_host.runtime import prepare_qt_runtime, prepare_qt_fonts
prepare_qt_runtime()
from PySide6.QtWidgets import QApplication
from car_host.controller import HostController
from car_host.encoder_panel import EncoderTotalsPanel
from car_host.encoder_verification import verify_encoder_recording
from car_host.project_protocol import (
    CAPS, ACK, STATE, ENCODER_CAPS, ENCODER_TOTALS, CAPS_LAYOUT, STATE_LAYOUT,
    ENCODER_CAPS_LAYOUT, ENCODER_TOTALS_LAYOUT, PROJECT_V1, encode_frame)
from car_host.transport import ConnectionConfig
from test_application_extensions import Link

class SoftwareLink(Link):
    source = "SIMULATION_PROJECT_V1"

root=Path(__file__).resolve().parents[1]
app=QApplication([]);prepare_qt_fonts()
host=HostController(root/".qa/encoder_totals_gui",worker_factory=SoftwareLink)
host.timer.stop()
assert host.connect_device(ConnectionConfig(port="SOFTWARE_ONLY",protocol=PROJECT_V1))
host.poll()
session=host.adapter.session
def state(tick,seq):
    return encode_frame(STATE,session,seq,STATE_LAYOUT.pack(
        tick,host.adapter.seq,0,0,0,0,0,0,11500,57,0,0,0,1,0,0,0,0))
def total(tick,seq,left,right):
    return encode_frame(ENCODER_TOTALS,session,seq,ENCODER_TOTALS_LAYOUT.pack(
        1,1,10,tick,tick//10,left,right,10,0))
host.worker.emit("rx",encode_frame(CAPS,session,1,CAPS_LAYOUT.pack(15,6000,300,1000,100,20,1))
    +encode_frame(ACK,session,1,b"\x01\x00")+state(1000,1))
host.poll()
panel=EncoderTotalsPanel(host);panel.resize(850,330)
panel.setWindowTitle("软件夹具验证 全量编码器五圈计数")
panel.show();app.processEvents()
panel.query_button.click()
seq=host.adapter.seq
host.worker.emit("rx",encode_frame(ENCODER_CAPS,session,seq,
    ENCODER_CAPS_LAYOUT.pack(1,64,100,20,0,1560,66000,160000))
    +encode_frame(ACK,session,seq,b"\x0a\x00")+state(1050,2)+total(1050,1,100,200))
host.poll();app.processEvents()
panel.begin_button.click()
assert host.encoder_totals.baseline is not None
host.worker.emit("rx",state(1100,3)+total(1100,2,7900,200))
host.poll()
host.worker.emit("rx",state(1150,4)+total(1150,3,7900,-7600))
host.poll()
host.worker.emit("rx",state(1350,5)+total(1350,7,7900,-7600))
host.poll();app.processEvents()
panel.finish_button.click()
assert host.encoder_totals.result["counts_per_left_turn"] == 1560
assert host.encoder_totals.result["counts_per_right_turn"] == 1560
deadline=time.monotonic()+3
while host.recorder.is_running and time.monotonic()<deadline:
    app.processEvents();host.poll();time.sleep(.01)
assert not host.recorder.is_running
audit=verify_encoder_recording(host.recorder.directory)
assert audit["verified"]
(host.recorder.directory/"encoder_verification.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
app.processEvents()
image=root/"docs/assets/encoder_totals_panel.png"
panel.grab().save(str(image))
assert [p[0][3] for p in host.worker.sent] == [1,10]
panel.close();assert host.shutdown()
print(json.dumps(dict(directory=str(host.recorder.directory),image=str(image),
    verified=True,counts_per_turn=1560,real_hardware_access=False),ensure_ascii=True))
