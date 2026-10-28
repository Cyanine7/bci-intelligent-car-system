"""角速度服务：能力、ACK/回读、运行快照和分块记录完整性。"""
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv
import json
import math
import struct
import time
from .device import DeviceResult
from .yaw_models import COMMANDS, CAPS, INFO, STEP, WHEEL_SPEED_LIMIT, YawConfig, YawSample, validate_segments
from .yaw_metrics import analyze_capture

class YawService:
    def __init__(self,host):self.host=host;self.reset()
    def reset(self):
        self.caps=self.config=self.revision=self.imu=self.info=self.meta=self.run_config=None
        self.samples={};self.live=deque(maxlen=1040);self.parts={};self.context={};self.run_context={}
        self.contexts={};self.download_running=False;self.download_request=None;self.saved_directory=None
        self.status='尚未查询角速度能力';self.active_run=None;self.integrity_errors=[];self.predictions=None;self.verified_indices=set()
        self._imu_poll_request=None;self._imu_poll_deadline=None;self._imu_poll_due=0.
        self._imu_telemetry_tick=None
        self._capture_followup=None
    def release_capture(self):
        self.info=self.meta=self.run_config=None;self.samples={};self.live.clear();self.parts={}
        self.active_run=None;self.run_context={};self.saved_directory=None;self.predictions=None;self.verified_indices=set()
        self._capture_followup=None
    def send(self,command,values=None):return self.host.send_command(command,values)
    def query_capabilities(self):return self.send('yaw_caps')
    def read_config(self):return self.send('yaw_read')
    def apply_config(self,cfg):return self.send('yaw_apply',asdict(cfg.validate()))
    def calibrate(self):
        result=self.send('yaw_calibrate')
        if result.success:
            # 新校准尚未确认时，不能继续沿用上一轮校准的使能条件。
            self.imu=None;self._imu_poll_request=None;self._imu_poll_deadline=None
        return result
    def read_status(self):return self.send('yaw_status')
    def arm(self):return self.send('yaw_arm')
    def read_log_info(self):return self.send('yaw_info')
    def read_log_meta(self):return self.send('yaw_meta')
    @property
    def imu_unavailable_reason(self):
        if not self.imu:return '等待IMU状态；采集后将自动更新'
        state=self.imu['calibration']
        if state==1:return '正在采集陀螺仪零偏，完成后自动更新'
        if state==3:return '零偏校准失败或被取消，请保持静止后重新采集'
        if state!=2:return '请先采集陀螺仪零偏'
        if not self.imu['valid'] or self.imu['age_ms']>60:return 'IMU数据无效或已过期，等待新数据'
        if not 0<=time.monotonic()-self.imu['received']<.5:return 'IMU状态已过期，等待角速度遥测或读取IMU状态'
        return ''
    @property
    def imu_ready(self):return not self.imu_unavailable_reason
    @property
    def result_fetching(self):return self._capture_followup is not None or self.download_running
    def run_step(self,segments,kind=1,expected_config=None):
        if expected_config is None or self.config is None or expected_config.pack()!=self.config.pack():
            return DeviceResult(False,'rejected','内外环草稿与确认RAM不一致，请先应用并回读')
        result=self.send('yaw_run',dict(segments=segments,kind=kind))
        if result.success:
            seq=self.host.command_metadata[result.request_id]['seq']
            self.contexts[seq]=dict(config=asdict(self.config),revision=self.revision,context=deepcopy(self.context),
                segments=deepcopy(segments),kind=kind,connection_id=self.host.connection_id,session=self.host.adapter.session)
            while len(self.contexts)>8:self.contexts.pop(next(iter(self.contexts)))
        return result
    def prepare(self,command,values):
        h=self.host
        if h.io_backlog_exceeded or not h.project_state_fresh or h.project_request_pending:
            raise ValueError('需要健康通信、新鲜STATE且无在途请求')
        if command=='yaw_caps':self.caps=None;h.adapter.yaw_negotiated=False
        elif self.caps is None:raise ValueError('请先查询角速度实验能力')
        if command not in ('yaw_run','yaw_status') and (h.latest.armed or h.project_motion_active):
            raise ValueError('此操作需要STOP并确认失能')
        if self.download_running and command!='yaw_data':raise ValueError('正在读取记录')
        if self.result_fetching and command in ('yaw_caps','yaw_apply','yaw_calibrate','yaw_arm','yaw_run'):
            raise ValueError('正在读取本次实验记录并计算指标，请等待完成')
        if command in ('yaw_arm','yaw_apply','yaw_run') and not self.caps['ready']:raise ValueError('设备角速度算法或内环滤波未就绪')
        if command=='yaw_apply':
            cfg=YawConfig(**values).validate()
            if cfg.pwm_limit>self.caps['pwm_limit']:raise ValueError('PWM超过实际能力')
        if command=='yaw_arm':
            if self.config is None or not h.latest.local_enable:raise ValueError('需要参数回读及本地允许')
            if self.imu_unavailable_reason:raise ValueError(self.imu_unavailable_reason)
        if command=='yaw_run':
            if not h.latest.armed or h.latest.control_mode!=5 or h.project_motion_active:raise ValueError('请先使能角速度实验模式')
            s=validate_segments(values['segments'],values.get('kind',1),max_segment=self.caps['max_segment_ms'],
                max_total=self.caps['max_total_ms'],speed_limit=self.caps['speed_limit'],omega_limit=self.caps['omega_limit'])
            elapsed=max(0,int((time.monotonic()-h.latest.received_monotonic)*1000))
            values['deadline_tick']=(h.latest.device_time_ms+elapsed+sum(x['duration_ms'] for x in s))&0xffffffff
        if command=='yaw_data':
            if not self.info or values['run']!=self.info['run'] or not 1<=values['count']<=8 or values['start']+values['count']>self.info['count']:
                raise ValueError('记录身份或索引不符')
        return values
    def _sample(self,payload,parts,key):
        run,index,part,reserved=struct.unpack_from('<IHBB',payload)
        if reserved or part not in (0,1) or len(payload)!=(44 if part else 56):raise ValueError('样本分块格式错误')
        blocks=parts.setdefault(key,{})
        if part in blocks and blocks[part]!=payload[8:]:raise ValueError('样本分块内容冲突')
        blocks[part]=payload[8:]
        if len(blocks)==2:
            s=YawSample.unpack(blocks[0]+blocks[1]);parts.pop(key,None);return run,index,s
        return None
    def diagnostic(self,event):
        if not self.caps:return
        p=event.values[0];run,index=struct.unpack_from('<IH',p)
        if self.active_run is not None and run!=self.active_run:return
        item=self._sample(p,self.parts,(event.seq,run,index))
        while len(self.parts)>8:self.parts.pop(next(iter(self.parts)))
        if item:
            s=item[2]
            if not self.live or 0<((s.tick-self.live[-1].tick)&0xffffffff)<0x80000000:self.live.append(s)
            # 完成校准必须先取得状态回包和ACK；遥测只刷新已确认校准的实时数据，
            # 不凭flags猜测校准完成。避免用户必须在手动读取后的0.5秒内点使能。
            if self.imu and self.imu['calibration']==2:
                newer=self._imu_telemetry_tick is None or 0<((s.tick-self._imu_telemetry_tick)&0xffffffff)<0x80000000
                not_older=((s.gyro_tick-self.imu['tick'])&0xffffffff)<0x80000000
                if newer and not_older:
                    age=(s.tick-s.gyro_tick)&0xffffffff
                    valid=bool(s.flags&32 and s.flags&64 and not s.flags&384 and age<=60)
                    self.imu.update(valid=valid,seq=s.gyro_seq,tick=s.gyro_tick,raw=s.raw_gyro,
                                    omega=s.omega,age_ms=age,received=time.monotonic())
                    self._imu_telemetry_tick=s.tick
    def handle(self,event,pending):
        h=self.host;command=pending['command'];kind=event.kind
        if kind==0x82:
            actual,status=event.values
            if actual!=COMMANDS[command]:return
            if status:
                pending['failed']=True;h._project_pending.pop(event.seq,None);self.download_running=False
                if h._motion_latched_seq==event.seq:h._motion_latched_seq=0
                from .project_protocol import ACK_MESSAGES
                self.status=ACK_MESSAGES[status] if status<len(ACK_MESSAGES) else '未知拒绝状态'
                h._remember_result(DeviceResult(False,'rejected',self.status,pending['request_id']));return
            pending['ack']=True
        else:
            p=event.values[0]
            if kind==0x8f and command=='yaw_caps':
                v,flags,hz,pwm,speed,seg,total,capacity,stale,W,omega=CAPS.unpack(p)
                if v!=1 or flags&~3 or hz!=100 or not 1<=pwm<=16500 or not 1<=speed<=round(WHEEL_SPEED_LIMIT*1000) or not 10<=seg<=5000 or not 20<=total<=10000 or capacity!=1040 or stale!=60 or W!=.16 or omega!=1.:
                    raise ValueError('角速度能力不符合协议')
                pending['value']=dict(ready=flags==3,control_hz=hz,pwm_limit=pwm,speed_limit=speed/1000,
                    max_segment_ms=seg,max_total_ms=total,capacity=capacity,stale_ms=stale,track=W,omega_limit=omega)
            elif kind==0x90 and command in ('yaw_read','yaw_apply'):
                rev=struct.unpack_from('<I',p)[0];cfg=YawConfig.unpack(p[4:]) if any(p[4:]) else None
                if command=='yaw_apply' and (cfg is None or cfg.pack()!=YawConfig(**h.command_metadata[pending['request_id']]['values']).pack()):
                    raise ValueError('内外环实际RAM与提交值不一致')
                pending['value']=(rev,cfg)
            elif kind==0x91 and command in ('yaw_status','yaw_calibrate'):
                cal,valid,n,seq,tick,raw,bias,corrected,started,age=struct.unpack('<BBHIIfffII',p)
                if cal>3 or valid>1 or not all(math.isfinite(x) for x in (raw,bias,corrected)):raise ValueError('IMU状态无效')
                pending['value']=dict(calibration=cal,valid=bool(valid),count=n,seq=seq,tick=tick,raw=raw,bias=bias,
                    omega=corrected,start=started,age_ms=age,received=time.monotonic())
            elif kind==0x92 and command=='yaw_info':
                run,rev,start,end,n,state,reason,typ,seg,reserved,bias=INFO.unpack(p)
                if reserved or n>1040 or state>4 or reason>6 or typ not in (0,1,2) or seg not in (0,1,255) or not math.isfinite(bias) or run>h.adapter.seq:
                    raise ValueError('角速度记录概要无效')
                pending['value']=dict(run=run,revision=rev,start=start,end=end,count=n,status=state,reason=reason,kind=typ,segment=seg,bias=bias)
            elif kind==0x96 and command=='yaw_info':
                pending['run_revision']=struct.unpack_from('<I',p)[0]
                pending['run_config']=YawConfig.unpack(p[4:]) if any(p[4:]) else None
            elif kind==0x93 and command=='yaw_meta':
                run,start,switch,end,typ,seg,reserved=struct.unpack_from('<IIIIBBH',p)
                if not self.info or run!=self.info['run'] or start!=self.info['start'] or end!=self.info['end'] or typ!=self.info['kind'] or seg!=self.info['segment'] or reserved:
                    raise ValueError('阶段身份与概要不一致')
                v,typ2,r,v0,t0,v1,t1,first,total,deadline=STEP.unpack(p[20:])
                if v!=1 or r or typ2!=typ:raise ValueError('阶段命令无效')
                segments=validate_segments([dict(v=v0,target=t0,duration_ms=first),dict(v=v1,target=t1,duration_ms=total-first)],typ)
                pending['value']=dict(run=run,start=start,switch=switch,end=end,kind=typ,segment=seg,segments=segments,deadline=deadline)
            elif kind in (0x94,0x95) and command=='yaw_data':
                requested=h.command_metadata[pending['request_id']]['values'];run,index=struct.unpack_from('<IH',p)
                if run!=requested['run'] or not requested['start']<=index<requested['start']+requested['count']:raise ValueError('下载样本身份错误')
                item=self._sample(p,pending.setdefault('parts',{}),index)
                if item:
                    pending.setdefault('samples',{})[index]=item[2]
                    self.samples[index]=item[2]  # 即使本批随后缺块/超时，已收到的完整样本也保留。
            else:return
        needs=command not in ('yaw_arm','yaw_run')
        ready=(not needs or 'value' in pending) if command!='yaw_data' else len(pending.get('samples',{}))==h.command_metadata[pending['request_id']]['values']['count']
        if command=='yaw_info':ready=ready and 'run_config' in pending
        if not pending['ack'] or not ready:return
        value=pending.get('value')
        if command=='yaw_caps':self.caps=value;h.adapter.yaw_negotiated=True
        elif command in ('yaw_read','yaw_apply'):self.revision,self.config=value
        elif command in ('yaw_status','yaw_calibrate'):
            self.imu=value
            if value['calibration']==1:
                if self._imu_poll_deadline is None:self._imu_poll_deadline=time.monotonic()+5.
                self._imu_poll_due=time.monotonic()+.25
            else:
                self._imu_poll_request=None;self._imu_poll_deadline=None
        elif command=='yaw_info':
            cfg=pending['run_config'];context=self.contexts.get(value['run'])
            if value['run'] and (cfg is None or pending['run_revision']!=value['revision']):raise ValueError('记录缺少一致配置快照')
            if context and (value['revision']!=context['revision'] or cfg.pack()!=YawConfig(**context['config']).pack()):raise ValueError('执行记录与冻结参数不一致')
            self.info=value;self.run_config=cfg;self.run_context=deepcopy(context or {});self.meta=None;self.samples={};self.verified_indices=set()
        elif command=='yaw_meta':self.meta=value
        elif command=='yaw_data':
            self.samples.update(pending['samples']);self.verified_indices.update(pending['samples'])
        elif command=='yaw_run':
            self.active_run=event.seq;self.info=self.meta=None;self.samples={};self.live.clear();self.parts={};self.saved_directory=None;self.integrity_errors=[];self.verified_indices=set()
            self._capture_followup=dict(run=event.seq,phase='wait_stop',request=None,due=0.,deadline=None)
        if command=='yaw_run':
            other=h.speed_control
            other.info=other.meta=None;other.samples={};other.live.clear();other.result_context={}
            self.predictions=None
        self.status='ACK及回读已确认' if needs else 'MCU已接受；以STATE和记录为准'
        if command in ('yaw_status','yaw_calibrate'):
            self.status=('尚未校准陀螺仪零偏','正在采集陀螺仪零偏，自动更新进度',
                         '零偏校准完成，可使能角速度实验' if self.imu_ready else '零偏已校准，等待有效IMU数据',
                         '零偏校准失败或被取消，请保持静止后重新采集')[value['calibration']]
        h._project_pending.pop(event.seq,None);h._remember_result(DeviceResult(True,'accepted',self.status,pending['request_id']))
    def download(self):
        if not self.info or not self.meta or not self.info['count'] or self.info['status'] not in (3,4):return DeviceResult(False,'rejected','先读取停车后的非空概要和阶段')
        self.samples={};self.verified_indices=set();self.integrity_errors=[];self.download_running=True;return self._next()
    def _next(self):
        if len(self.verified_indices)==self.info['count']:
            self.download_running=False
            self.saved_directory=self.export();return DeviceResult(True,'complete','完整下载并保存')
        start=next(i for i in range(self.info['count']) if i not in self.verified_indices)
        result=self.send('yaw_data',dict(run=self.info['run'],start=start,count=min(8,self.info['count']-start)))
        self.download_request=result.request_id
        if not result.success:self.download_running=False
        return result
    def poll(self):
        if not self.host.connected:
            self.download_running=False;self._imu_poll_request=None;self._imu_poll_deadline=None;self._capture_followup=None;return
        self._poll_calibration()
        if self.download_running and self.download_request:
            r=self.host.command_results.get(self.download_request)
            if r and r.status=='accepted' and not self.host.project_request_pending:
                try:self._next()
                except (OSError,ValueError) as exc:self.download_running=False;self.status=str(exc)
            elif r and r.status in ('unknown','rejected'):
                self.download_running=False;self.status='下载中断，已收到数据保留；不自动重发'
                self.integrity_errors.append('下载请求'+str(self.download_request)+'：'+r.status)
        self._poll_completed_run()
    def _poll_completed_run(self):
        """仅对本服务已确认执行的实验自动读概要、阶段及记录；不发运动命令。"""
        job=self._capture_followup
        if job is None:return
        h=self.host;now=time.monotonic()
        if job['phase']=='wait_stop':
            if not h.project_state_fresh or h.latest.armed or h.latest.device_last_seq<job['run']:return
            job.update(phase='info',due=now+.25,deadline=now+3.)
            self.status='实验已结束，等待后采样并自动计算指标';return
        if job['phase']=='download':
            if self.download_running:return
            self._capture_followup=None
            if self.saved_directory:self.status='本次实验指标已计算；已保存 '+self.saved_directory.name
            return
        request=job.get('request')
        if request:
            result=h.command_results.get(request)
            if not result or result.status not in ('accepted','unknown','rejected'):return
            job['request']=None
            if result.status!='accepted':
                self._capture_followup=None;self.status='自动读取实验结果未确认，已停止；可手动读取，不自动重发';return
            if job['phase']=='info_pending':
                if not self.info or self.info['run']!=job['run']:
                    self._capture_followup=None;self.status='实验记录身份不一致，未计算本次指标';return
                if self.info['status'] in (3,4) and self.info['count']:job['phase']='meta'
                elif now<job['deadline']:job.update(phase='info',due=now+.1)
                else:
                    self._capture_followup=None;self.status='等待完整后采样超时，请手动读取实验概要';return
            elif job['phase']=='meta_pending':
                result=self.download()
                if result.success:job['phase']='download';self.status='正在下载本次100Hz记录并计算指标'
                else:self._capture_followup=None;self.status=result.message
                return
        if now<job['due'] or h.project_request_pending or not h.project_state_fresh or h.latest.armed or h.project_motion_active:return
        if job['phase'] not in ('info','meta'):return
        result=self.read_log_info() if job['phase']=='info' else self.read_log_meta()
        if result.success:job['request']=result.request_id;job['phase']+='_pending'
        else:self._capture_followup=None;self.status=result.message
    def _poll_calibration(self):
        """用户发起校准后短时读取进度；未知结果不重发，不触发ARM或运动。"""
        if self._imu_poll_deadline is None:return
        h=self.host;now=time.monotonic()
        if self._imu_poll_request:
            result=h.command_results.get(self._imu_poll_request)
            if result and result.status in ('unknown','rejected'):
                self._imu_poll_request=None;self._imu_poll_deadline=None
                self.status='校准状态读取未确认，请手动读取IMU状态；不自动重发'
                return
            if not result or result.status!='accepted':return
            self._imu_poll_request=None
        if now>=self._imu_poll_deadline:
            self._imu_poll_deadline=None;self.status='等待校准完成状态超时，请读取IMU状态';return
        if (now<self._imu_poll_due or not self.caps or self.download_running or h.project_request_pending or
            not h.project_state_fresh or h.io_backlog_exceeded or h.latest.armed or h.project_motion_active):return
        result=self.read_status()
        if result.success:self._imu_poll_request=result.request_id
        else:
            self._imu_poll_deadline=None;self.status=result.message
    @property
    def complete(self):
        if not self.info or not self.meta or self.info['status']!=3 or not self.info['count'] or len(self.samples)!=self.info['count'] or len(self.verified_indices)!=self.info['count']:return False
        ordered=[self.samples.get(i) for i in range(self.info['count'])]
        return all(ordered) and all(((b.tick-a.tick)&0xffffffff)==b.dt for a,b in zip(ordered,ordered[1:])) and not self.integrity_errors
    def analysis(self):
        return analyze_capture(self.info,self.meta,self.samples,self.complete,self.run_context,
                               asdict(self.run_config) if self.run_config else None)
    def gyro_rate(self):
        """按记录中的gyro_seq变化估计实际采样率；无记录时保留旧固件默认55Hz。"""
        ordered=[s for _,s in sorted(self.samples.items())] if self.samples else []
        if len(ordered)<2:return 55.
        changes=sum(1 for a,b in zip(ordered,ordered[1:]) if b.gyro_seq!=a.gyro_seq)
        span=((ordered[-1].tick-ordered[0].tick)&0xffffffff)/1000
        rate=changes/span if span>0 else 0.
        return rate if 20<=rate<=1000 else 55.
    def export(self):
        from .yaw_analysis import predict
        from .motor_models import MotorModel
        self.predictions=None
        model=self.run_context.get('context',{}).get('model')
        if model and self.run_config and self.meta:
            # 停车后按本次冻结参数和实际请求阶段重算，不沿用界面上一组预测。
            options=self.run_context.get('context',{}).get('prediction_options',{})
            self.predictions=predict(self.run_config,MotorModel(**model),self.meta['segments'],self.meta['kind'],
                                     gyro_hz=self.gyro_rate(),**options)
        directory=Path(self.host.data_directory)/('yaw_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_UTC_')+uuid4().hex[:8]);directory.mkdir(parents=True)
        with (directory/'samples.csv').open('w',newline='',encoding='utf-8-sig') as f:
            fields=['index',*YawSample.__dataclass_fields__,'sample_hex'];w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
            for i,s in sorted(self.samples.items()):w.writerow(dict(index=i,**asdict(s),sample_hex=s.pack().hex()))
        summary=dict(schema_version=1,source=self.host.adapter.source,complete=self.complete,caps=self.caps,info=self.info,meta=self.meta,
            run_config=asdict(self.run_config) if self.run_config else None,context=self.run_context,
            predictions=self.predictions,analysis=self.analysis(),integrity_errors=self.integrity_errors)
        (directory/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        self.status=f'已保存：{directory.name}';return directory
