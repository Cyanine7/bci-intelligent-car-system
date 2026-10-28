"""上位机实测速度的五点滑动平均；不修改设备反馈或原始记录。"""
import math
from collections import deque

WINDOW_POINTS = 5


def smooth_speed(samples, side, origin, scale=0.):
    """当前点和前四点等权平均，保留当前点时间戳；缺点后重新积满五点。"""
    points=[]; batch=deque(maxlen=WINDOW_POINTS); previous=None
    for index,sample in enumerate(samples):
        velocity=getattr(sample,'measured_'+side)
        # 不跨越丢样或非法周期拼接窗口；uint32差值兼容设备tick回绕。
        if previous is not None and ((sample.tick-previous.tick)&0xffffffff)!=sample.dt:
            batch.clear()
        previous=sample
        if not 0<sample.dt<=30 or not math.isfinite(velocity):
            batch.clear();continue
        batch.append(sample)
        if len(batch)<WINDOW_POINTS:continue
        span=sum(s.dt for s in batch)/1000
        end=(((sample.tick-origin+0x80000000)&0xffffffff)-0x80000000)/1000
        # 速度来自连续累计编码器位置的差分。相邻位置误差会抵消；
        # 以下权重边界也适用于不等dt，等dt时退化为一计数/五周期。
        rates=[1000/s.dt for s in batch]
        uncertainty=scale/(2*WINDOW_POINTS)*(rates[0]+rates[-1]+
            sum(abs(a-b) for a,b in zip(rates,rates[1:])))
        points.append(dict(index=index,time_s=end,start_s=end-span,
            velocity_mps=sum(getattr(s,'measured_'+side) for s in batch)/WINDOW_POINTS,
            count_uncertainty_mps=uncertainty,window_s=span,
            delay_s=sum(((sample.tick-s.tick)&0xffffffff)/1000 for s in batch)/WINDOW_POINTS))
    return points
