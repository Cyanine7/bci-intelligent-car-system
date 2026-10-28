"""PROJECT_V1_FAKE角速度模型；只生成软件记录，不访问实物。"""
from collections import deque
from dataclasses import replace
import math
import struct
from .yaw_models import CAPS, CONFIG, INFO, STEP, WHEEL_SPEED_LIMIT, YawConfig, YawSample, validate_segments
from .motor_models import MotorModel
from .speed_control_analysis import ReferenceController

class FakeYawDevice:
    def __init__(self,worker):
        self.worker=worker;self.config=None;self.revision=0;self.calibration=0;self.reset_session()
    def reset_session(self):
        self.negotiated=False;self.stage=0;self.run=0;self.start=self.switch=self.end=self.reason=0
        self.kind=0;self.segment=255;self.command=bytes(28);self.snapshot=bytes(60);self.run_revision=0
        self.samples=[];self.pre=deque(maxlen=10);self.latest=None;self.plant=[0.,0.];self.measured=[0.,0.]
        self.controllers=[];self.output=self.error=0.;self.diag_seq=0;self.gyro_seq=0;self.gyro_tick=0;self.gyro=0.
        self.calibration=0;self.calibration_count=0;self.calibration_start=0;self.bias=0.;self.segments=[]
    @property
    def busy(self):return self.stage in (1,2) or self.calibration==1
    def emit(self,kind,seq,payload,now=None):
        from .project_protocol import encode_frame
        self.worker._emit('rx',encode_frame(kind,self.worker.session,seq,payload),monotonic=now)
    def stop(self,reason):
        if self.stage==1:self.stage=2;self.end=self.worker.tick_ms;self.reason=reason
        if self.calibration==1:self.calibration=3
        self.output=self.error=0.
        for c in self.controllers:c.reset()
    def state(self):
        w=self.worker;valid=not w.faults.get('yaw_stale_gyro')
        raw=self.gyro+float(w.faults.get('yaw_bias',0));sign=self.config.sign if self.config else 1
        return struct.pack('<BBHIIfffII',self.calibration,int(valid),self.calibration_count,self.gyro_seq,self.gyro_tick,
            raw,self.bias,sign*(raw-self.bias),self.calibration_start,(w.tick_ms-self.gyro_tick)&0xffffffff)
    def execute(self,kind,body):
        w=self.worker
        if kind==23:
            if body:return 1
            self.emit(0x91,w.seq,self.state());return 0
        if kind==2:
            if not self.negotiated:return 9
            if self.busy or w.speed.busy or w.armed:return 4
            if not self.config or self.calibration!=2:return 7
            if not w.local_enable:return 5
            w._stop(0);w._clear_profile();w.mode=5;w.armed=True;w._idle_deadline=(w.tick_ms+5000)&0xffffffff;return 0
        if kind==24:
            if not self.negotiated:return 9
            if not w.local_enable:return 5
            if not w.armed or w.mode!=5 or w._deadline is not None:return 4
            if not self.config or self.calibration!=2 or w.faults.get('yaw_stale_gyro'):return 7
            try:
                v,typ,r,v0,t0,v1,t1,first,total,deadline=STEP.unpack(body)
                if v!=1 or r:return 1
                segments=validate_segments([dict(v=v0,target=t0,duration_ms=first),dict(v=v1,target=t1,duration_ms=total-first)],typ)
            except (ValueError,struct.error):return 1
            remaining=(deadline-w.tick_ms)&0xffffffff
            if not 0<remaining<=10000:return 8
            self.run=w.seq;self.run_revision=self.revision;self.snapshot=self.config.pack();self.command=body
            self.stage=1;self.kind=typ;self.segment=255;self.accepted=w.tick_ms;self.start=self.switch=self.end=0
            self.segments=segments;self.samples=[replace(s,flags=(s.flags&~(2|512|1024))|1) for s in self.pre];self.reason=0
            self.controllers=[ReferenceController(2,kp,ki,self.config.pwm_limit) for kp,ki in
                ((self.config.kp_left,self.config.ki_left),(self.config.kp_right,self.config.ki_right))]
            w.speed.stage=0;w.speed.samples=[];w.speed.run=0
            w._deadline=(w.tick_ms+min(remaining,total))&0xffffffff;w._idle_deadline=None;return 0
        if w.armed or self.busy or w.speed.busy:return 4
        if kind==19:
            if body:return 1
            self.negotiated=True;self.emit(0x8f,w.seq,CAPS.pack(1,3,100,16500,round(WHEEL_SPEED_LIMIT*1000),5000,10000,1040,60,.16,1.));return 0
        if not self.negotiated:return 9
        if kind in (20,21):
            if kind==20 and body:return 1
            if kind==21:
                try:self.config=YawConfig.unpack(body)
                except (ValueError,struct.error):return 1
                self.revision+=1
            self.emit(0x90,w.seq,struct.pack('<I',self.revision)+(self.config.pack() if self.config else bytes(60)));return 0
        if kind==22:
            if body:return 1
            self.calibration=1;self.calibration_start=w.tick_ms;self.calibration_count=0
            self.emit(0x91,w.seq,self.state());return 0
        if kind==25:
            if body:return 1
            self.emit(0x92,w.seq,INFO.pack(self.run,self.run_revision,self.start,self.end,len(self.samples),self.stage,self.reason,self.kind,self.segment,0,self.bias))
            self.emit(0x96,w.seq,struct.pack('<I',self.run_revision)+self.snapshot);return 0
        if kind==27:
            if body:return 1
            self.emit(0x93,w.seq,struct.pack('<IIIIBBH',self.run,self.start,self.switch,self.end,self.kind,self.segment,0)+self.command);return 0
        if kind==26:
            try:run,start,n=struct.unpack('<IHH',body)
            except struct.error:return 1
            if run!=self.run or not run or self.stage<3 or not 1<=n<=8 or start+n>len(self.samples):return 1
            for i in range(start,start+n):
                if i==w.faults.get('yaw_missing_index'):continue
                self.sample_reply(w.seq,i,self.samples[i],False)
            return 0
        return 9
    def sample_reply(self,seq,index,s,live):
        raw=s.pack()
        for part in (0,1):
            self.emit((0x97 if live else 0x94)+part,seq,struct.pack('<IHBB',self.run,index,part,0)+raw[(48 if part else 0):(84 if part else 48)])
    def tick(self):
        w=self.worker
        if not self.negotiated:return
        if w._tick_number%2==0 and not w.faults.get('yaw_stale_gyro'):
            self.gyro_seq=(self.gyro_seq+1)&0xffffffff;self.gyro_tick=w.tick_ms
            self.gyro=(self.plant[1]-self.plant[0])/.16
            if self.calibration==1:self.calibration_count+=1
        if self.calibration==1 and ((w.tick_ms-self.calibration_start)&0xffffffff)>=2000:
            self.calibration=2 if self.calibration_count>=80 and not any(self.plant) else 3
            if self.calibration==2:self.bias=float(w.faults.get('yaw_bias',0))
        active=w.armed and w.mode==5 and w._deadline is not None
        valid=((w.tick_ms-self.gyro_tick)&0xffffffff)<=60
        flags=(32 if valid else 0)|(64 if self.calibration==2 else 0)
        values=dict(v=0.,target=0.,raw_gyro=self.gyro+float(w.faults.get('yaw_bias',0)),omega=0.,integral=0.,raw_delta=0.,delta=0.,
                    left_ref=0.,right_ref=0.,left_filtered=0.,right_filtered=0.,left=self.measured[0],right=self.measured[1],raw_left=0.,raw_right=0.,pwm_left=0,pwm_right=0)
        values['omega']=(self.config.sign if self.config else 1)*(values['raw_gyro']-self.bias)
        if active and not valid:flags|=128;w._stop(6);active=False
        if active:
            if self.segment==255:self.segment=0;self.start=w.tick_ms
            if self.segment==0 and ((w.tick_ms-self.accepted)&0xffffffff)>=self.segments[0]['duration_ms']:self.segment=1;self.switch=w.tick_ms
            if self.segment==1:flags|=512
            s=self.segments[self.segment];values['v']=s['v'];values['target']=s['target'];e=s['target']-values['omega']
            raw=s['target'] if self.kind==2 else self.output+self.config.kp*(e-self.error)+self.config.ki*e*.01
            limit=min(self.config.delta_limit,2*(WHEEL_SPEED_LIMIT-abs(s['v'])));delta=max(-limit,min(limit,raw))
            if not s['v'] and not s['target']:raw=delta=e=0.
            self.output=delta;self.error=e;values.update(raw_delta=raw,delta=delta,integral=0 if self.kind==2 else delta-self.config.kp*e)
            if raw!=delta:flags|=8
            for i,side in enumerate(('left','right')):
                ref=s['v']+(delta/2 if i else -delta/2);filtered,r,pwm,saturated,fault=self.controllers[i].step(ref,self.measured[i],.01)
                values[side+'_ref']=ref;values[side+'_filtered']=filtered;values['raw_'+side]=r;values['pwm_'+side]=pwm
                if saturated:flags|=(16 if i else 4)
            w.pwm_left=values['pwm_left'];w.pwm_right=values['pwm_right']
        for i,(K,tau) in enumerate(MotorModel.default('负载').wheels):
            pwm=values['pwm_right' if i else 'pwm_left'];decay=math.exp(-.01/tau);steady=K*pwm
            self.measured[i]=steady+(self.plant[i]-steady)*tau*(1-decay)/.01
            self.plant[i]=steady+(self.plant[i]-steady)*decay
        if self.stage in (1,2):flags|=(2 if self.stage==1 else 1024)
        sample=YawSample(w.tick_ms,10,flags,self.gyro_tick,self.gyro_seq,**values,battery_mv=12000)
        self.latest=sample;self.pre.append(sample)
        if self.stage in (1,2):
            if len(self.samples)>=1040:self.stage=4;w._stop(6)
            else:
                self.samples.append(sample)
                if self.stage==2 and ((w.tick_ms-self.end)&0xffffffff)>=200:self.stage=3
    def report(self,now):
        if self.negotiated and self.latest:
            self.diag_seq+=1;self.sample_reply(self.diag_seq,len(self.samples),self.latest,True)
