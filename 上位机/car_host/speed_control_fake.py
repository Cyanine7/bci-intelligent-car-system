"""Explicit PROJECT_V1_FAKE v2 device; host reference algorithm, no physical I/O."""
from collections import deque
from dataclasses import replace
import math
import struct
from .speed_control_models import *
from .speed_control_analysis import ReferenceController,MODELS

class FakeSpeedDevice:
    def __init__(self,worker):
        self.worker=worker;self.config=None;self.revision=0;self.controllers=[]
        self.reset_session()
    def reset_session(self):
        self.negotiated=False;self.diag_seq=0;self.run=0;self.stage=0;self.kind=0;self.segment=255
        self.start=self.switch=self.end=self.reason=0;self.snapshot=bytes(36);self.run_revision=0
        self.command=bytes(20);self.segments=[];self.samples=[];self.pre=deque(maxlen=10);self.latest=None
        self.plant=[0.,0.];self.remainders=[0.,0.];self.measured=[0.,0.];self._pending_start=False
    @property
    def ready(self):return not self.worker.faults.get('speed_unimplemented')
    @property
    def busy(self):return self.stage in (1,2)
    def emit(self,kind,seq,payload,now=None):
        from .project_protocol import encode_frame
        self.worker._emit('rx',encode_frame(kind,self.worker.session,seq,payload),monotonic=now)
    def stop(self,reason):
        if self.stage==1:self.stage=2;self.end=self.worker.tick_ms;self.reason=reason
        self._pending_start=False
        for c in self.controllers:c.reset()
    def execute(self,kind,body):
        w=self.worker
        if kind==2:
            mode=body[0]
            if self.busy or w.armed:return 4
            if not self.negotiated:return 9
            if mode==3 and (not self.ready or self.config is None):return 7
            if not w.local_enable:return 5
            w._stop(0);w._clear_profile();w.mode=mode;w.armed=True
            w._idle_deadline=(w.tick_ms+5000)&0xffffffff
            if mode==3:self.controllers=[ReferenceController(self.config.algorithm,kp,ki,self.config.pwm_limit) for kp,ki in self.config.gains]
            return 0
        if kind in (14,17):
            if not self.negotiated:return 9
            try:
                segments,deadline=decode_step(STEP_STRUCT.unpack(body),pwm=kind==17)
            except (ValueError,struct.error):return 1
            if not w.local_enable:return 5
            if not w.armed or w.mode!=(4 if kind==17 else 3) or w._deadline is not None:return 4
            remaining=(deadline-w.tick_ms)&0xffffffff
            if not 0<remaining<=3000:return 8
            self.run=w.seq;self.run_revision=self.revision;self.snapshot=self.config.pack() if kind==14 else bytes(36)
            self.kind=1 if kind==14 else 2;self.command=body;self.segments=segments
            self.accepted=w.tick_ms;self._pending_start=True;self.stage=1;self.segment=255
            self.samples=[];self.reason=0;self.start=self.switch=self.end=0
            w._deadline=(w.tick_ms+min(sum(s['duration_ms'] for s in segments),remaining))&0xffffffff
            w._idle_deadline=None;return 0
        if w.armed or self.busy:return 4
        if kind==11:
            if w.faults.get('speed_unsupported'):return 9
            if body:return 1
            self.negotiated=True
            flags=(1 if w.faults.get('speed_no_target_filter') else 3) if self.ready else 0
            self.emit(SPEED_CAPS,w.seq,CAPS_STRUCT.pack(2,flags,100,16500,SPEED_LIMIT_MM_S,3000,384,1560,66000,1e6,4e7,100,200));return 0
        if not self.negotiated:return 9
        if kind in (12,13):
            if kind==12 and body:return 1
            if kind==13:
                try:cfg=SpeedConfig.unpack(CONFIG_STRUCT.unpack(body))
                except (ValueError,struct.error):return 1
                if not self.ready:return 7
                self.config=cfg;self.revision=(self.revision+1)&0xffffffff
            self.emit(SPEED_CONFIG,w.seq,struct.pack('<I',self.revision)+(self.config.pack() if self.config else bytes(36)));return 0
        if kind==15:
            if body:return 1
            self.emit(SPEED_INFO,w.seq,struct.pack('<IIIIHBBBBH',self.run,self.run_revision,self.start,self.end,
                len(self.samples),self.stage,self.reason,self.kind,self.segment,0)+self.snapshot);return 0
        if kind==18:
            if body:return 1
            self.emit(SPEED_META,w.seq,struct.pack('<IIIIBBH',self.run,self.start,self.switch,self.end,self.kind,self.segment,0)+self.command);return 0
        if kind==16:
            if len(body)!=8:return 1
            run,start,count=struct.unpack('<IHH',body)
            if run!=self.run or not run or self.stage<3 or not 1<=count<=8 or start+count>len(self.samples):return 1
            for index in range(start,start+count):
                if index==w.faults.get('speed_missing_index'):continue
                self.emit(SPEED_DATA,w.seq,struct.pack('<IHH',run,index,0)+self.samples[index].pack())
            return 0
        return 9
    def tick(self):
        w=self.worker
        if not self.negotiated:return
        if self._pending_start:
            self.start=w.tick_ms;self._pending_start=False
            self.samples=[replace(s,flags=PRE) for s in self.pre]
        active=w.armed and w.mode in (3,4) and w._deadline is not None
        if active:
            segment=int(((w.tick_ms-self.accepted)&0xffffffff)>=self.segments[0]['duration_ms'])
            if segment==1 and self.segment!=1:self.switch=w.tick_ms
            self.segment=segment
        flags=SECOND if active and self.segment==1 else 0
        filtered=[0.,0.];raw=[0.,0.];pwm=[0,0];enc=[];targets=[0,0]
        scale=.066*math.pi/1560
        for side,(K,tau) in enumerate(MODELS['空载']):
            self.remainders[side]+=self.plant[side]*.01/scale
            n=int(self.remainders[side]);self.remainders[side]-=n;enc.append(n)
            self.measured[side]=n*scale/.01
            if active:
                target=self.segments[self.segment]['left' if side==0 else 'right'];targets[side]=target
                if self.kind==2:raw[side]=target;pwm[side]=target
                else:
                    f,u,p,sat,_=self.controllers[side].step(target/1000,self.measured[side],.01)
                    filtered[side]=f;raw[side]=u;pwm[side]=p
                    if sat:flags|=SAT_L if side==0 else SAT_R
            self.plant[side]=self.plant[side]*math.exp(-.01/tau)+K*pwm[side]*(1-math.exp(-.01/tau))
        if active:w.pwm_left,w.pwm_right=pwm
        self.latest=SpeedSample(w.tick_ms,10,flags,*targets,*filtered,*self.measured,*raw,*pwm,12000,0,*enc)
        if self.stage in (1,2):
            if len(self.samples)>=384:self.stage=4;w._stop(6)
            else:
                self.samples.append(replace(self.latest,flags=flags|(ACTIVE if self.stage==1 else POST)))
                if self.stage==2 and ((w.tick_ms-self.end)&0xffffffff)>=200:self.stage=3
        self.pre.append(self.latest)
    def report(self,now):
        if self.negotiated and self.latest:
            self.diag_seq+=1;self.emit(SPEED_DIAG,self.diag_seq,struct.pack('<I',self.run)+self.latest.pack(),now)

