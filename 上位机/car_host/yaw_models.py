"""角速度实验独立线格式；所有速度用SI单位，Ki为连续增益。"""
from dataclasses import dataclass, asdict
import math
import struct

COMMANDS=dict(yaw_caps=19,yaw_read=20,yaw_apply=21,yaw_calibrate=22,yaw_status=23,
              yaw_run=24,yaw_info=25,yaw_data=26,yaw_meta=27,yaw_arm=2)
CONFIG=struct.Struct('<BbH7d')
STEP=struct.Struct('<BBH4fHHI')
SAMPLE=struct.Struct('<IHHII15fhhHH')
CAPS=struct.Struct('<BB7Hdd')
INFO=struct.Struct('<IIIIHBBBBHf')
REPLIES={k:struct.Struct(f'<{n}s') for k,n in
         ((0x8f,32),(0x90,64),(0x91,32),(0x92,28),(0x93,48),(0x94,56),
          (0x95,44),(0x96,64),(0x97,56),(0x98,44))}
MAX_SEGMENT_MS=5000
MAX_TOTAL_MS=10000
CAPACITY=1040
TRACK=.16
# 与固件PROJECT_SPEED_LIMIT一致：PWM 16500按负载模型折合约1.19m/s，取1.18。
WHEEL_SPEED_LIMIT=1.18

def finite(value):
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
        raise ValueError('参数必须是有限数')
    return value

@dataclass(frozen=True)
class YawConfig:
    version:int=1
    sign:int=1
    pwm_limit:int=16500
    kp:float=.269598536858538
    ki:float=2.1422925162150044
    delta_limit:float=.16
    kp_left:float=42736.99955736679
    ki_left:float=1188723.7747308263
    kp_right:float=40483.753121200316
    ki_right:float=1141172.9197219384

    def validate(self):
        from .speed_control_models import SpeedConfig
        if self.version!=1 or type(self.sign) is not int or self.sign not in (-1,1):raise ValueError('版本或IMU方向错误')
        if not 0<=finite(self.kp)<=100 or not 0<finite(self.ki)<=1000 or not 0<finite(self.delta_limit)<=.6:
            raise ValueError('角速度PI或差速上限越界')
        SpeedConfig(2,self.pwm_limit,self.kp_left,self.ki_left,self.kp_right,self.ki_right).validate()
        return self
    def pack(self):return CONFIG.pack(*asdict(self.validate()).values())
    @classmethod
    def unpack(cls,data):return cls(*CONFIG.unpack(data)).validate()

def validate_segments(segments,kind=1,*,max_segment=5000,max_total=10000,speed_limit=WHEEL_SPEED_LIMIT,omega_limit=1.):
    if type(kind) is not int or kind not in (1,2) or not isinstance(segments,(list,tuple)) or len(segments)!=2:
        raise ValueError('角速度实验需要两段和有效实验类型')
    result=[]
    for s in segments:
        if set(s)!={'v','target','duration_ms'}:raise ValueError('每段需要v、target和duration_ms')
        v,target=finite(s['v']),finite(s['target']);duration=s['duration_ms']
        if type(duration) is not int or duration%10 or not 10<=duration<=max_segment:raise ValueError('每段10～5000ms，须为10ms整数倍')
        if abs(v)>speed_limit+1e-7 or abs(target)>(omega_limit if kind==1 else .6)+1e-7:raise ValueError('实验目标超过能力')
        v=max(-speed_limit,min(speed_limit,v))
        result.append(dict(v=float(v),target=float(target),duration_ms=duration))
    if sum(x['duration_ms'] for x in result)>max_total:raise ValueError('两段总时长超过实际能力')
    if all(result[0][key]==result[1][key] for key in ('v','target')):raise ValueError('两段目标需要变化')
    return result

def encode(command,values):
    if command=='yaw_arm':return b'\x05'
    if command=='yaw_apply':return YawConfig(**values).pack()
    if command=='yaw_data':return struct.pack('<IHH',values['run'],values['start'],values['count'])
    if command=='yaw_run':
        kind=values.get('kind',1);s=validate_segments(values['segments'],kind)
        return STEP.pack(1,kind,0,s[0]['v'],s[0]['target'],s[1]['v'],s[1]['target'],
                         s[0]['duration_ms'],sum(x['duration_ms'] for x in s),values['deadline_tick'])
    return b''

@dataclass(frozen=True)
class YawSample:
    tick:int;dt:int;flags:int;gyro_tick:int;gyro_seq:int
    v:float;target:float;raw_gyro:float;omega:float;integral:float;raw_delta:float;delta:float
    left_ref:float;right_ref:float;left_filtered:float;right_filtered:float
    left:float;right:float;raw_left:float;raw_right:float
    pwm_left:int;pwm_right:int;battery_mv:int;reserved:int=0
    def pack(self):return SAMPLE.pack(*asdict(self).values())
    @classmethod
    def unpack(cls,data):
        s=cls(*SAMPLE.unpack(data))
        if s.reserved or s.flags&~2047 or not 0<s.dt<=65535 or any(not math.isfinite(x) for x in SAMPLE.unpack(data)[5:20]):
            raise ValueError('角速度样本字段无效')
        return s
