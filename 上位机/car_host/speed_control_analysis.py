"""Course design, incremental reference prediction and count-window identification."""
import math
import numpy as np
from .motor_models import MotorModel,DEFAULTS,SOURCE
from .speed_control_models import SpeedConfig,ACTIVE,PRE,SECOND,FAULT,BAD_DT,SPEED_LIMIT_MM_S
from .speed_design import response_metrics
from .speed_smoothing import smooth_speed, WINDOW_POINTS
MODELS={k:((v[0],v[1]),(v[2],v[3])) for k,v in DEFAULTS.items()}
MODEL_SOURCE=SOURCE
DEFAULT_MP=.05

def pole_match_pi(mp_ratio,ts_s,K,tau):
    """Course second-order pole placement from theoretical Mp and 2% ts."""
    if any(type(x) not in (int,float) or not math.isfinite(x) for x in (mp_ratio,ts_s,K,tau)):
        raise ValueError('设计输入必须为有限数')
    if not 0<mp_ratio<1 or min(ts_s,K,tau)<=0:
        raise ValueError('要求0<Mp<1，ts、K、τ为正数')
    log=math.log(mp_ratio);zeta=-log/math.sqrt(math.pi**2+log**2)
    wn=4/(zeta*ts_s);kp=(2*zeta*wn*tau-1)/K;ki=tau*wn**2/K
    if not all(math.isfinite(x) for x in (zeta,wn,kp,ki)) or kp<0:
        raise ValueError('设计不可实现：Kp为负或结果越界')
    return dict(kp=kp,ki=ki,zeta=zeta,wn=wn)


def conclude_pi(mp_ratio,ts_s,K,tau):
    return pole_match_pi(mp_ratio,ts_s,K,tau)

def design(K,tau,*,algorithm=2,zeta=None,mp_ratio=DEFAULT_MP,settling_s=.2,target=.1,dt=.01):
    if zeta is not None:
        if not 0<zeta<1:raise ValueError('Mp设计采用0<ζ<1')
        mp_ratio=math.exp(-math.pi*zeta/math.sqrt(1-zeta*zeta))
    if algorithm!=2:raise ValueError('仅支持固定增量PI（algorithm=2）')
    row=conclude_pi(mp_ratio,settling_s,K,tau)
    row['design_method']='二阶极点配置公式 + Tf=Kp/Ki目标前置滤波；不优化或替换增益'
    row['design_dt_s']=dt
    kp,ki=row['kp'],row['ki'];tf=kp/ki if ki else 0.
    poles=np.roots([tau,1+K*kp,K*ki]) if ki else [-((1+K*kp)/tau)]
    return row|dict(K=K,tau=tau,mp_ratio=mp_ratio,ki_step=ki*dt,tf=tf,
        continuous_start=0.,discrete_candidate=ki*dt*target,target_filter_tau_s=tf,
        filtered_first=(kp+ki*dt)*target*dt/(tf+dt) if tf else kp*target,
        poles=[[float(np.real(p)),float(np.imag(p))] for p in poles],zero=-ki/kp if ki and kp else None)

def design_pair(condition='空载',algorithm=2,zeta=None,settling_s=.2,target=.1,pwm_limit=16500,
                *,model=None,mp_ratio=DEFAULT_MP):
    model=(model or MotorModel.default(condition)).validate()
    rows=[design(K,tau,algorithm=algorithm,zeta=zeta,settling_s=settling_s,target=target,mp_ratio=mp_ratio)
          for K,tau in model.wheels]
    cfg=SpeedConfig(algorithm,pwm_limit,*[v for r in rows for v in (r['kp'],r['ki'])]).validate()
    # Prediction describes the formula's result; it must never change the gains.
    for i,(K,tau) in enumerate(model.wheels):
        sim=predict(K,tau,cfg,i,target=target,duration=max(2.,8*settling_s,20*tau))
        rows[i]['predicted_integer_metrics']=dict(overshoot_percent=sim['discrete_overshoot_percent'],
                settling_time_s=sim['discrete_settling_time_s'],saturation_s=sim['discrete_saturation_s'],target_mps=target)
    return cfg,rows

class ReferenceController:
    """Software prediction only; does not implement the user's MCU functions."""
    def __init__(self,algorithm,kp,ki,limit,*,target_filter=True):
        if algorithm!=2:raise ValueError('仅支持固定增量PI')
        self.algorithm,self.kp,self.ki,self.limit=algorithm,kp,ki,limit
        self.target_filter=target_filter
        self.previous_error=self.previous_output=self.filtered=0.
    def reset(self):self.previous_error=self.previous_output=self.filtered=0.
    def step(self,target,measured,dt):
        if not target:
            self.reset();return 0.,0.,0,False,False
        # 与MCU采用相同的后向欧拉前置滤波；原始目标为零仍立即停车复位。
        if self.target_filter:
            ki_dt=self.ki*dt
            self.filtered+=ki_dt/(self.kp+ki_dt)*(target-self.filtered)
        else:self.filtered=target  # 仅用于重算旧固件留下的未滤波历史记录。
        error=self.filtered-measured
        delta=self.kp*(error-self.previous_error)+self.ki*dt*error
        raw=self.previous_output+delta
        clipped=max(-self.limit,min(self.limit,raw))
        self.previous_output=clipped;self.previous_error=error
        return self.filtered,raw,int(clipped),raw!=clipped,False

def predict(K,tau,config,side=0,target=.1,duration=1.,segments=None,pwm=False,target_filter=True):
    if not all(math.isfinite(x) and x>0 for x in (K,tau,duration)):raise ValueError('无效模型')
    kp,ki=config.gains[side]
    if segments:
        first=segments[0]['duration_ms']/1000
        duration=sum(s['duration_ms'] for s in segments)/1000
        key='left' if side==0 else 'right'
        def ref(t):return segments[0 if t<first-1e-9 else 1][key]/(1 if pwm else 1000)
    else:
        first=0.
        def ref(t):return target
    # Continuous model via RK4, no clipping; discrete trace includes clipping.
    def deriv(state,r):
        v,integral,filtered=state
        # 连续理想前置滤波与PI的参考零点相消后，等价于I-P结构：
        # u=z-Kp*v，dz/dt=Ki*(r-v)。这样不用数值积分可能很快的滤波极点。
        u=r if pwm else (-kp*v+integral if target_filter else kp*(r-v)+integral)
        return ((-v+K*u)/tau,ki*(r-v) if not pwm else 0.,
                0.)
    state=(0.,0.,0.);continuous=[];h=.001
    def add(a,b,f):return tuple(x+f*y for x,y in zip(a,b))
    for i in range(round(duration/h)+1):
        t=i*h;r=ref(t);v,integ,f=state
        u=r if pwm else (-kp*v+integ if target_filter else kp*(r-v)+integ)
        continuous.append((t,v,u))
        # Zero targets are explicit stops in the MCU; match this in prediction.
        if not r and not pwm:
            state=(v,0.,0.);continuous[-1]=(t,v,0.)
            state=(v*math.exp(-h/tau),0.,0.);continue
        a=deriv(state,r);b=deriv(add(state,a,h/2),r);c=deriv(add(state,b,h/2),r);d=deriv(add(state,c,h),r)
        state=tuple(x+h*(aa+2*bb+2*cc+dd)/6 for x,aa,bb,cc,dd in zip(state,a,b,c,d))
    controller=ReferenceController(config.algorithm,kp,ki,config.pwm_limit,target_filter=target_filter);measured=0.;plant_speed=0.;discrete=[]
    for i in range(round(duration/.01)+1):
        t=i*.01;r=ref(t)
        if pwm:f,raw,out,sat,held=0.,r,int(r),False,False
        else:f,raw,out,sat,held=controller.step(r,measured,.01)
        discrete.append((t,measured,f,raw,out,sat,held))
        decay=math.exp(-.01/tau);average=tau*(1-decay)/.01
        measured=average*plant_speed+K*out*(1-average)
        plant_speed=plant_speed*decay+K*out*(1-decay)
    final_r=ref(duration);final=K*final_r if pwm else final_r
    baseline=next((v for t,v,u in continuous if t>=first),0.)
    change=final-baseline
    after=[(t,v) for t,v,u in continuous if t>=first]
    overshoot=max(0.,max(((v-final)*(1 if change>0 else -1)) for t,v in after)/abs(change)*100) if change else None
    discrete_base=next((v for t,v,f,u,out,sat,held in discrete if t>=first),0.)
    discrete_delta=final-discrete_base
    discrete_after=[v for t,v,f,u,out,sat,held in discrete if t>=first]
    discrete_overshoot=(max(0.,max((v-final)*(1 if discrete_delta>0 else -1) for v in discrete_after)/abs(discrete_delta)*100)
                         if discrete_delta else None)
    cm=response_metrics([t-first for t,v in after],[v for t,v in after],baseline,final)
    dm=response_metrics([t-first for t,v,f,u,out,sat,held in discrete if t>=first],discrete_after,discrete_base,final)
    return dict(continuous=continuous,discrete=discrete,ideal_overshoot_percent=overshoot,
                continuous_settling_time_s=cm['settling_time_s'],discrete_settling_time_s=dm['settling_time_s'],
                discrete_overshoot_percent=discrete_overshoot,
                discrete_saturation_s=sum(.01 for t,v,f,u,out,sat,held in discrete if sat),
                target_filter=bool(target_filter and not pwm),target_filter_tau_s=kp/ki if not pwm and target_filter else None,
                model_scope='4000附近增量模型线性外推；离散采用周期平均反馈与整数PWM，编码器量化、死区和负载变化未建模')

def step_range(config,side=0,u0=0.,error_previous=0.,target0=0.,dt=.01,dead_pwm=None,target_filter=True):
    # 首平台已经稳定时，滤波后的目标阶跃首周期只贡献Ki*dt*Δr。
    kp,ki=config.gains[side];A=ki*dt if target_filter else kp+ki*dt
    if not A:raise ValueError('增益无效')
    bias=u0+ki*dt*error_previous
    low=(-config.pwm_limit-bias)/A;high=(config.pwm_limit-bias)/A
    cap=SPEED_LIMIT_MM_S/1000
    recommended=[max(-cap-target0,.95*low),min(cap-target0,.95*high)]
    return dict(theoretical_delta=[low,high],recommended_delta=recommended,
                deadzone_delta=None if dead_pwm is None else (dead_pwm-bias)/A,
                basis='目标前置滤波首周期估计（首平台/滤波已稳定）' if target_filter else '历史未滤波首周期估计',
                bias_source='用户指定或模型估计，实验后按实际第一平台复核')

def rolling_deadzone(points):
    points=[(float(u),float(v)) for u,v in points if u!=0 and u*v>0]
    if len(set(u for u,v in points))<3:return None
    direction=1 if points[0][0]>0 else -1
    if any(u*direction<=0 for u,v in points):raise ValueError('死区拟合不得混合方向')
    x=np.array([abs(u) for u,v in points]);y=np.array([abs(v) for u,v in points])
    slope,intercept=np.polyfit(x,y,1)
    if slope<=0:return None
    return dict(pwm_estimate=float(-intercept/slope),slope=float(slope),intercept=float(intercept),
                status='同工况滚动平台外推估计，非启动阈值')

def _platform(samples,side,scale):
    if not samples:return dict(valid=False,reason='该段无样本')
    end=samples[-1].tick
    tail=[s for s in samples if ((end-s.tick)&0xffffffff)+s.dt<=300]
    if sum(s.dt for s in tail)<290:return dict(valid=False,reason='末300ms观察不足')
    def mean(xs):return sum(getattr(s,'encoder_'+side) for s in xs)*scale/(sum(s.dt for s in xs)/1000)
    halfway=len(tail)//2
    a,b=mean(tail[:halfway]),mean(tail[halfway:]);v=mean(tail)
    tolerance=max(abs(v)*.05,scale/.15)
    times=np.array([((s.tick-tail[0].tick)&0xffffffff)/1000 for s in tail])
    trend=float(np.polyfit(times,[getattr(s,'measured_'+side) for s in tail],1)[0]*(times[-1]-times[0]))
    valid=abs(a-b)<=tolerance and abs(trend)<=max(tolerance,scale/.01)
    return dict(valid=bool(valid),reason='' if valid else '平台均值或趋势不稳定',speed=v,
                pwm=float(np.mean([getattr(s,'pwm_'+side) for s in tail])),count=len(tail),trend=trend)

def _fit_tau(samples,side,v0,vt,switch,scale):
    # Aggregate full-count intervals in <=50ms windows; integrate across t=0.
    windows=[];batch=[]
    for s in samples:
        batch.append(s)
        if sum(x.dt for x in batch)>=50:
            b=((batch[-1].tick-switch)&0xffffffff)/1000
            if b>1e6:b-=4294967.296
            a=b-sum(x.dt for x in batch)/1000
            windows.append((a,b,sum(getattr(x,'encoder_'+side) for x in batch)*scale));batch=[]
    if len(windows)<4 or abs(vt-v0)<scale/.05:return dict(tau=None,tau_reason='响应或时间分辨率不足')
    a=np.array([x[0] for x in windows]);b=np.array([x[1] for x in windows]);obs=np.array([x[2] for x in windows])
    taus=np.linspace(.005,min(2.,max(.05,float(b[-1]))),1600)
    aa=np.maximum(0,a)[None,:];bb=np.maximum(0,b)[None,:];tt=taus[:,None]
    expected=v0*(np.minimum(b,0)-np.minimum(a,0))[None,:]+vt*(bb-aa)+(v0-vt)*tt*(np.exp(-aa/tt)-np.exp(-bb/tt))
    costs=np.mean((expected-obs[None,:])**2,axis=1);best=int(np.argmin(costs))
    if best in (0,len(taus)-1):return dict(tau=None,tau_reason='拟合达到搜索边界')
    threshold=v0+.6321205588*(vt-v0);direction=1 if vt>v0 else -1
    t63=next(((a+b)/2 for a,b,c in windows if direction*(c/(b-a)-threshold)>=0),None)
    return dict(tau=float(taus[best]),tau_fit_integral_rmse=float(math.sqrt(costs[best])),
                tau_63_window_s=t63,tau_readout_resolution_s=.05)

def analyze(info,samples,caps,meta=None,acceptance=None):
    result=dict(valid=False,reasons=[],wheels={},stop_reason=info['stop_reason'],
                run=info['run'],revision=info.get('revision'),executed_config=info.get('config'))
    ordered=[samples[i] for i in range(info['count']) if i in samples]
    if info['status']!=3 or len(ordered)!=info['count']:result['reasons'].append('记录不完整或溢出')
    if not meta or meta['run']!=info['run'] or meta['segment']!=1:result['reasons'].append('无有效第二段切换')
    if any(s.flags&(FAULT|BAD_DT) for s in ordered):result['reasons'].append('控制故障或采样异常')
    if any(((b.tick-a.tick)&0xffffffff)!=b.dt or not 0<((b.tick-a.tick)&0xffffffff)<=30 for a,b in zip(ordered,ordered[1:])):
        result['reasons'].append('样本时间不连续')
    if result['reasons']:return result
    scale=caps['diameter_um']/1e6*math.pi/caps['counts_per_turn']
    first=[s for s in ordered if s.flags&ACTIVE and not s.flags&SECOND]
    second=[s for s in ordered if s.flags&ACTIVE and s.flags&SECOND]
    if not first or not second:result['reasons'].append('两段实际输出证据不足');return result
    kind=info['kind'];result['valid']=True
    nominal=sum(s['duration_ms'] for s in meta['segments'])
    elapsed=(info['end_tick']-info['start_tick'])&0xffffffff
    result['deadline_clipped']=elapsed<nominal
    result['actual_total_ms']=elapsed
    for side in ('left','right'):
        p0=_platform(first,side,scale);p1=_platform(second,side,scale)
        m=dict(platform0=p0,platform1=p1,pwm_peak=max(abs(getattr(s,'pwm_'+side)) for s in first+second),
            saturation_s=sum(s.dt/1000 for s in first+second if s.flags&(8 if side=='left' else 16)))
        result['wheels'][side]=m
        if not p0['valid'] or not p1['valid']:
            m['status']='平台不足，指标无法判定';continue
        v0,vt=p0['speed'],p1['speed'];delta=vt-v0
        m.update(v0=v0,v_infinity=vt,u0=p0['pwm'],u_infinity=p1['pwm'])
        if abs(delta)<scale/.3:
            m['status']='速度变化小于平台计数量化';continue
        if kind==2:
            du=p1['pwm']-p0['pwm']
            if not du:m['status']='输入未变化';continue
            m['K']=delta/du;m.update(_fit_tau(second,side,v0,vt,meta['switch_tick'],scale))
            m['status']='开环辨识完成' if m.get('tau') else m.get('tau_reason')
            continue
        r0=meta['segments'][0][side]/1000;r1=meta['segments'][1][side]/1000
        step=r1-v0
        if abs(step)<scale/.3:
            m['status']='目标变化小于平台计数量化';continue
        direction=1 if step>0 else -1
        times=[((s.tick-meta['switch_tick'])&0xffffffff)/1000 for s in second]
        speeds=[getattr(s,'measured_'+side) for s in second]
        m.update(r0=r0,r1=r1,static_error=r1-vt,relative_error_percent=(r1-vt)/abs(r1)*100 if r1 else None,
            overshoot_percent=max(0,max(direction*(v-r1) for v in speeds)/abs(step)*100),
            platform_overshoot_percent=max(0,max((1 if delta>0 else -1)*(v-vt) for v in speeds)/abs(delta)*100),
            settling_time_s=None,rise_time_s=None,status='停止响应' if not r1 else '闭环分析完成')
        peak=max(second,key=lambda s:direction*getattr(s,'measured_'+side))
        peak_speed=getattr(peak,'measured_'+side);quantum=scale/(peak.dt/1000)
        delta_low=max(abs(step)-scale/.3,1e-15);delta_high=abs(step)+scale/.3
        m.update(raw_peak_speed_mps=peak_speed,raw_peak_encoder_count=getattr(peak,'encoder_'+side),
                 raw_peak_dt_ms=peak.dt,raw_peak_time_s=((peak.tick-meta['switch_tick'])&0xffffffff)/1000,
                 raw_overshoot_resolution_pp=quantum/abs(step)*100,
                 raw_overshoot_percent_interval=[max(0.,max(direction*(getattr(s,'measured_'+side)-r1)-scale/(s.dt/1000) for s in second)/delta_high*100),
                                                  max(0.,max(direction*(getattr(s,'measured_'+side)-r1)+scale/(s.dt/1000) for s in second)/delta_low*100)])
        # 图、超调和时间指标共用同一五点平均；保留原始指标供核对。
        points=[p for p in smooth_speed(ordered,side,meta['switch_tick'],scale)
                if ordered[p['index']].flags&ACTIVE and ordered[p['index']].flags&SECOND]
        if points:
            smooth_times=[p['time_s'] for p in points];smooth_values=[p['velocity_mps'] for p in points]
            smooth=response_metrics(smooth_times,smooth_values,v0,r1)
            crossings=[next((p['time_s'] for p in points if direction*(p['velocity_mps']-v0)>=f*abs(step)),None)
                       for f in (.1,.9)]
            smooth.update(points=WINDOW_POINTS,rise_time_s=crossings[1]-crossings[0] if all(t is not None for t in crossings) else None,
                          peak_speed_mps=max(smooth_values,key=lambda v:direction*v),
                          delay_s=points[0]['delay_s'])
            m['smoothed']=smooth
        if not r1:continue
        crossings=[next((t for t,v in zip(times,speeds) if direction*(v-v0)>=f*abs(step)),None) for f in (.1,.9)]
        if all(t is not None for t in crossings):m['rise_time_s']=crossings[1]-crossings[0]
        finals={'settling_time_s':r1}
        for key,final in finals.items():
            band=.02*abs(final-v0);quantum=scale/(max(s.dt for s in second)/1000)
            if band<quantum:m[key+'_reason']='2%误差带小于单周期速度量化';continue
            last=max((i for i,v in enumerate(speeds) if abs(v-final)>band),default=-1)
            if last+1<len(times) and times[-1]-times[last+1]>=.1:m[key]=times[last+1]
            else:m[key+'_reason']='观察窗内未持续进入2%误差带'
    if info['kind']==1:
        from .speed_assessment import assess_capture
        result['comparison']=assess_capture(result,second,caps,meta,acceptance,history=ordered)
    return result

def describe_analysis(result):
    lines=[f'实测记录 run={result.get("run")} · PI revision={result.get("revision")}']
    if result.get('comparison'):
        a=result['comparison'];lines.append(a['status'])
        if a.get('theory'):
            lines.append(f'冻结理论设计值：Mp={a["theory"]["mp_percent"]:.6g}%，ts={a["theory"]["ts_s"]:.6g}s（2%带）')
            lines.append('实测采用五点滑动平均，与曲线一致；10ms采样时约滞后20ms，时间戳不前移。')
        for side,label in (('left','左轮'),('right','右轮')):
            verdict=a.get('wheels',{}).get(side)
            if verdict:
                lines.append(label+'：'+verdict['reason'])
                if verdict.get('overshoot_percent') is not None:
                    lo,hi=verdict['overshoot_percent_interval']
                    lines.append(f'  五点平滑超调={verdict["overshoot_percent"]:.4g}%（计数量化区间{lo:.4g}～{hi:.4g}%），相对理论差={verdict["overshoot_difference_pp"]:+.4g}个百分点')
                interval=verdict.get('settling_time_s_interval')
                if interval:lines.append(f'  ts区间={interval[0]:.4g}～'+('未确定上界' if interval[1] is None else f'{interval[1]:.4g}s'))
    if result.get('reasons'):
        lines.append('分析条件不足：'+'；'.join(result['reasons']))
    for side,label in (('left','左轮'),('right','右轮')):
        row=result.get('wheels',{}).get(side)
        if not row:continue
        lines.append(label+'：'+row.get('status','等待结果'))
        if 'v0' in row:
            lines.append(f'  v0={row["v0"]:.6g} m/s，v∞={row["v_infinity"]:.6g} m/s')
        if 'K' in row:
            tau=row.get('tau')
            lines.append(f'  K={row["K"]:.9g} (m/s)/PWM，τ='+ (f'{tau:.6g} s' if tau else '无法判定'))
        if 'static_error' in row:
            smooth=row.get('smoothed',{})
            if smooth:
                rise=smooth.get('rise_time_s');settle=smooth.get('settling_time_s')
                lines.append(f'  五点平滑：超调={smooth["overshoot_percent"]:.4g}%，上升时间='+
                             (f'{rise:.4g}s' if rise is not None else '无法判定')+'，ts估计='+
                             (f'{settle:.4g}s' if settle is not None else '观察窗内未持续进入2%带'))
                lines.append('  平滑会降低峰值；ts估计按平滑曲线计算，计数量化影响另见区间。')
            lines.append(f'  静差={row["static_error"]:.6g} m/s，10ms原始速度超调={row["overshoot_percent"]:.4g}%')
            rise=row.get('rise_time_s');settle=row.get('settling_time_s')
            lines.append('  原始上升时间='+ (f'{rise:.4g} s' if rise is not None else '无法判定')+
                         '，原始调节时间='+ (f'{settle:.4g} s' if settle is not None else '无法判定'))
            if row.get('settling_time_s_reason'):lines.append('  '+row['settling_time_s_reason'])
            lines.append(f'  原始峰值={row["raw_peak_speed_mps"]:.8g}m/s，{row["raw_peak_encoder_count"]}计数/{row["raw_peak_dt_ms"]}ms；一计数≈{row["raw_overshoot_resolution_pp"]:.4g}个超调百分点')
            lo,hi=row['raw_overshoot_percent_interval'];lines.append(f'  原始超调量化区间={lo:.4g}～{hi:.4g}%')
        lines.append(f'  PWM峰值={row["pwm_peak"]}，限幅时间={row["saturation_s"]:.4g} s')
    return '\n'.join(lines)

