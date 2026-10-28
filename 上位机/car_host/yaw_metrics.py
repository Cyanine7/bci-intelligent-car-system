"""角速度实验指标与中文报告；图与指标共用同一因果五点平均。"""
import math
import numpy as np
from .speed_design import response_metrics


def relative_seconds(tick,anchor):
    return (((tick-anchor+0x80000000)&0xffffffff)-0x80000000)/1000


def valid_gyro(sample):
    return bool(sample.flags&32 and sample.flags&64 and not sample.flags&384
                and (sample.tick-sample.gyro_tick)&0xffffffff<=60)


def smooth_samples(ordered,anchor):
    window=[];points=[];previous=None
    for sample in ordered:
        if previous is not None and (sample.tick-previous)&0xffffffff!=sample.dt:window=[]
        if not valid_gyro(sample):window=[]
        else:
            window.append(sample.omega);window=window[-5:]
            if len(window)==5:points.append((sample,relative_seconds(sample.tick,anchor),sum(window)/5))
        previous=sample.tick
    return points


def _metrics(times,values,initial,target,has_step,can_settle):
    row=dict(overshoot_percent=None,settling_time_s=None,settling_crossing_s=None,
             rise_time_s=None,peak_rad_s=None,peak_time_s=None)
    if not len(values):row['reason']='没有有效角速度数据';return row
    change=target-initial
    if not has_step or abs(change)<1e-12:
        row['reason']='角速度目标没有阶跃变化，超调、tr和ts不适用';return row
    values=np.asarray(values,dtype=float);times=np.asarray(times,dtype=float)
    row.update(response_metrics(times,values,initial,target))
    normalized=(values-initial)/change
    crossings=[next((float(t) for t,y in zip(times,normalized) if y>=level),None) for level in (.1,.9)]
    if all(t is not None for t in crossings):row['rise_time_s']=crossings[1]-crossings[0]
    peak=int(np.argmax(normalized));row.update(peak_rad_s=float(values[peak]),peak_time_s=float(times[peak]))
    if not can_settle:
        row.update(settling_time_s=None,settling_crossing_s=None,rise_time_s=None,
                   reason='记录不完整或含失效/缺失样本；超调仅为已观测值，时间指标无法判定')
    elif row['settling_time_s'] is None:row['reason']='观察窗内未持续进入并保持在目标±2%阶跃幅值带至少0.1s'
    return row


def analyze_capture(info,meta,samples,complete,context=None,config=None):
    if not info or not meta or not samples:return {}
    ordered=[s for _,s in sorted(samples.items())]
    smooth=smooth_samples(ordered,meta['start']);stages=[]
    theory=(context or {}).get('context',{}).get('design',{})
    for index,segment in enumerate(meta['segments']):
        target=segment['target'] if meta['kind']==1 else segment['target']/.16
        start=meta['switch'] if index else meta['start']
        active=[s for s in ordered if s.flags&2 and bool(s.flags&512)==bool(index)]
        row=dict(index=index,executed=bool(active),target_rad_s=target,initial_rad_s=None,
                 steady_rad_s=None,steady_error_rad_s=None,relative_error_percent=None,
                 raw={},five_point={},reasons=[])
        stages.append(row)
        if not active:
            row['reasons'].append('本段未执行或没有本段记录');continue
        usable=[s for s in active if valid_gyro(s)]
        if not usable:row['reasons'].append('没有有效且已校准的IMU角速度样本');continue
        before=[s for s in ordered if valid_gyro(s) and 0<((start-s.tick)&0xffffffff)<=300]
        initial=float(np.mean([s.omega for s in before])) if before else None
        row['initial_rad_s']=initial
        if initial is None:row['reasons'].append('缺少阶跃前实测基线，无法计算阶跃动态指标')
        continuous=all((b.tick-a.tick)&0xffffffff==b.dt for a,b in zip(usable,usable[1:]))
        can_settle=bool(complete and continuous and len(usable)==len(active))
        times=[relative_seconds(s.tick,start) for s in usable];values=[s.omega for s in usable]
        tail=[s for s in usable if (usable[-1].tick-s.tick)&0xffffffff<300]
        tail_contiguous=all((b.tick-a.tick)&0xffffffff==b.dt for a,b in zip(tail,tail[1:]))
        tail_span=((tail[-1].tick-tail[0].tick)&0xffffffff)+tail[0].dt
        if tail_contiguous and tail_span>=300:
            steady=float(np.average([s.omega for s in tail],weights=[s.dt for s in tail]))
            row.update(steady_rad_s=steady,steady_error_rad_s=target-steady,
                       relative_error_percent=(target-steady)/abs(target)*100 if abs(target)>1e-12 else None,
                       steady_window_s=.3,steady_min_rad_s=min(s.omega for s in tail),steady_max_rad_s=max(s.omega for s in tail))
        else:row['reasons'].append('末300ms有效连续数据不足，静差无法判定')
        previous_target=meta['segments'][index-1]['target'] if index else 0.
        if meta['kind']==2:previous_target/=.16
        has_step=abs(target-previous_target)>1e-12
        baseline=initial if initial is not None else target
        row['raw']=_metrics(times,values,baseline,target,has_step and initial is not None,can_settle)
        points=[(s,relative_seconds(s.tick,start),value) for s,_,value in smooth
                if s.flags&2 and bool(s.flags&512)==bool(index)]
        row['five_point']=_metrics([p[1] for p in points],[p[2] for p in points],baseline,target,
                                  has_step and initial is not None,can_settle)
        row['five_point'].update(points=5,timestamp='五点窗口末点，时间不前移')
        row.update(pwm_peak_left=max(abs(s.pwm_left) for s in active),pwm_peak_right=max(abs(s.pwm_right) for s in active),
                   delta_saturation_s=sum(s.dt for s in active if s.flags&8)/1000,
                   left_saturation_s=sum(s.dt for s in active if s.flags&4)/1000,
                   right_saturation_s=sum(s.dt for s in active if s.flags&16)/1000)
        if not can_settle:row['reasons'].append('本段含缺失/失效样本或记录未经完整确认')
        for metrics in (row['raw'],row['five_point']):
            ts=metrics.get('settling_time_s')
            if ts is not None and theory.get('design_ts_s') is not None:metrics['ts_difference_s']=ts-theory['design_ts_s']
    primary=next((row for row in reversed(stages) if row['executed']),stages[0])
    result=dict(run=info['run'],revision=info['revision'],complete=bool(complete),kind=meta['kind'],
                stop_reason=info['reason'],primary_stage=primary['index'],stages=stages,theory=theory,config=config,
                scope='校正IMU角速度，按设备控制tick计时；静差=目标−末300ms平均值')
    # 保留上一版导出顶层字段，中文主显示采用five_point，raw独立保留。
    result.update(primary.get('raw',{}))
    for key in ('target_rad_s','initial_rad_s','steady_rad_s','steady_error_rad_s','relative_error_percent','five_point'):
        result[key]=primary.get(key)
    return result


def _value(value,unit='',signed=False):
    if value is None or not math.isfinite(value):return '无法判定'
    return (format(value,'+.6g' if signed else '.6g'))+unit


def describe_analysis(result):
    if not result:return '等待本次实验记录；实验结束后自动读取并计算指标。'
    primary=result['stages'][result['primary_stage']];main=primary.get('five_point',{})
    lines=[f'本次角速度实验 run={result["run"]} · PI revision={result["revision"]} · '+('记录完整' if result['complete'] else '记录不完整'),
           f'主要指标（第{primary["index"]+1}段，五点平滑）：',
           '超调 Mp='+_value(main.get('overshoot_percent'),'%')+'；静差 ess='+_value(primary.get('steady_error_rad_s'),' rad/s',True),
           '调节时间 ts（2%）='+_value(main.get('settling_time_s'),' s')+'；上升时间 tr（10%～90%）='+_value(main.get('rise_time_s'),' s'),
           '目标='+_value(primary.get('target_rad_s'),' rad/s')+'；末300ms平均='+_value(primary.get('steady_rad_s'),' rad/s')+
           '；相对静差='+_value(primary.get('relative_error_percent'),'%',True)]
    reason=main.get('reason')
    if reason:lines.append('指标说明：'+reason)
    if not result['complete']:lines.append('记录不完整：超调仅为已观测值，不能确认tr或ts。')
    if result['stop_reason']!=2:lines.append('本次提前停止，按实际执行段分析；未执行段不计算结果。')
    cfg=result.get('config')
    if cfg:lines.append(f'本次实际外环 Kp={cfg["kp"]:.9g}，连续Ki={cfg["ki"]:.9g}')
    if result.get('theory'):
        t=result['theory'];lines.append('冻结设计：ζ='+_value(t.get('zeta'))+'，目标ts='+_value(t.get('design_ts_s'),' s'))
    lines.append('超调、tr和ts以实测前平台为起点、原始目标为终点；五点平均保留末点时间，原始数据不变。')
    for row in result['stages']:
        lines.append('')
        lines.append(f'第{row["index"]+1}段 · 目标 '+_value(row['target_rad_s'],' rad/s'))
        if not row['executed']:lines.append('未执行或没有本段记录');continue
        lines.append('静差（目标−末300ms平均）='+_value(row['steady_error_rad_s'],' rad/s',True))
        for key,label in (('five_point','五点平滑'),('raw','原始IMU（已扣零偏）')):
            m=row[key]
            lines.append(label+'：超调='+_value(m.get('overshoot_percent'),'%')+'，ts='+_value(m.get('settling_time_s'),' s')+
                         '，tr='+_value(m.get('rise_time_s'),' s')+'，峰值='+_value(m.get('peak_rad_s'),' rad/s'))
            if m.get('reason'):lines.append('  '+m['reason'])
        if row.get('five_point',{}).get('ts_difference_s') is not None:
            lines.append('五点ts相对设计差='+_value(row['five_point']['ts_difference_s'],' s',True))
        if row.get('steady_rad_s') is not None:
            lines.append('末300ms角速度范围：'+_value(row['steady_min_rad_s'])+'～'+_value(row['steady_max_rad_s'],' rad/s'))
        if 'pwm_peak_left' in row:
            lines.append(f'PWM峰值 左/右={row["pwm_peak_left"]}/{row["pwm_peak_right"]}；差速限幅累计={row["delta_saturation_s"]:.4g}s')
        lines.extend(row['reasons'])
    return '\n'.join(lines)
