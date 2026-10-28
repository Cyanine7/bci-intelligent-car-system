"""Finite v2 experiment service. ACK + reply, explicit arming, immutable contexts."""
from collections import deque
from copy import deepcopy
import time
from .device import DeviceResult
from .speed_control_models import *

class SpeedControlService:
    def __init__(self,host):
        self.host=host;self.reset()

    def reset(self):
        self.caps=self.config=self.info=self.meta=None
        self.revision=None;self.samples={};self.live=deque(maxlen=384)
        self.status='尚未查询速度实验能力'
        self.download_running=False;self._download_request=None;self._last_diag=None
        self.context={};self.config_context={};self.result_context={}
        self._contexts={};self._apply_context={};self.saved_directory=None
        self._meta_needed=False;self.integrity_errors=[]
        self.record_generation=0;self.active_run=None

    @property
    def complete(self):
        return bool(self.info and self.meta and self.info['status']==3 and
                    self.info['count']>0 and len(self.samples)==self.info['count'] and not self.integrity_errors)
    def query_capabilities(self):return self.host.send_command('speed_caps')
    def read_config(self):return self.host.send_command('speed_read')
    def apply_config(self,config):
        self._apply_context=deepcopy(self.context)
        return self.host.send_command('speed_apply',config.as_dict())
    def arm(self,pwm=False):return self.host.send_command('speed_arm_pwm' if pwm else 'speed_arm')
    def run_step(self,segments,pwm=False,expected_config=None):
        if not pwm and expected_config is not None and (self.config is None or self.config.pack()!=expected_config.pack()):
            return DeviceResult(False,'rejected','PI草稿与已确认RAM不一致，请先应用并回读')
        result=self.host.send_command('speed_pwm' if pwm else 'speed_run',dict(segments=segments))
        if result.success:
            seq=self.host.command_metadata[result.request_id]['seq']
            self._contexts[seq]=dict(prediction=deepcopy(self.context),tuning=deepcopy(self.config_context),
                connection_id=self.host.connection_id,session=self.host.adapter.session,
                config_revision=self.revision,executed_config=self.config.as_dict() if self.config and not pwm else None,
                target_filter=bool(self.caps and self.caps.get('target_filter')))
            while len(self._contexts)>8:self._contexts.pop(next(iter(self._contexts)))
            self.live.clear()
        return result
    def read_log_info(self):return self.host.send_command('speed_info')
    def read_log_meta(self):return self.host.send_command('speed_meta')
    def read_log_data(self,run,start,count):
        return self.host.send_command('speed_data',dict(run=run,start=start,count=count))

    def download(self):
        if not self.info or not self.meta or self.info['status'] not in (3,4):
            return DeviceResult(False,'rejected','请先取得停车后的记录概要及阶段元数据')
        self.download_running=True;self.samples={};self.integrity_errors=[]
        return self._next_batch()

    def _next_batch(self):
        start=len(self.samples)
        if start==self.info['count']:
            self.download_running=False
            self.status='100Hz记录完整' if self.complete else '记录不完整'
            from .speed_control_recording import export_capture
            try:
                self.saved_directory=export_capture(self,self.host.data_directory)
                self.status+=f'；已保存 {self.saved_directory.name}'
            except (OSError,ValueError) as exc:self.status+=f'；保存失败：{exc}'
            return DeviceResult(True,'complete',self.status)
        result=self.read_log_data(self.info['run'],start,min(8,self.info['count']-start))
        self._download_request=result.request_id
        if not result.success:self.download_running=False
        return result

    def prepare(self,command,values):
        h=self.host
        if h.io_backlog_exceeded or not h.project_state_fresh:
            raise ValueError('需要通信健康且500ms内、覆盖前次指令的STATE')
        if h.project_request_pending:raise ValueError('有在途请求，请等待确认')
        if command!='speed_caps' and self.caps is None:raise ValueError('先显式查询并确认版本2实验能力')
        running=command in ('speed_run','speed_pwm')
        if not running and h.latest.armed:raise ValueError('此操作需要先STOP并确认失能')
        if command in ('speed_arm','speed_arm_pwm','speed_apply','speed_caps') and self.download_running:
            raise ValueError('记录正在读取')
        if command=='speed_caps':
            self.caps=None;h.adapter.speed_negotiated=False
        if command in ('speed_arm','speed_apply'):
            if not self.caps['algorithm_ready']:raise ValueError('当前设备固件未启用速度PI；闭环不可使能')
            if not self.caps.get('target_filter'):raise ValueError('当前固件没有目标前置滤波，请先更新固件')
        if command=='speed_apply':
            cfg=SpeedConfig(**values).validate()
            if cfg.pwm_limit>self.caps['pwm_limit'] or any(kp>self.caps['kp_max'] or ki>self.caps['ki_max'] for kp,ki in cfg.gains):
                raise ValueError('配置超过实际CAPS限制')
        if command in ('speed_arm','speed_arm_pwm'):
            if not h.latest.local_enable:raise ValueError('设备本地禁止')
            if command=='speed_arm' and self.config is None:raise ValueError('需要实际PI配置回读')
            if self.info and self.info['count'] and not self.complete:
                raise ValueError('请先完整读取上一实验记录')
        if running:
            mode=4 if command=='speed_pwm' else 3
            if (not h.latest.armed or h.latest.control_mode!=mode or h.project_motion_active):
                raise ValueError('请先显式使能对应新实验模式并等待STATE')
            if mode==3 and (not self.caps['algorithm_ready'] or self.config is None):
                raise ValueError('需要算法就绪和实际参数')
            if mode==3 and not self.caps.get('target_filter'):raise ValueError('当前固件没有目标前置滤波')
            segments=validate_step(values,pwm=mode==4,maximum=self.caps['max_duration_ms'])
            limit=self.caps['pwm_limit'] if mode==4 else self.caps['speed_limit']
            if any(abs(s[k])>limit for s in segments for k in ('left','right')):
                raise ValueError('目标超过实际CAPS')
            elapsed=int(max(0,time.monotonic()-h.latest.received_monotonic)*self.caps['control_hz'])
            values['deadline_tick']=(h.latest.device_time_ms+int(elapsed*1000/self.caps['control_hz'])+
                                     sum(s['duration_ms'] for s in segments))&0xffffffff
        if command=='speed_data':
            if not self.info or values.get('run')!=self.info['run']:raise ValueError('记录身份不匹配')
            if values.get('start',-1)+values.get('count',0)>self.info['count']:raise ValueError('记录索引越界')
        return values

    def diagnostic(self,event):
        if not self.caps:return
        run,*fields=event.values;s=SpeedSample.unpack(fields)
        if self.active_run is not None and run!=self.active_run:return
        if run>self.host.adapter.seq:raise ValueError('诊断实验序号错误')
        if self._last_diag and (not 0<((event.seq-self._last_diag[0])&0xffffffff)<0x80000000 or
                               not 0<((s.tick-self._last_diag[1])&0xffffffff)<0x80000000):return
        self._last_diag=(event.seq,s.tick);self.live.append((run,s))

    def handle(self,event,pending):
        h=self.host;command=pending['command']
        if event.kind==0x82:
            from .project_protocol import COMMAND_TYPES,ACK_MESSAGES
            kind,status=event.values
            if kind!=COMMAND_TYPES[command] or status>=len(ACK_MESSAGES):return
            if status:
                self.status=ACK_MESSAGES[status]
                h._remember_result(DeviceResult(False,'rejected',self.status,pending['request_id']))
                h._project_pending.pop(event.seq,None);self.download_running=False
                if h._motion_latched_seq==event.seq:h._motion_latched_seq=0
                return
            pending['ack']=True
        elif event.kind==SPEED_CAPS and command=='speed_caps':
            v,flags,hz,pwm,speed,duration,capacity,counts,diameter,kp,ki,pre,post=event.values
            if (v!=2 or flags&~3 or flags&2 and not flags&1 or hz!=100 or not 1<=pwm<=16500 or not 1<=speed<=SPEED_LIMIT_MM_S
                or not 20<=duration<=3000 or capacity!=384 or not 1<=counts<=10000000
                or not 1000<=diameter<=1000000 or kp!=1e6 or ki!=4e7 or pre!=100 or post!=200):
                raise ValueError('实验CAPS不满足版本2契约')
            pending['value']=dict(version=2,algorithm_ready=bool(flags&1),target_filter=bool(flags&2),control_hz=hz,pwm_limit=pwm,
                speed_limit=speed,max_duration_ms=duration,capacity=capacity,counts_per_turn=counts,
                diameter_um=diameter,kp_max=kp,ki_max=ki,pre_ms=pre,post_ms=post);pending['reply']=True
        elif event.kind==SPEED_CONFIG and command in ('speed_read','speed_apply'):
            rev,*fields=event.values;cfg=SpeedConfig.unpack(fields,allow_empty=True)
            if self.revision is not None and ((rev-self.revision)&0xffffffff)>=0x80000000:
                raise ValueError('配置revision倒退')
            if command=='speed_apply' and (cfg is None or cfg.pack()!=SpeedConfig(**h.command_metadata[pending['request_id']]['values']).pack()):
                raise ValueError('binary64回读与提交值不一致')
            pending['value']=(rev,cfg);pending['reply']=True
        elif event.kind==SPEED_INFO and command=='speed_info':
            run,rev,start,end,count,state,reason,kind,segment,reserved,*fields=event.values
            if (reserved or count>384 or state>4 or reason>6 or kind not in (0,1,2)
                or segment not in (0,1,255) or run>h.adapter.seq or (not run and (count or state or kind))):
                raise ValueError('实验概要无效')
            cfg=SpeedConfig.unpack(fields,allow_empty=True)
            if run and kind==1 and cfg is None:raise ValueError('闭环记录缺少配置快照')
            context=self._contexts.get(run,{})
            if kind==1 and context.get('executed_config') is not None:
                if rev!=context['config_revision'] or cfg.pack()!=SpeedConfig(**context['executed_config']).pack():
                    raise ValueError('实验记录PI或revision与执行时确认的RAM不一致')
            pending['value']=dict(run=run,revision=rev,start_tick=start,end_tick=end,count=count,status=state,
                stop_reason=reason,kind=kind,segment=segment,config=cfg.as_dict() if cfg else None);pending['reply']=True
        elif event.kind==SPEED_META and command=='speed_meta':
            run,start,switch,end,kind,segment,reserved,*fields=event.values
            if not self.info or reserved or (run,start,end,kind,segment)!=(self.info['run'],self.info['start_tick'],
                self.info['end_tick'],self.info['kind'],self.info['segment']):
                raise ValueError('阶段元数据与概要不一致')
            segments,deadline=decode_step(fields,pwm=kind==2) if run else ([],0)
            if run and segment==1 and not 0<=((switch-start)&0xffffffff)<=3000:raise ValueError('切段时间错误')
            pending['value']=dict(run=run,start_tick=start,switch_tick=switch,end_tick=end,kind=kind,
                                 segment=segment,segments=segments,deadline_tick=deadline);pending['reply']=True
        elif event.kind==SPEED_DATA and command=='speed_data':
            run,index,reserved,*fields=event.values
            meta=h.command_metadata[pending['request_id']]['values']
            if reserved or run!=meta['run'] or not meta['start']<=index<meta['start']+meta['count']:
                raise ValueError('样本身份或索引错误')
            sample=SpeedSample.unpack(fields)
            if sample.flags & (PRE|ACTIVE|POST) not in (PRE,ACTIVE,POST):
                raise ValueError('记录采样阶段标志冲突')
            if sample.flags & ACTIVE:
                segment=self.meta['segments'][1 if sample.flags & SECOND else 0]
                if (sample.target_left,sample.target_right)!=(segment['left'],segment['right']):
                    raise ValueError('采样目标与冻结阶段不一致')
                limit=self.info['config']['pwm_limit'] if self.info['kind']==1 else self.caps['pwm_limit']
                if max(abs(sample.pwm_left),abs(sample.pwm_right))>limit:
                    raise ValueError('采样输出超过本次配置')
            old=pending.setdefault('samples',{}).get(index)
            if old and old.pack()!=sample.pack():raise ValueError('同索引样本冲突')
            pending['samples'][index]=sample;pending['reply']=len(pending['samples'])==meta['count']
        else:return
        needs=command not in ('speed_arm','speed_arm_pwm','speed_run','speed_pwm')
        if not pending['ack'] or (needs and not pending['reply']):return
        if command=='speed_caps':
            self.caps=pending['value'];h.adapter.speed_negotiated=True;self._last_diag=None
        elif command in ('speed_read','speed_apply'):
            self.revision,self.config=pending['value']
            self.config_context=deepcopy(self._apply_context) if command=='speed_apply' else {}
        elif command=='speed_info':
            value=pending['value']
            if not self.info or value['run']!=self.info['run']:self.samples={};self.integrity_errors=[]
            self.info=value;self.meta=None;self._meta_needed=bool(value['run'])
            self.result_context=deepcopy(self._contexts.get(value['run'],{}))
            self.active_run=value['run'] or None
            self.record_generation+=1
        elif command=='speed_meta':self.meta=pending['value']
        elif command=='speed_data':self.samples.update(pending['samples'])
        elif command in ('speed_run','speed_pwm'):
            h.yaw_control.release_capture()
            # A new accepted experiment cannot display/export the last run as its result.
            self.info=self.meta=None;self.samples={};self.result_context={};self.saved_directory=None
            self.integrity_errors=[];self._meta_needed=False;self.record_generation+=1
            self.active_run=event.seq
            self.live=deque(((run,s) for run,s in self.live if run==event.seq),maxlen=384)
        self.status=('能力已确认；当前固件未启用速度PI，开环可用' if self.caps and not self.caps['algorithm_ready']
                     else '能力已确认；当前固件没有目标前置滤波，闭环不可用' if self.caps and not self.caps.get('target_filter')
                     else 'ACK及回读已确认' if needs else 'MCU已接受；执行以STATE和记录为准')
        h._project_pending.pop(event.seq,None)
        h._remember_result(DeviceResult(True,'accepted',self.status,pending['request_id']))

    def poll(self):
        if not self.host.connected:
            self.download_running=False;self._meta_needed=False;return
        if self._meta_needed and not self.host.project_request_pending:
            self._meta_needed=False
            self.read_log_meta()
        if self.download_running and self._download_request:
            r=self.host.command_results.get(self._download_request)
            if r and r.status=='accepted' and not self.host.project_request_pending:self._next_batch()
            elif r and r.status in ('unknown','rejected'):
                self.download_running=False;self.status='读取中断，不自动重发；已收到数据保留'

