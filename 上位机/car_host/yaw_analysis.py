"""课程平均建模和63.2%一阶等效；预测不改写计算增益。"""
from dataclasses import replace
import cmath
import math
import numpy as np
from .motor_models import MotorModel
from .speed_control_analysis import pole_match_pi, ReferenceController
from .speed_design import response_metrics
from .yaw_models import YawConfig, TRACK, WHEEL_SPEED_LIMIT, finite, validate_segments

def step_value(t,a,b):
    disc=cmath.sqrt(a*a-4*b);p=(-a+disc)/2;q=(-a-disc)/2
    if abs(p-q)<1e-9:return 1-(1+a*t/2)*math.exp(-a*t/2)
    return float((1+(q*cmath.exp(p*t)-p*cmath.exp(q*t))/(p-q)).real)

def second_order_transition(a,b,dt):
    """常值参考下精确推进二阶状态，手动快极点也不受RK4步长稳定域限制。"""
    disc=cmath.sqrt(a*a-4*b);p=(-a+disc)/2;q=(-a-disc)/2
    if abs(p-q)<1e-9:
        r=-a/2;decay=math.exp(r*dt)
        return np.array([[1-r*dt,dt],[-b*dt,1+r*dt]])*decay
    ep=cmath.exp(p*dt);eq=cmath.exp(q*dt);e01=(ep-eq)/(p-q)
    return np.array([[(p*eq-q*ep)/(p-q),e01],[-b*e01,(p*ep-q*eq)/(p-q)]]).real

def equivalent_model(model,inner,tv_override=None):
    """Tv均保持单位直流增益；手动Tv不改写左右轮物理模型和内环参数。"""
    model.validate()
    inner.validate();K=(model.k_left+model.k_right)/2;tau=(model.tau_left+model.tau_right)/2
    kp=(inner.kp_left+inner.kp_right)/2;ki=(inner.ki_left+inner.ki_right)/2
    a=(1+K*kp)/tau;b=K*ki/tau
    if not all(math.isfinite(x) and x>0 for x in (a,b)) or not math.isfinite(a*a+4*b):
        raise ValueError('模型数值超出计算范围')
    derived=[1.,a,b];order=2;source='轮速模型与内环PI理论等效'
    if tv_override is not None:
        order=tv_override.get('order')
        if order==1:
            tv=finite(tv_override['tau_v_s'])
            if not .001<=tv<=10:raise ValueError('手动τv须在0.001～10s之间')
            source='手动一阶Tv'
        elif order==2:
            a=finite(tv_override['a']);b=finite(tv_override['b'])
            if not .01<=a<=10000 or not .01<=b<=1000000:raise ValueError('手动Tv要求0.01≤a≤10000、0.01≤b≤1000000')
            source='手动二阶Tv'
        else:raise ValueError('Tv需要一阶或二阶模型')
    # 先寻找首次跨越63.2%的区间，再二分，不用开环τ替代闭环τ。
    goal=1-math.exp(-1);lo=0.;hi=0.
    horizon=max(1.,20*a/b,20/math.sqrt(b))
    if not math.isfinite(horizon):raise ValueError('闭环时间尺度超出计算范围')
    for t in ([] if order==1 else np.linspace(0,horizon,10001)[1:]):
        if step_value(float(t),a,b)>=goal:hi=float(t);break
        lo=float(t)
    if order==2 and not hi:raise ValueError('当前模型未找到63.2%交点')
    for _ in range(0 if order==1 else 60):
        mid=(lo+hi)/2
        if step_value(mid,a,b)<goal:lo=mid
        else:hi=mid
    if order==2:tv=(lo+hi)/2
    times=np.linspace(0,max(1,10*tv),2001)
    deviation=max(abs(step_value(float(t),a,b)-(1-math.exp(-t/tv))) for t in times) if order==2 else 0.
    return dict(model=model.snapshot(),average=dict(K=K,tau_m=tau,kp=kp,ki=ki),
        tv_numerator=b if order==2 else 1.,tv_denominator=[1.,a,b] if order==2 else [tv,1.],
        tv_order=order,tv_source=source,derived_tv_denominator=derived,
        track_m=TRACK,tau_v_s=tv,equivalence='理论阶跃首次达到1-exp(-1)' if order==2 else '手动一阶时间常数',
        equivalence_max_step_error=deviation,inner_prefilter=True,
        scope='负载正向局部模型；原地转向涉及反向轮速，单独比较实测')

def design(model=None,inner=None,zeta=1.5,settling=.7,*,tv_override=None,gain_scale=1.):
    model=(model or MotorModel.default('负载')).validate()
    if finite(zeta)<=1 or finite(settling)<=0:raise ValueError('课程过阻尼设计要求ζ>1、ts>0')
    if not 0<finite(gain_scale)<=1:raise ValueError('时延修正增益比例须大于0且不超过1')
    if inner is None:
        rows=[pole_match_pi(.05,.2,K,tau) for K,tau in model.wheels]
        inner=YawConfig(kp_left=rows[0]['kp'],ki_left=rows[0]['ki'],kp_right=rows[1]['kp'],ki_right=rows[1]['ki'])
    context=equivalent_model(model,inner,tv_override);tv=context['tau_v_s']
    # 等价写法避免大ζ时两相近数相减。
    wn=4*(zeta+math.sqrt(zeta*zeta-1))/settling
    op=TRACK*(2*zeta*wn*tv-1);oi=TRACK*tv*wn*wn
    cfg=replace(inner,kp=op*gain_scale,ki=oi*gain_scale).validate()
    context.update(zeta=zeta,design_ts_s=settling,wn=wn,kp=cfg.kp,ki=cfg.ki,
                   course_kp=op,course_ki=oi,gain_scale=gain_scale)
    return cfg,context

def predict(config,model,segments,kind=1,dt=.001,*,tv_override=None,feedback_delay_s=0.,gyro_hz=55.):
    """一阶、完整平均、双轮独立连续模型，以及实际10ms算法的离散复核。"""
    validate_segments(segments,kind);config.validate();model.validate()
    if not 0<=finite(feedback_delay_s)<=.2:raise ValueError('预测附加反馈时延须在0～200ms之间')
    if not 20<=finite(gyro_hz)<=1000:raise ValueError('陀螺采样率须在20～1000Hz之间')
    context=equivalent_model(model,config,tv_override)
    a,b=(context['tv_denominator'][1:] if context['tv_order']==2 else context['derived_tv_denominator'][1:]);tv=context['tau_v_s']
    duration=sum(s['duration_ms'] for s in segments)/1000
    traces={}
    for name in ('一阶等效','当前Tv','双轮独立'):
        state=np.zeros(4);previous_error=output=0.;values=[];times=[]
        coefficients=[((1+K*kp)/tau,K*ki/tau) for (K,tau),(kp,ki) in zip(model.wheels,
                    ((config.kp_left,config.ki_left),(config.kp_right,config.ki_right)))]
        if name!='双轮独立':coefficients=[(a,b),(a,b)]
        transitions=[second_order_transition(aa,bb,dt) for aa,bb in coefficients]
        for j in range(int(round(duration/dt))+1):
            t=j*dt;s=segments[0 if t<segments[0]['duration_ms']/1000 else 1]
            omega=(state[2]-state[0])/TRACK;e=s['target']-omega
            raw=s['target'] if kind==2 else output+config.kp*(e-previous_error)+config.ki*e*dt
            limit=min(config.delta_limit,2*(WHEEL_SPEED_LIMIT-abs(s['v'])))
            delta=max(-limit,min(limit,raw));output=delta;previous_error=e
            if s['v']==0 and s['target']==0:delta=output=previous_error=0.
            refs=(s['v']-delta/2,s['v']+delta/2)
            values.append(omega);times.append(t)
            if name=='一阶等效' or (name=='当前Tv' and context['tv_order']==1):
                for i in (0,2):state[i]+=(1-math.exp(-dt/tv))*(refs[i//2]-state[i])
            else:
                for i in (0,2):
                    shifted=transitions[i//2]@np.array([state[i]-refs[i//2],state[i+1]])
                    state[i]=refs[i//2]+shifted[0];state[i+1]=shifted[1]
        traces[name]=dict(times=times,omega=values)
    # 1ms推进电机，准确表达实际陀螺采样；额外时延独立于采样保持。
    for name,delay in (('10ms离散双轮',0.),('含时延离散双轮',feedback_delay_s)):
        traces[name]=sampled_prediction(config,model,segments,kind,delay,gyro_hz)
    switch=segments[0]['duration_ms']/1000
    for value in traces.values():
        selected=[i for i,t in enumerate(value['times']) if t>=switch]
        initial=value['omega'][max(0,selected[0]-1)]
        target=segments[1]['target'] if kind==1 else segments[1]['target']/TRACK
        value['metrics']=response_metrics(np.array(value['times'])[selected]-switch,np.array(value['omega'])[selected],initial,target)
    return traces


def sampled_prediction(config,model,segments,kind,delay,gyro_hz=55.):
    controllers=[ReferenceController(2,kp,ki,config.pwm_limit) for kp,ki in
                 ((config.kp_left,config.ki_left),(config.kp_right,config.ki_right))]
    plant=np.zeros(2);distance=np.zeros(2);last_distance=np.zeros(2);pwm=np.zeros(2)
    history=[];gyro=previous_error=output=0.;times=[];values=[];true_values=[];next_gyro=0.
    delay_steps=round(delay*1000)
    for n in range(sum(s['duration_ms'] for s in segments)+1):
        omega=(plant[1]-plant[0])/TRACK;history.append(float(omega))
        if n+1e-9>=next_gyro:
            gyro=history[max(0,n-delay_steps)];next_gyro+=1000/gyro_hz
        if n%10==0:
            s=segments[0 if n<segments[0]['duration_ms'] else 1]
            measured=(distance-last_distance)/.01;last_distance=distance.copy()
            e=s['target']-gyro
            raw=s['target'] if kind==2 else output+config.kp*(e-previous_error)+config.ki*e*.01
            limit=min(config.delta_limit,2*(WHEEL_SPEED_LIMIT-abs(s['v'])));delta=max(-limit,min(limit,raw))
            output=delta;previous_error=e
            if s['v']==0 and s['target']==0:delta=output=previous_error=0.
            refs=(s['v']-delta/2,s['v']+delta/2)
            for i in range(2):pwm[i]=controllers[i].step(refs[i],float(measured[i]),.01)[2]
            times.append(n*.001);values.append(float(gyro));true_values.append(float(omega))
        for i,(K,tau) in enumerate(model.wheels):
            decay=math.exp(-.001/tau);steady=K*pwm[i]
            distance[i]+=steady*.001+(plant[i]-steady)*tau*(1-decay)
            plant[i]=steady+(plant[i]-steady)*decay
    return dict(times=times,omega=values,true_omega=true_values,feedback_delay_s=delay,
                gyro_hz=gyro_hz,scope='正向局部电机模型，含采样保持及附加时延；未含摩擦、滑动和编码器量化')


def stability_margin(config,context,delay_s=0.):
    """当前Tv的连续开环裕度；与实际双轮离散预测分别报告。"""
    w=np.logspace(-4,5,12000);s=1j*w
    denominator=np.polyval(context['tv_denominator'],s)
    loop=(config.kp+config.ki/s)*context['tv_numerator']/denominator/TRACK
    magnitude=abs(loop);phase=np.unwrap(np.angle(loop))
    crossings=np.flatnonzero((magnitude[:-1]>=1)&(magnitude[1:]<1))
    if not len(crossings):return dict(reason='当前频率范围内未找到增益交越点')
    i=int(crossings[0]);ratio=-math.log(magnitude[i])/math.log(magnitude[i+1]/magnitude[i])
    wc=math.exp(math.log(w[i])+ratio*math.log(w[i+1]/w[i]));ph=phase[i]+ratio*(phase[i+1]-phase[i])
    pm=math.pi+ph
    return dict(crossover_hz=wc/(2*math.pi),phase_margin_deg=math.degrees(pm),
                with_delay_phase_margin_deg=math.degrees(pm-wc*delay_s),
                extra_delay_margin_s=max(0.,pm/wc),feedback_delay_s=delay_s,
                scope='当前Tv连续模型的纯时延裕度；未含数字采样/执行时延')
