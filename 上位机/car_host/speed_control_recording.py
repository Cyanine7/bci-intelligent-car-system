"""Export an immutable speed capture, including identity and raw sample bytes."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
from uuid import uuid4
from .speed_control_analysis import analyze, predict, step_range
from .motor_models import MotorModel
from .speed_control_models import SpeedConfig
from .speed_assessment import design_spec_from_context

def export_capture(service, directory):
    if not service.info or not service.caps:
        raise ValueError('没有实验记录')
    # New directory per export prevents silently replacing earlier evidence.
    path = Path(directory)/('speed_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_UTC_')+uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=False)
    summary = dict(schema_version=2, connection_id=service.host.connection_id,
        session=service.host.adapter.session, caps=service.caps, info=service.info,
        design_context=service.result_context,
        meta=service.meta, integrity_errors=service.integrity_errors,
        voltage_range_mv=([min(s.battery_mv for s in service.samples.values()),
                           max(s.battery_mv for s in service.samples.values())] if service.samples else None),
        complete=service.complete, analysis=analyze(service.info, service.samples, service.caps, service.meta,
            design_spec_from_context(service.result_context)))
    summary['predictions']={}
    try:
        model_data=service.result_context.get('prediction',{}).get('model')
        if model_data and service.meta and service.meta['segments']:
            model=MotorModel(**model_data).validate()
            pwm=service.info['kind']==2
            cfg=SpeedConfig(2,16500,0.,1.,0.,1.) if pwm else SpeedConfig(**service.info['config'])
            for i,(side,(K,tau)) in enumerate(zip(('left','right'),model.wheels)):
                # 历史CAPS未声明滤波时，保留当时的未滤波算法进行重算。
                filtered=bool(service.caps.get('target_filter',False))
                predicted=predict(K,tau,cfg,i,segments=service.meta['segments'],pwm=pwm,target_filter=filtered)
                summary['predictions'][side]=dict(
                    continuous_overshoot_percent=predicted['ideal_overshoot_percent'],
                    discrete_overshoot_percent=predicted['discrete_overshoot_percent'],
                    continuous_settling_time_s=predicted['continuous_settling_time_s'],
                    discrete_settling_time_s=predicted['discrete_settling_time_s'],
                    discrete_pwm_peak=max(abs(row[4]) for row in predicted['discrete']),
                    discrete_saturation_s=predicted['discrete_saturation_s'],
                    target_filter=predicted['target_filter'],target_filter_tau_s=predicted['target_filter_tau_s'],
                    scope=predicted['model_scope'])
                actual=summary['analysis']['wheels'].get(side,{})
                if not pwm and 'u0' in actual:
                    actual['actual_step_range']=step_range(cfg,i,actual['u0'],actual['r0']-actual['v0'],actual['r0'],target_filter=filtered)
        else:
            summary['prediction_reason']='没有与本次实验关联的模型快照'
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        summary['prediction_reason']=str(exc)
    with (path/'samples.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        fields = ['index', *asdict(next(iter(service.samples.values()))).keys(), 'sample_hex'] if service.samples else ['index']
        writer = csv.DictWriter(stream, fieldnames=fields);writer.writeheader()
        for index,sample in sorted(service.samples.items()):
            writer.writerow(dict(index=index, **asdict(sample), sample_hex=sample.pack().hex()))
    (path/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return path
