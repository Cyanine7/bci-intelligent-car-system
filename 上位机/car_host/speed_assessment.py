"""Compare independent measurements with theoretical design values.

No upper-limit acceptance test. Original samples remain untouched. Both
metrics and plots use the same five-point trailing moving average.
"""
import math
from .speed_smoothing import smooth_speed, WINDOW_POINTS


def design_spec_from_context(context):
    prediction=context.get('prediction',{})
    return prediction.get('design_spec') or prediction.get('acceptance')


def compare_interval(interval,theory):
    if interval is None:return dict(status='无法比较',difference_interval=None)
    low,high=interval
    difference=[None if low is None else low-theory,None if high is None else high-theory]
    if low is None or high is None:status='区间不完整，无法确定与理论的差异'
    elif low<=theory<=high:status='测量区间包含理论值'
    elif high<theory:status='实测低于理论值'
    else:status='实测高于理论值'
    return dict(status=status,difference_interval=difference)


def assess_capture(result,samples,caps,meta,settings,*,history=None):
    report=dict(status='未记录理论设计值',wheels={})
    if not settings:return report
    mp=settings.get('mp_ratio');ts=settings.get('design_ts_s')
    if (type(mp) not in (int,float) or type(ts) not in (int,float) or
        not math.isfinite(mp) or not math.isfinite(ts) or not 0<mp<1 or ts<=0):
        report['status']='理论设计值无效';return report
    report.update(status='理论与实测对比',theory=dict(mp_percent=mp*100,ts_s=ts,band_ratio=.02),
                  method='五点滑动平均：超调、调节时间与图中实测平滑速度一致',
                  scope='窗口平均速度会平滑瞬时峰值；区间包含理论值不表示已证明二者相等')
    scale=caps['diameter_um']/1e6*math.pi/caps['counts_per_turn']
    source=history if history is not None else samples
    ticks={s.tick for s in samples}
    for side in ('left','right'):
        row=result['wheels'].get(side,{})
        item=dict(status='无法比较',reason=row.get('status','无有效响应'),
                  overshoot_percent_interval=None,settling_time_s_interval=None)
        report['wheels'][side]=item
        if not samples or 'r1' not in row or not row['r1']:
            if meta['segments'][0][side]==meta['segments'][1][side]:
                item.update(status='不参与',reason='该轮目标未发生阶跃')
            continue
        initial=row['v0'];target=row['r1'];delta=target-initial
        delta_low=max(0.,abs(delta)-scale/.3);delta_high=abs(delta)+scale/.3
        band=.02*delta_low;wide_band=.02*delta_high
        if not band:
            item['reason']='阶跃幅值不超过起点平台计数量化';continue
        # 切段不清空平滑窗，与连续绘图一致；仅用第二段活动样本的输出作指标。
        peak_points=[p for p in smooth_speed(source,side,meta['switch_tick'],scale)
                     if p['time_s']>=0 and source[p['index']].tick in ticks]
        settle_points=peak_points
        peak_window=max((p['window_s'] for p in peak_points),default=0.)
        settling_window=peak_window
        item.update(smoothing_points=WINDOW_POINTS,overshoot_window_s=peak_window,window_s=settling_window,
                    estimates=peak_points,settling_estimates=settle_points)
        direction=1 if delta>0 else -1
        if peak_points:
            low=max(0.,max(direction*(p['velocity_mps']-target)-p['count_uncertainty_mps'] for p in peak_points)/delta_high*100)
            high=max(0.,max(direction*(p['velocity_mps']-target)+p['count_uncertainty_mps'] for p in peak_points)/delta_low*100)
            central=max(0.,max(direction*(p['velocity_mps']-target) for p in peak_points)/abs(delta)*100)
            item.update(overshoot_percent=central,overshoot_percent_interval=[low,high],
                        overshoot_difference_pp=central-mp*100,
                        overshoot_comparison=compare_interval([low,high],mp*100))
        if settle_points:
            definite_out=[i for i,p in enumerate(settle_points) if abs(p['velocity_mps']-target)-p['count_uncertainty_mps']>wide_band]
            not_inside=[i for i,p in enumerate(settle_points) if abs(p['velocity_mps']-target)+p['count_uncertainty_mps']>band]
            last_out=max(definite_out,default=-1);last_unknown=max(not_inside,default=-1)
            lower=max(0.,settle_points[last_out]['start_s']) if last_out>=0 else 0.
            next_index=last_unknown+1;upper=None
            if next_index<len(settle_points) and settle_points[-1]['time_s']-settle_points[next_index]['time_s']>=max(.1,settling_window)-1e-9:
                upper=settle_points[next_index]['time_s']
            item['settling_time_s_interval']=[lower,upper]
        item['settling_comparison']=compare_interval(item['settling_time_s_interval'],ts)
        item['raw_overshoot_comparison']=compare_interval(row.get('raw_overshoot_percent_interval'),mp*100)
        item['raw_overshoot_difference_pp']=None if 'overshoot_percent' not in row else row['overshoot_percent']-mp*100
        item['status']='已比较' if peak_points else '无法比较'
        item['reason']='；'.join((item.get('overshoot_comparison',{}).get('status','超调观察窗不足'),
                                 item['settling_comparison']['status']))
    return report
