"""Software-only leased device for Qt and protocol checks."""
from dataclasses import replace
import math
from .continuous_models import CAPS, TARGET, STATE, LEASE_MS, OMEGA_LIMIT
from .speed_control_analysis import ReferenceController
from .motor_models import MotorModel


class FakeContinuousDevice:
    def __init__(self, worker):
        self.worker=worker;self.reset_session()

    def reset_session(self):
        self.negotiated=False;self.config=None;self.revision=0;self.accepted=0;self.report_seq=0
        self.plant=[0.,0.];self.measured=[0.,0.];self.gyro_tick=0;self.omega=0.;self.latest=None
        self.controllers=[];self.stop()

    def stop(self):
        self.requested=[0.,0.];self.current=[0.,0.];self.error=self.output=0.
        for c in self.controllers:c.reset()

    def execute(self,kind,body):
        w=self.worker;y=w.yaw
        if kind==28:
            if body:return 1
            if w.armed or y.busy or w.speed.busy:return 4
            if w.faults.get('continuous_unsupported'):return 9
            limit=float(w.faults.get('continuous_omega_limit',OMEGA_LIMIT))
            version=w.faults.get('continuous_version',2)
            self.negotiated=True;y.emit(0x99,w.seq,CAPS.pack(version,7 if version==2 else 3,100,20,LEASE_MS if version==2 else 1000,1.18,limit,.5,2.));return 0
        if not self.negotiated:return 9
        if kind==2:
            if w.armed or y.busy or w.speed.busy:return 4
            if not y.config or y.calibration!=2 or not y.negotiated or w.faults.get('yaw_stale_gyro'):return 7
            if not w.local_enable:return 5
            w._stop(0);self.config=replace(y.config);self.revision=y.revision
            self.controllers=[ReferenceController(2,kp,ki,self.config.pwm_limit) for kp,ki in
                              ((self.config.kp_left,self.config.ki_left),(self.config.kp_right,self.config.ki_right))]
            w.armed=True;w.mode=6;w._idle_deadline=(w.tick_ms+LEASE_MS)&0xffffffff;return 0
        try:version,flags,reserved,revision,deadline,v,omega=TARGET.unpack(body)
        except Exception:return 1
        limit=float(w.faults.get('continuous_omega_limit',OMEGA_LIMIT))
        if version!=2 or flags & ~1 or reserved or not all(math.isfinite(x) for x in (v,omega)) or abs(v)>1.1800001 or abs(omega)>limit+1e-7:return 1
        if not w.local_enable:return 5
        if not w.armed or w.mode!=6:return 4
        if revision!=self.revision:return 7
        if not 0<((deadline-w.tick_ms)&0xffffffff)<=LEASE_MS:return 8
        self.requested=[v,omega];self.accepted=w.seq
        if flags & 1:
            for i,x in enumerate(self.requested):
                if x==0:self.current[i]=0.
            if not v and not omega:self.stop()
        w._deadline=deadline;w._idle_deadline=None;return 0

    def tick(self):
        w=self.worker
        if not self.negotiated:return
        if not w.faults.get('yaw_stale_gyro'):
            self.gyro_tick=w.tick_ms;self.omega=(self.plant[1]-self.plant[0])/.16
        fresh=((w.tick_ms-self.gyro_tick)&0xffffffff)<=60
        active=w.armed and w.mode==6
        if active and not fresh:w._stop(6);active=False
        refs=[0.,0.];flags=2 if fresh else 0
        if active:
            flags|=1
            for i,step in enumerate((.005,.02)):
                d=self.requested[i]-self.current[i]
                self.current[i]+=max(-step,min(step,d))
            v,omega=self.current;e=omega-self.omega
            raw=self.output+self.config.kp*(e-self.error)+self.config.ki*e*.01
            limit=min(self.config.delta_limit,2*(1.18-abs(v)));delta=max(-limit,min(limit,raw))
            if v==0 and omega==0:delta=e=0.
            self.output=delta;self.error=e
            if raw!=delta:flags|=4
            refs=[v-delta/2,v+delta/2]
            pwms=[]
            for i in range(2):
                filtered,raw,pwm,saturated,fault=self.controllers[i].step(refs[i],self.measured[i],.01)
                pwms.append(pwm)
                if saturated:flags|=(8 if i==0 else 16)
            w.pwm_left,w.pwm_right=pwms
        for i,(K,tau) in enumerate(MotorModel.default('负载').wheels):
            pwm=(w.pwm_right if i else w.pwm_left) if active else 0
            steady=K*pwm;decay=math.exp(-.01/tau)
            self.measured[i]=steady+(self.plant[i]-steady)*tau*(1-decay)/.01
            self.plant[i]=steady+(self.plant[i]-steady)*decay
        self.latest=STATE.pack(w.tick_ms,self.accepted,self.revision,w._deadline or 0,
                              *self.requested,*self.current,self.omega,*refs,
                              w.pwm_left if active else 0,w.pwm_right if active else 0,
                              min(65535,(w.tick_ms-self.gyro_tick)&0xffffffff),flags)

    def report(self,now):
        if self.negotiated and self.latest and not self.worker.faults.get('continuous_stale'):
            self.report_seq+=1;self.worker.yaw.emit(0x9a,self.report_seq,self.latest,now)
