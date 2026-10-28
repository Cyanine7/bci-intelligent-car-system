"""Speed experiment extension v2. Explicit little endian, binary64 gains."""
from dataclasses import dataclass, asdict
import math
import struct

SPEED_COMMANDS = dict(speed_caps=11, speed_read=12, speed_apply=13, speed_run=14,
    speed_info=15, speed_data=16, speed_pwm=17, speed_meta=18, speed_arm=2, speed_arm_pwm=2)
SPEED_CAPS, SPEED_CONFIG, SPEED_DIAG, SPEED_INFO, SPEED_DATA, SPEED_META = range(0x89, 0x8f)
CAPS_STRUCT = struct.Struct('<BBHHHHHIIddHH')
CONFIG_STRUCT = struct.Struct('<BBHdddd')
STEP_STRUCT = struct.Struct('<BBHhhhhHHI')
SAMPLE_STRUCT = struct.Struct('<IHHhhffffffhhHHii')
REPLY_LAYOUTS = {
    SPEED_CAPS: CAPS_STRUCT, SPEED_CONFIG: struct.Struct('<I'+CONFIG_STRUCT.format[1:]),
    SPEED_DIAG: struct.Struct('<I'+SAMPLE_STRUCT.format[1:]),
    SPEED_INFO: struct.Struct('<IIIIHBBBBH'+CONFIG_STRUCT.format[1:]),
    SPEED_DATA: struct.Struct('<IHH'+SAMPLE_STRUCT.format[1:]),
    SPEED_META: struct.Struct('<IIIIBBH'+STEP_STRUCT.format[1:])}
PRE, ACTIVE, POST, SAT_L, SAT_R, FAULT, BAD_DT, SECOND = 1,2,4,8,16,128,256,512
# 与固件PROJECT_SPEED_LIMIT一致：PWM 16500按负载模型折合约1.19m/s，取1.18。
SPEED_LIMIT_MM_S=1180

def integer(v, low, high):
    if type(v) is not int or not low <= v <= high:
        raise ValueError(f'整数必须为 {low}..{high}')
    return v

def finite(v, low=0., high=float('inf')):
    if type(v) not in (float,int) or not math.isfinite(v) or not low <= v <= high:
        raise ValueError('数值必须有限且在允许范围内')
    return float(v)

@dataclass(frozen=True)
class SpeedConfig:
    algorithm: int = 2
    pwm_limit: int = 16500
    kp_left: float = 0.
    ki_left: float = 0.
    kp_right: float = 0.
    ki_right: float = 0.

    @property
    def gains(self): return ((self.kp_left,self.ki_left),(self.kp_right,self.ki_right))

    def validate(self):
        integer(self.algorithm,2,2);integer(self.pwm_limit,1,16500)
        for kp,ki in self.gains:
            finite(kp,0,1e6);finite(ki,0,4e7)
            if ki<=0:
                raise ValueError('固定增量PI要求Kp≥0、Ki>0')
        return self

    def pack(self):
        self.validate()
        return CONFIG_STRUCT.pack(self.algorithm,0,self.pwm_limit,*[v for pair in self.gains for v in pair])

    @classmethod
    def unpack(cls, fields, *, allow_empty=False):
        mode,reserved,limit,*gains=fields
        if reserved: raise ValueError('配置保留字段非零')
        if allow_empty and not any(fields): return None
        return cls(mode,limit,*gains).validate()

    def as_dict(self): return asdict(self)

def validate_step(values, *, pwm=False, maximum=3000):
    if set(values) not in ({'segments'},{'segments','deadline_tick'}):
        raise ValueError('两段命令字段错误')
    segments=values['segments']
    if not isinstance(segments,(list,tuple)) or len(segments)!=2:
        raise ValueError('需要恰好两段')
    result=[]
    for s in segments:
        if not isinstance(s,dict) or set(s)!={'left','right','duration_ms'}:
            raise ValueError('每段需要left、right、duration_ms')
        limit=16500 if pwm else SPEED_LIMIT_MM_S
        left=integer(s['left'],-limit,limit);right=integer(s['right'],-limit,limit)
        duration=integer(s['duration_ms'],10,maximum)
        if duration%10: raise ValueError('时长必须为10ms整数倍')
        result.append(dict(left=left,right=right,duration_ms=duration))
    if sum(s['duration_ms'] for s in result)>maximum: raise ValueError('总时长超过实际能力')
    if any(result[0][side]*result[1][side]<0 for side in ('left','right')):
        raise ValueError('同轮两段非零目标不得跨方向')
    if all(result[0][side]==result[1][side] for side in ('left','right')):
        raise ValueError('至少一个轮子的两段输入需要不同')
    return result

def step_fields(values,pwm=False):
    a,b=validate_step(values,pwm=pwm)
    deadline=integer(values['deadline_tick'],0,0xffffffff)
    return (2,2,0,a['left'],a['right'],b['left'],b['right'],
            a['duration_ms'],a['duration_ms']+b['duration_ms'],deadline)

def decode_step(fields,pwm=False):
    version,count,reserved,l0,r0,l1,r1,first,total,deadline=fields
    if (version,count,reserved)!=(2,2,0): raise ValueError('两段协议版本错误')
    segments=validate_step(dict(segments=[dict(left=l0,right=r0,duration_ms=first),
        dict(left=l1,right=r1,duration_ms=total-first)]),pwm=pwm)
    return segments,deadline

@dataclass(frozen=True)
class SpeedSample:
    tick:int
    dt:int
    flags:int
    target_left:int
    target_right:int
    filtered_left:float
    filtered_right:float
    measured_left:float
    measured_right:float
    raw_left:float
    raw_right:float
    pwm_left:int
    pwm_right:int
    battery_mv:int
    reserved:int
    encoder_left:int
    encoder_right:int

    @classmethod
    def unpack(cls,fields):
        s=cls(*fields)
        if (s.reserved or s.flags & ~927 or not 1<=s.dt<=65535
            or any(not math.isfinite(x) for x in fields[5:11])
            or max(abs(s.pwm_left),abs(s.pwm_right),abs(s.target_left),abs(s.target_right))>16500):
            raise ValueError('速度采样字段无效')
        return s
    def pack(self): return SAMPLE_STRUCT.pack(*asdict(self).values())

def encode_speed_command(command,values):
    if command in ('speed_caps','speed_read','speed_info','speed_meta'):
        if values: raise ValueError('查询不含参数')
        return b''
    if command in ('speed_arm','speed_arm_pwm'): return bytes([3 if command=='speed_arm' else 4])
    if command=='speed_apply': return SpeedConfig(**values).pack()
    if command in ('speed_run','speed_pwm'):
        return STEP_STRUCT.pack(*step_fields(values,pwm=command=='speed_pwm'))
    if command=='speed_data':
        run,start,count=(values[k] for k in ('run','start','count'))
        integer(run,1,0xffffffff);integer(start,0,383);integer(count,1,8)
        if start+count>384: raise ValueError('读取范围超过容量')
        return struct.pack('<IHH',run,start,count)
    raise ValueError('未知速度实验命令')

