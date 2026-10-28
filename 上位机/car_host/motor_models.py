"""Editable, immutable motor model snapshots; custom profiles never overwrite defaults."""
from dataclasses import dataclass, asdict
from copy import deepcopy
from pathlib import Path
import json
import math
import os
import tempfile

DEFAULTS = {
    '空载': (7.646220324939004e-5,.059,7.598223770509158e-5,.059),
    # 用户于2026-10-22指定的负载正向参数，按提供的有效数字保存。
    '负载': (7.20687e-5,.102,7.21277e-5,.098)}
DEFAULT_CONDITION='负载'
SOURCE='data/K_tau_NL_full_20261015/满电参数汇总.json；满电空载12.6V；4000附近增量模型'
LOAD_SOURCE='data/K_tau_LD_2s2s_20261015/parameters.json；负载正向2秒+2秒；2026-10-22用户指定；4000附近增量模型'
OLD_NO_LOAD=(6.601372255427784e-5,.058,6.532700262166621e-5,.057)
@dataclass(frozen=True)
class MotorModel:
    condition:str='空载'
    k_left:float=DEFAULTS['空载'][0]
    tau_left:float=DEFAULTS['空载'][1]
    k_right:float=DEFAULTS['空载'][2]
    tau_right:float=DEFAULTS['空载'][3]
    name:str='默认'
    reference_voltage:float|None=None
    notes:str=''
    source:str=SOURCE

    @classmethod
    def default(cls,condition='空载'):
        return cls(condition,*DEFAULTS[condition],reference_voltage=12.6 if condition=='空载' else None,
                   source=SOURCE if condition=='空载' else LOAD_SOURCE)

    @classmethod
    def historical_no_load(cls):
        return cls('空载',*OLD_NO_LOAD,name='历史非满电空载',
                   source='data/K_tau_summary_20261015/四组参数.json；历史非满电空载；4000附近增量模型')
    @property
    def wheels(self): return ((self.k_left,self.tau_left),(self.k_right,self.tau_right))
    def validate(self):
        if self.condition not in DEFAULTS: raise ValueError('模型工况错误')
        for pair in self.wheels:
            for x in pair:
                if type(x) not in (int,float) or not math.isfinite(x) or x<=0:
                    raise ValueError('K、τ必须为正有限数')
        if self.reference_voltage is not None and (type(self.reference_voltage) not in (int,float)
            or not math.isfinite(self.reference_voltage) or not 0<self.reference_voltage<=100):
            raise ValueError('参考电压必须为0～100V正数或留空')
        for text,limit in ((self.name,80),(self.notes,1000),(self.source,1000)):
            if not isinstance(text,str) or len(text)>limit: raise ValueError('模型文字长度或类型错误')
        if not self.name.strip(): raise ValueError('模型名称不能为空')
        return self
    def snapshot(self): return deepcopy(asdict(self))

class MotorModelStore:
    MAX_BYTES=65536
    def __init__(self,path):
        self.path=Path(path);self.profiles={};self.warning=''
        self.load()
    def load(self):
        self.profiles={};self.warning=''
        if not self.path.exists(): return
        try:
            if self.path.stat().st_size>self.MAX_BYTES: raise ValueError('文件超过64KiB')
            data=json.loads(self.path.read_text(encoding='utf-8'))
            if set(data)!={'schema_version','profiles'} or type(data['schema_version']) is not int or data['schema_version']!=1:
                raise ValueError('模型文件版本错误')
            if not isinstance(data['profiles'],dict) or len(data['profiles'])>64: raise ValueError('模型数量越界')
            for name,value in data['profiles'].items():
                m=MotorModel(**value).validate()
                if name!=m.condition+'/'+m.name or m.name=='默认': raise ValueError('模型名称错误')
                self.profiles[name]=m
        except (ValueError,TypeError,OSError) as exc:
            self.profiles={};self.warning=f'自定义模型读取失败，使用默认模型：{exc}'
    def save(self,model):
        model.validate()
        if model.name=='默认': raise ValueError('自定义模型请使用新名称')
        profiles=dict(self.profiles);profiles[model.condition+'/'+model.name]=model
        if len(profiles)>64: raise ValueError('最多64个自定义模型')
        payload=json.dumps(dict(schema_version=1,profiles={k:v.snapshot() for k,v in profiles.items()}),
                           ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        if len(payload.encode('utf-8'))>self.MAX_BYTES: raise ValueError('模型文件超过64KiB')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix='.motor-model-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(payload);stream.flush();os.fsync(stream.fileno())
            os.replace(tmp,self.path);self.profiles=profiles;self.warning=''
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

