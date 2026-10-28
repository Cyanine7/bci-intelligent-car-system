"""Predict the full sampled PI loop, including its zero and zero-order hold.

Prediction utilities only. Formula-based gain calculation lives in
speed_control_analysis; predictions never optimize or replace those gains.
"""
import math
import numpy as np


def response_metrics(times, values, initial=0., target=1.):
    """Target-relative overshoot and last exit from the 2% step-size band."""
    change=target-initial
    if abs(change)<1e-15:return dict(overshoot_percent=None,settling_time_s=None)
    normalized=(np.asarray(values)-initial)/change
    error=np.abs(normalized-1.)
    outside=np.flatnonzero(error>.02)
    last=int(outside[-1]) if len(outside) else -1
    settle=None
    crossing=None
    if last+1<len(times) and times[-1]-times[last+1]>=.1:
        settle=float(times[last+1]) if last>=0 else 0.
        if last>=0:
            fraction=(error[last]-.02)/(error[last]-error[last+1])
            crossing=float(times[last]+fraction*(times[last+1]-times[last]))
        else:crossing=0.
    return dict(overshoot_percent=float(max(0.,np.max(normalized)-1.)*100),
                settling_time_s=settle,settling_crossing_s=crossing)


def sampled_response(K,tau,kp,ki,dt,duration,*,target_filter=False):
    """Exact sampled PI with the preceding interval's average velocity.

    x[n+1]=a*x[n]+b*u[n], y[n+1]=c*x[n]+d*u[n]. This is the
    encoder-count-window measurement used by the MCU, before count quantization.
    The complete characteristic polynomial is cubic; retain all three poles.
    """
    a=math.exp(-dt/tau);b=K*(1-a)
    c=tau*(1-a)/dt;d=K*(1-c);f=K*(c-a);gain=kp+ki*dt
    roots=np.roots([1.,-(a+1)+d*gain,a+f*gain-d*kp,-f*kp]).astype(complex)
    if max(abs(roots))>=1:return None
    n=np.arange(int(math.ceil(duration/dt))+1)
    # 前置滤波与离散PI参考零点相消；闭环反馈极点不变，阶跃初值改变。
    u0=ki*dt if target_filter else gain
    e1=1-d*u0
    u1=2*ki*dt-gain*d*u0 if target_filter else ki*dt+gain*e1
    e2=1-c*b*u0-d*u1
    matrix=np.array([np.ones(3),roots,roots**2])
    if np.linalg.cond(matrix)>1e10:
        # Repeated/near-repeated poles: use the defining physical recurrence.
        x=measured=previous=u=previous_measured=0.;error=np.empty(len(n))
        for i in n:
            error[i]=1-measured
            u+=(kp*(error[i]-previous) if not target_filter else -kp*(measured-previous_measured))+ki*dt*error[i]
            previous=error[i];previous_measured=measured
            measured=c*x+d*u;x=a*x+b*u
    else:
        coefficients=np.linalg.solve(matrix,np.array([1.,e1,e2]))
        error=np.sum(coefficients[:,None]*roots[:,None]**n[None,:],axis=0)
    return n*dt,1-np.real(error),roots

