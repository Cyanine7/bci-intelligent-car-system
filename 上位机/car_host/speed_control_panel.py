"""Editable models, binary64 PI design and two-stage experiment UI."""
from dataclasses import replace
from copy import deepcopy
from pathlib import Path
import json
import math
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog,QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,
    QLabel,QPushButton,QLineEdit,QSpinBox,QComboBox,QCheckBox,QPlainTextEdit,QTabWidget,QScrollArea)
import pyqtgraph as pg
from .motor_models import MotorModel,MotorModelStore,DEFAULTS,DEFAULT_CONDITION
from .speed_control_models import SpeedConfig,ACTIVE,SECOND,SPEED_LIMIT_MM_S
from .speed_control_analysis import DEFAULT_MP,design_pair,predict,analyze,step_range,rolling_deadzone,describe_analysis
from .speed_control_recording import export_capture
from .speed_assessment import design_spec_from_context
from .speed_smoothing import smooth_speed

class FloatEdit(QLineEdit):
    def __init__(self,value=0.):
        super().__init__();self.setValue(value);self.setMinimumWidth(150)
    def value(self):
        try:v=float(self.text())
        except ValueError:raise ValueError('请输入十进制或科学计数法数值')
        if not math.isfinite(v):raise ValueError('数值必须有限')
        return v
    def setValue(self,value):self.setText(format(float(value),'.17g'))

class SpeedControlPanel(QDialog):
    def __init__(self,host):
        super().__init__();self.host=host;self.service=host.speed_control
        from .runtime import prepare_qt_fonts
        prepare_qt_fonts()
        self.setWindowTitle('速度环实验 · 增量式 PI');self.resize(1180,900)
        self._updating=True;self._condition=DEFAULT_CONDITION;self.last_request=None
        self._design_stale=True;self._calculation=None;self._plot_token=None;self._analysis_token=None
        self._view_generation=(host.connection_id,self.service.record_generation)
        self._open_points={};self._last_points_run=None;self._deadzone_results={}
        if not hasattr(host,'speed_model_drafts'):host.speed_model_drafts={}
        self.model_drafts=host.speed_model_drafts
        self.model_store=MotorModelStore(Path(host.data_directory).parent/'config'/'motor_models.json')
        layout=QVBoxLayout(self);self.status_label=QLabel();self.status_label.setWordWrap(True);layout.addWidget(self.status_label)
        self.tabs=tabs=QTabWidget();layout.addWidget(tabs);curves=QWidget()
        forms=[]
        for title in ('电机模型','PI 参数','阶跃执行'):
            settings=QWidget();scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(settings)
            tabs.addTab(scroll,title);forms.append(QVBoxLayout(settings))
        curves_scroll=QScrollArea();curves_scroll.setWidgetResizable(True);curves_scroll.setWidget(curves)
        tabs.addTab(curves_scroll,'曲线与指标')
        form=forms[0]
        self.voltage_label=QLabel();form.addWidget(self.voltage_label)
        grid=QGridLayout();form.addLayout(grid)
        self.condition=QComboBox();self.condition.addItems(DEFAULTS)
        self.condition.setCurrentText(DEFAULT_CONDITION)
        self.profiles=QComboBox()
        self.model_name=QLineEdit('默认');self.reference_voltage=QLineEdit();self.reference_voltage.setPlaceholderText('可选，单位 V')
        self.notes=QLineEdit();self.notes.setPlaceholderText('电压、路面或负载说明')
        for col,(label,field) in enumerate((('工况',self.condition),('模型选择',self.profiles),('模型名称',self.model_name),('参考电压 V',self.reference_voltage))):
            grid.addWidget(QLabel(label),0,col);grid.addWidget(field,1,col)
        self.model_boxes=[]
        for i,label in enumerate(('左 K (m/s)/PWM','左 τ (s)','右 K (m/s)/PWM','右 τ (s)')):
            box=FloatEdit();self.model_boxes.append(box);grid.addWidget(QLabel(label),2,i);grid.addWidget(box,3,i)
        grid.addWidget(QLabel('备注'),4,0);grid.addWidget(self.notes,4,1,1,3)
        row=QHBoxLayout();form.addLayout(row);self.buttons={}
        for key,label,cb in (('restore','恢复当前工况默认模型',self._restore),('save_model','保存为自定义模型',self._save_model),
                             ('identified_model','辨识结果复制到模型草稿',self._identified_model)):
            self._button(row,key,label,cb)
        self.model_status=QLabel(self.model_store.warning);self.model_status.setWordWrap(True);form.addWidget(self.model_status)
        form.addStretch()
        form=forms[1]
        design_grid=QGridLayout();form.addLayout(design_grid)
        self.mode=QComboBox();self.mode.addItems(['闭环速度阶跃','开环 PWM 阶跃'])
        self.algorithm=QComboBox();self.algorithm.addItem('增量 PI + 目标前置滤波',2)
        self.entry_method=QComboBox();self.entry_method.addItems(['直接输入 Kp / Ki','按 Mp / ts 计算']);self.entry_method.setCurrentIndex(1)
        self.mp=FloatEdit(DEFAULT_MP*100);self.settling=FloatEdit(.2);self.design_target=FloatEdit(.1)
        self.limit=QSpinBox();self.limit.setRange(1,16500);self.limit.setValue(16500)
        for i,(label,field) in enumerate((('控制算法',self.algorithm),('参数方式',self.entry_method),
            ('理论设计超调量 Mp (%)',self.mp),('理论设计调节时间 ts (s，2%)',self.settling),('预测阶跃 (m/s)',self.design_target),('闭环PWM幅值上限',self.limit))):
            design_grid.addWidget(QLabel(label),i//3*2,i%3);design_grid.addWidget(field,i//3*2+1,i%3)
        row=QHBoxLayout();form.addLayout(row)
        self.calculate_button=self._button(row,'calculate','计算并填入PI草稿（不发送）',self._calculate)
        self._button(row,'preview','刷新预测与阶跃范围',self._preview)
        gain_grid=QGridLayout();form.addLayout(gain_grid);self.gain_boxes=[]
        for i,label in enumerate(('左 Kp','左连续 Ki','右 Kp','右连续 Ki')):
            box=FloatEdit();self.gain_boxes.append(box);gain_grid.addWidget(QLabel('草稿 · '+label),0,i);gain_grid.addWidget(box,1,i)
        self.actual_label=QLabel('实际RAM：尚未回读');self.actual_label.setWordWrap(True);form.addWidget(self.actual_label)
        row=QHBoxLayout();form.addLayout(row)
        for key,label,cb in (('read','读取实际RAM',self.service.read_config),
                            ('copy','实际复制到草稿',self._copy),('apply','应用草稿并回读',self._apply)):
            self._button(row,key,label,cb)
        self.prediction=QPlainTextEdit();self.prediction.setReadOnly(True);self.prediction.setMinimumHeight(180);form.addWidget(self.prediction)
        form=forms[2]
        row=QHBoxLayout();row.addWidget(QLabel('实验模式'));row.addWidget(self.mode);form.addLayout(row)
        motion_grid=QGridLayout();form.addLayout(motion_grid);self.targets=[];self.durations=[]
        for stage in range(2):
            motion_grid.addWidget(QLabel('第一段' if stage==0 else '第二段'),stage+1,0);boxes=[]
            for side in range(2):
                box=QSpinBox();box.setRange(-SPEED_LIMIT_MM_S,SPEED_LIMIT_MM_S);boxes.append(box);motion_grid.addWidget(box,stage+1,side+1)
            duration=QSpinBox();duration.setRange(10,2990);duration.setSingleStep(10);duration.setValue(1500)
            self.targets.append(boxes);self.durations.append(duration);motion_grid.addWidget(duration,stage+1,3)
        self.target_header=QLabel('左目标 mm/s');self.right_header=QLabel('右目标 mm/s')
        motion_grid.addWidget(self.target_header,0,1);motion_grid.addWidget(self.right_header,0,2);motion_grid.addWidget(QLabel('时长 ms'),0,3)
        self.execution_label=QLabel();self.execution_label.setWordWrap(True);form.addWidget(self.execution_label)
        self.left,self.right=self.targets[1];self.duration=self.durations[1]
        row=QHBoxLayout();form.addLayout(row)
        for index,(key,label,cb) in enumerate((('arm','显式使能当前模式',lambda:self.service.arm(self.mode.currentIndex()==1)),
            ('run','执行两段一次',self._run),('stop','STOP · 停止并失能',host.motion.stop),
            ('info','刷新概要及阶段数据',self.service.read_log_info),('download','读取完整100Hz记录',self.service.download),
            ('export','导出 CSV / JSON',self._export))):
            if index==3:
                row=QHBoxLayout();form.addLayout(row)
            self._button(row,key,label,cb)
        self.result_label=QLabel('编辑模型与参数不会发送或运动');self.result_label.setWordWrap(True);form.addWidget(self.result_label)
        form.addStretch()
        capability_row=QHBoxLayout()
        capability_row.addWidget(self.status_label,1)
        self._button(capability_row,'caps','查询设备实验能力',self.service.query_capabilities)
        layout.insertLayout(0,capability_row)
        layout.addWidget(self.result_label)
        curve_layout=QVBoxLayout(curves);row=QHBoxLayout();curve_layout.addLayout(row)
        self.pause=QCheckBox('暂停显示（继续接收与记录）');self.guides_enabled=QCheckBox('显示性能辅助线');self.guides_enabled.setChecked(True)
        row.addWidget(self.pause);row.addWidget(self.guides_enabled)
        curve_layout.addWidget(QLabel('实测速度：当前点及前四点滑动平均；10ms采样时约滞后20ms，原始速度以细线保留。'))
        self.plots=[];self.lines=[];self.guides=[]
        labels=[('左目标','左五点平滑速度','左原始速度','右目标','右五点平滑速度','右原始速度'),('左限幅前','左PWM','右限幅前','右PWM'),('左平滑误差','右平滑误差')]
        colors=['#338aff','#f59729','#38a65c','#a767db','#dc4b4b','#00a9ad']
        for title,names in zip(('速度 m/s','输出 PWM','误差 m/s'),labels):
            plot=pg.PlotWidget();plot.addLegend(colCount=3 if len(names)>4 else 2);plot.setLabel('left',title);plot.setLabel('bottom','相对阶跃时间','s');plot.showGrid(x=True,y=True)
            plot.setMinimumHeight(200)
            plot.getAxis('left').enableAutoSIPrefix(False)
            self.plots.append(plot);self.lines.append([plot.plot(name=name,pen=pg.mkPen(colors[i],width=1.5)) for i,name in enumerate(names)])
            if len(self.plots)>1:
                plot.setXLink(self.plots[0])
            curve_layout.addWidget(plot)
        self.metrics=QPlainTextEdit();self.metrics.setReadOnly(True);self.metrics.setMinimumHeight(190);curve_layout.insertWidget(1,self.metrics)
        self.estimated_lines=[self.lines[0][1],self.lines[0][4]]
        for line,color in zip(self.estimated_lines,('#f59729','#dc4b4b')):line.setPen(pg.mkPen(color,width=2.5))
        for index,color in ((2,'#38a65c'),(5,'#00a9ad')):
            pen_color=pg.mkColor(color);pen_color.setAlpha(85)
            self.lines[0][index].setPen(pg.mkPen(pen_color,width=1))
        self.filtered_target_lines=[self.plots[0].plot(name=label+'滤波目标',pen=pg.mkPen(color,width=1.5))
                                    for label,color in (('左','#b6cbff'),('右','#ebbdff'))]
        self.preview_timer=QTimer(self);self.preview_timer.setSingleShot(True);self.preview_timer.setInterval(250);self.preview_timer.timeout.connect(lambda:self._safe_preview())
        self.condition.currentTextChanged.connect(self._switch_condition);self.profiles.currentTextChanged.connect(self._select_profile)
        for box in self.model_boxes+[self.reference_voltage,self.model_name,self.notes]:box.textChanged.connect(self._model_changed)
        for box in (self.mp,self.settling,self.design_target):box.textChanged.connect(self._design_changed)
        for box in self.gain_boxes:box.textEdited.connect(self._gain_edited)
        self.algorithm.currentIndexChanged.connect(self._design_changed);self.entry_method.currentIndexChanged.connect(self.refresh)
        self.limit.valueChanged.connect(self._gain_edited)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        for box in [b for row in self.targets for b in row]+self.durations:box.valueChanged.connect(self._queue_preview)
        self.guides_enabled.toggled.connect(self._redraw);self.pause.toggled.connect(self._redraw)
        host.changed.connect(self.refresh)
        self._load_model(MotorModel.default(self._condition));self._update_profiles()
        if self._condition in self.model_drafts:
            raw,self._model_source=self.model_drafts[self._condition]
            for box,text in zip(self.model_boxes+[self.model_name,self.reference_voltage,self.notes],raw):box.setText(text)
        self._updating=False
        self._action(self._calculate);self.refresh()

    def _button(self,row,key,label,callback):
        button=QPushButton(label);row.addWidget(button);self.buttons[key]=button
        button.clicked.connect(lambda checked=False:self._action(callback));return button
    def _action(self,callback):
        try:
            result=callback()
            if result is not None:self.last_request=result.request_id;self.result_label.setText(result.message)
        except (ValueError,OSError,TypeError,KeyError) as exc:self.result_label.setText(str(exc))
        self.refresh()
    def _raw_model(self):
        return [b.text() for b in self.model_boxes]+[self.model_name.text(),self.reference_voltage.text(),self.notes.text()]
    def _model(self):
        ref=self.reference_voltage.text().strip()
        return MotorModel(self.condition.currentText(),*[b.value() for b in self.model_boxes],
            self.model_name.text(),float(ref) if ref else None,self.notes.text(),
            getattr(self,'_model_source','手动输入')).validate()
    def _load_model(self,model):
        previous=self._updating;self._updating=True
        for box,value in zip(self.model_boxes,[v for pair in model.wheels for v in pair]):box.setValue(value)
        self.model_name.setText(model.name);self.reference_voltage.setText('' if model.reference_voltage is None else format(model.reference_voltage,'.17g'))
        self.notes.setText(model.notes);self._model_source=model.source;self._updating=previous
    def _switch_condition(self,condition):
        if self._updating:return
        self.model_drafts[self._condition]=(self._raw_model(),getattr(self,'_model_source','手动输入'))
        self._condition=condition;self._updating=True
        raw=self.model_drafts.get(condition)
        if raw:
            texts,self._model_source=raw
            for box,text in zip(self.model_boxes+[self.model_name,self.reference_voltage,self.notes],texts):box.setText(text)
        else:self._load_model(MotorModel.default(condition))
        self._update_profiles();self._updating=False;self._model_changed()
    def _update_profiles(self):
        self.profiles.blockSignals(True);self.profiles.clear();self.profiles.addItem('默认')
        if self.condition.currentText()=='空载':self.profiles.addItem('内置：历史非满电空载')
        self.profiles.addItems([m.name for m in self.model_store.profiles.values() if m.condition==self.condition.currentText()])
        self.profiles.blockSignals(False)
    def _select_profile(self,name):
        if self._updating:return
        model=(MotorModel.default(self.condition.currentText()) if name=='默认' else
               MotorModel.historical_no_load() if name=='内置：历史非满电空载' else
               self.model_store.profiles[self.condition.currentText()+'/'+name])
        self._load_model(model);self._model_changed()
    def _restore(self):self._load_model(MotorModel.default(self.condition.currentText()));self._model_changed()
    def _save_model(self):
        model=self._model();self.model_store.save(model);self._update_profiles()
        self.model_status.setText('已保存自定义模型：'+model.name)
    def _model_changed(self,*args):
        if self._updating:return
        if args and not self._model_source.startswith('手动编辑'):
            self._model_source='手动编辑；来源 '+self._model_source
        self._design_stale=True
        try:self._model();self.model_status.setText('模型已修改，PI草稿尚未重新计算' if self.entry_method.currentIndex()==1 else '当前模型用于预测；手动PI保持不变')
        except ValueError as exc:self.model_status.setText(str(exc))
        self._queue_preview();self.refresh()
    def _design_changed(self,*args):
        if self._updating:return
        self._design_stale=True;self._queue_preview();self.refresh()
    def _gain_edited(self,*args):
        if self._updating:return
        self.entry_method.setCurrentIndex(0);self._calculation=None;self._queue_preview();self.refresh()
    def _queue_preview(self,*args):
        if not self._updating:self.preview_timer.start()
    def _mode_changed(self,*args):
        if self._updating:return
        pwm=self.mode.currentIndex()==1
        self._updating=True
        # Clear physical inputs on mode change; units change between mm/s and PWM.
        for row in self.targets:
            for box in row:box.setRange(-16500 if pwm else -SPEED_LIMIT_MM_S,16500 if pwm else SPEED_LIMIT_MM_S);box.setValue(0)
        self._updating=False
        self.target_header.setText('左 PWM' if pwm else '左目标 mm/s');self.right_header.setText('右 PWM' if pwm else '右目标 mm/s')
        self._queue_preview();self.refresh()
    def _draft(self):
        return SpeedConfig(2,self.limit.value(),*[b.value() for b in self.gain_boxes]).validate()
    def _calculate(self):
        model=self._model();target=self.design_target.value()
        if not 0<target<=SPEED_LIMIT_MM_S/1000:raise ValueError('计算阶跃应为0～1.18m/s正数')
        cfg,rows=design_pair(algorithm=2,model=model,mp_ratio=self.mp.value()/100,
                            settling_s=self.settling.value(),target=target,pwm_limit=self.limit.value())
        self._updating=True
        for box,value in zip(self.gain_boxes,[v for pair in cfg.gains for v in pair]):box.setValue(value)
        self.entry_method.setCurrentIndex(1);self._updating=False;self._design_stale=False
        self._calculation=dict(mp_ratio=self.mp.value()/100,design_ts_s=self.settling.value(),
            model=model.snapshot(),rows=rows,calculated_gains=cfg.as_dict())
        self.model_status.setText('PI草稿已按当前模型计算；尚未发送');self._preview();return None
    def _segments(self):
        return [dict(left=row[0].value(),right=row[1].value(),duration_ms=d.value()) for row,d in zip(self.targets,self.durations)]
    def _context(self):
        mp=self.mp.value()/100;ts=self.settling.value()
        if not 0<mp<1 or ts<=0:raise ValueError('理论设计要求0<Mp<100%，ts>0')
        return dict(model=self._model().snapshot(),parameter_source='performance' if self.entry_method.currentIndex()==1 else 'manual',
                    calculation=deepcopy(self._calculation) if self.entry_method.currentIndex()==1 else None,
                    design_spec=dict(mp_ratio=mp,design_ts_s=ts,band_ratio=.02,method='pole_matching'))
    def _apply(self):
        if self.entry_method.currentIndex()==1 and self._design_stale:raise ValueError('模型或设计目标已修改，请重新计算；也可切换直接输入')
        self.service.context=self._context();return self.service.apply_config(self._draft())
    def _run(self):
        problem=self._execution_issue()
        if problem:raise ValueError(problem)
        pwm=self.mode.currentIndex()==1
        self.service.context=self._context();return self.service.run_step(self._segments(),pwm=pwm,
            expected_config=None if pwm else self._draft())
    def _execution_issue(self):
        if self.mode.currentIndex()==1:return ''
        if self.service.caps and not self.service.caps.get('target_filter'):return '当前固件没有目标前置滤波，请更新固件后再进行闭环实验'
        if self.entry_method.currentIndex()==1 and self._design_stale:return '模型或设计值已修改，请重新计算，再应用并回读PI'
        try:draft=self._draft()
        except ValueError as exc:return str(exc)
        if self.service.config is None:return '需要应用并回读本次PI'
        if draft.pack()!=self.service.config.pack():return 'PI草稿与已确认RAM不一致，请先应用并回读'
        return ''
    def _copy(self):
        cfg=self.service.config
        if cfg is None:return
        self._updating=True;self.limit.setValue(cfg.pwm_limit)
        for box,value in zip(self.gain_boxes,[v for pair in cfg.gains for v in pair]):box.setValue(value)
        self.entry_method.setCurrentIndex(0);self._updating=False;self._calculation=None;self._preview()
    def _safe_preview(self):
        try:self._preview()
        except (ValueError,OverflowError) as exc:self.prediction.setPlainText(str(exc))
    def _preview(self):
        model=self._model();pwm=self.mode.currentIndex()==1
        cfg=SpeedConfig(2,16500,0.,1.,0.,1.) if pwm else self._draft()
        from .speed_control_models import validate_step
        segments=self._segments();valid_segments=True
        try:validate_step(dict(segments=segments),pwm=pwm)
        except ValueError:valid_segments=False
        text=[f'当前模型：{model.condition} / {model.name}；来源：{model.source}']
        if self._calculation and self.entry_method.currentIndex()==1:
            r=self._calculation['rows'][0];text.append(f'{r["design_method"]}；等效ζ={r["zeta"]:.9g}，ωn={r["wn"]:.9g} rad/s；'+('设计已修改，待重算' if self._design_stale else '计算结果已填入草稿'))
        if not valid_segments:
            target=self.design_target.value()
            segments=[dict(left=0,right=0,duration_ms=1500),dict(left=round(target*1000),right=round(target*1000),duration_ms=1500)] if not pwm else [dict(left=4000,right=4000,duration_ms=1500),dict(left=5000,right=5000,duration_ms=1500)]
            text.append('预测使用计算示例；实际执行输入仍为界面原值，尚未构成有效阶跃。')
        simulations=[]
        for i,((K,tau),(kp,ki)) in enumerate(zip(model.wheels,cfg.gains)):
            sim=predict(K,tau,cfg,i,segments=segments,pwm=pwm);simulations.append(sim)
            side='left' if i==0 else 'right';r0=segments[0][side]/1000
            rng=step_range(cfg,i,u0=r0/K,target0=r0)
            text.append(f'{"左" if i==0 else "右"} Kp={kp:.17g}，Ki={ki:.17g}，Ki×10ms={ki*.01:.9g}')
            if not pwm:text.append(f'目标前置滤波 Tf=Kp/Ki={kp/ki:.9g}s；理论及实测指标均相对原始目标计算')
            if not pwm:text.append(f'95%建议Δv范围={rng["recommended_delta"]} m/s；死区下界未知；PWM0为线性模型估计')
            text.append(f'连续预测超调={sim["ideal_overshoot_percent"]}%；离散预测超调={sim["discrete_overshoot_percent"]}%；离散PWM峰值={max(abs(v[4]) for v in sim["discrete"])}')
            text.append(f'连续预测ts={sim["continuous_settling_time_s"]}s；离散预测ts={sim["discrete_settling_time_s"]}s（2%带）')
            if not pwm:
                mp,ts=sim['discrete_overshoot_percent'],sim['discrete_settling_time_s']
                text.append('完整模型预测相对理论设计的偏差：'+
                            ('超调无法比较' if mp is None else f'ΔMp={mp-self.mp.value():+.6g}个百分点')+'；'+
                            ('ts无法比较' if ts is None else f'Δts={ts-self.settling.value():+.6g}s')+'；保持公式所得PI不变')
        text.append('局部增量模型线性外推；设计超调、连续预测、离散预测及实测分别比较。')
        self.prediction.setPlainText('\n'.join(text));self._simulations=simulations
        self._preview_segments=deepcopy(segments);self._preview_pwm=pwm
        if not self.service.samples and not self.service.live:self._plot_prediction()
    def _plot_prediction(self):
        if self.pause.isChecked():return
        for line in self.estimated_lines:line.clear()
        for i,sim in enumerate(self._simulations):
            first=self._preview_segments[0]['duration_ms']/1000
            d=sim['discrete'];t=[v[0]-first for v in d]
            side='left' if i==0 else 'right'
            target=[self._preview_segments[0 if v[0]<first-1e-9 else 1][side]/1000 for v in d]
            for line,ys in zip(self.lines[0][i*3:i*3+3],(target,[v[2] for v in d],[v[1] for v in d])):
                if line in self.estimated_lines or self._preview_pwm and line in self.lines[0][i*3:i*3+2]:line.clear()
                else:line.setData(t,ys)
            if self._preview_pwm:self.filtered_target_lines[i].clear()
            else:self.filtered_target_lines[i].setData(t,[v[2] for v in d])
            for line,k in zip(self.lines[1][i*2:i*2+2],(3,4)):line.setData(t,[v[k] for v in d])
            self.lines[2][i].setData(t,[v[2]-v[1] for v in d])
        self.metrics.setPlainText('当前显示离散预测；连续预测数值见设计页。')
    def _export(self):
        self.last_request=None;path=export_capture(self.service,self.host.data_directory);self.result_label.setText('已导出：'+str(path))
    def _identified_model(self):
        s=self.service
        if not s.complete:raise ValueError('需要完整开环记录')
        result=analyze(s.info,s.samples,s.caps,s.meta)
        if s.info['kind']!=2 or not result['valid']:raise ValueError('请选择有效开环辨识记录')
        current=self._model();values=list(v for pair in current.wheels for v in pair);changed=False
        for i,side in enumerate(('left','right')):
            row=result['wheels'].get(side,{})
            if row.get('K',0)>0 and row.get('tau',0) and row['tau']>0:
                values[2*i:2*i+2]=[row['K'],row['tau']];changed=True
        if not changed:raise ValueError('没有同时有效的K、τ可复制')
        voltage=sum(v.battery_mv for v in s.samples.values())/len(s.samples)/1000
        model=MotorModel(current.condition,*values,f'辨识{s.info["run"]}',voltage,current.notes,f'实验{s.info["run"]} 100Hz计数窗拟合')
        self._load_model(model);self._model_changed()
    def _redraw(self,*args):self._plot_token=None;self.refresh()
    def _show_guides(self,result,meta):
        for plot,line in self.guides:plot.removeItem(line)
        self.guides=[]
        if not self.guides_enabled.isChecked():return
        def add(plot,pos,label,vertical=False,position=.9):
            line=pg.InfiniteLine(pos=pos,angle=90 if vertical else 0,pen=pg.mkPen('#8b949e',style=__import__('PySide6.QtCore',fromlist=['Qt']).Qt.PenStyle.DashLine),
                                 label=label,labelOpts=dict(position=position))
            plot.addItem(line);self.guides.append((plot,line))
        for plot in self.plots:add(plot,0,'实际切段',True)
        for side,label in (('left','左'),('right','右')):
            row=result.get('wheels',{}).get(side,{})
            if 'v0' not in row:continue
            offset=0 if side=='left' else .08
            for index,(value,name) in enumerate(((row['v0'],'v0'),(row['v_infinity'],'v∞'),(row['v0']+.6321205588*(row['v_infinity']-row['v0']),'63.2%'))):
                add(self.plots[0],value,label+name,position=.12+.16*index+offset)
            if row.get('r1') is not None:
                r=row['r1'];band=.02*abs(r-row['v0'])
                for index,(value,name) in enumerate(((r,'目标'),(r-band,'−2%'),(r+band,'+2%'))):
                    add(self.plots[0],value,label+name,position=.6+.12*index+offset)
            smooth=row.get('smoothed',{})
            if smooth.get('settling_time_s') is not None:add(self.plots[0],smooth['settling_time_s'],label+'平滑ts',True)
            verdict=result.get('comparison',{}).get('wheels',{}).get(side,{})
            interval=verdict.get('settling_time_s_interval')
            if interval:
                for value,name in zip(interval,('ts下界','ts上界')):
                    if value is not None:add(self.plots[0],value,label+name,True)
            if smooth.get('peak_speed_mps') is not None:
                add(self.plots[0],smooth['peak_speed_mps'],label+'平滑峰值',position=.95 if side=='left' else .08)
            else:
                active=[v for v in self.service.samples.values() if v.flags&ACTIVE and v.flags&SECOND]
                if not active:continue
                direction=1 if row['v_infinity']>=row['v0'] else -1
                peak=max(active,key=lambda v:direction*getattr(v,'measured_'+side))
                add(self.plots[0],getattr(peak,'measured_'+side),label+'峰值',position=.95 if side=='left' else .08)
    def refresh(self,*args):
        if self._updating:return
        h,s=self.host,self.service
        ready=bool(h.project_state_fresh and not h.control_owner and not h.io_backlog_exceeded)
        idle=bool(ready and not h.latest.armed and not h.project_request_pending);cap=s.caps;pwm=self.mode.currentIndex()==1
        try:self._model();model_valid=True
        except (ValueError,TypeError):model_valid=False
        execution_issue=self._execution_issue()
        self.buttons['calculate'].setEnabled(model_valid and not pwm)
        self.buttons['preview'].setEnabled(model_valid)
        self.buttons['caps'].setEnabled(idle)
        for key in ('read','info'):self.buttons[key].setEnabled(bool(idle and cap))
        self.buttons['copy'].setEnabled(s.config is not None)
        self.buttons['apply'].setEnabled(bool(idle and cap and cap['algorithm_ready'] and cap.get('target_filter') and not s.download_running and
            (self.entry_method.currentIndex()==0 or not self._design_stale) and model_valid and not pwm))
        self.buttons['arm'].setEnabled(bool(idle and cap and h.latest.local_enable and not s.download_running and
            (pwm or cap['algorithm_ready'] and s.config and not execution_issue) and (not s.info or not s.info['count'] or s.complete)))
        self.buttons['run'].setEnabled(bool(ready and cap and h.latest.armed and h.latest.control_mode==(4 if pwm else 3)
            and not h.project_request_pending and not h.project_motion_active and model_valid and not execution_issue))
        self.buttons['run'].setToolTip(execution_issue)
        self.execution_label.setText(execution_issue or ('开环PWM实验' if pwm else f'执行参数已与RAM确认一致 · revision={s.revision}'))
        self.buttons['stop'].setEnabled(bool(h.connected and h.config.protocol=='PROJECT_V1'))
        self.buttons['download'].setEnabled(bool(idle and s.info and s.meta and s.info['status'] in (3,4) and not s.download_running))
        self.buttons['export'].setEnabled(bool(s.info and s.samples))
        self.buttons['identified_model'].setEnabled(bool(s.complete and s.info['kind']==2))
        self.buttons['save_model'].setEnabled(model_valid)
        self.status_label.setText(('[纯软件假设备] ' if h.source=='SIMULATION_PROJECT_V1' else '')+
            ('离线设计：可编辑模型、计算 PI 与预测；连接设备后执行实验' if not h.connected else
             s.status+(' · STATE过期或控制权被占用' if not ready else ''))+
            (f' · 样本{len(s.samples)}/{s.info["count"]}' if s.info else '')+
            (' · '+self.model_store.warning if self.model_store.warning else ''))
        self.voltage_label.setText('当前电压：'+(f'{h.latest.battery_mv/1000:.3f} V（{"新鲜" if h.project_state_fresh else "过期"}）' if h.latest and h.latest.battery_mv is not None else '等待遥测'))
        self.actual_label.setText(f'实际RAM revision={s.revision}：{s.config.as_dict()}' if s.config else '实际RAM：尚未配置或回读')
        if self.last_request in h.command_results:
            result=h.command_results[self.last_request];self.result_label.setText(result.status+' · '+result.message)
        if self.pause.isChecked():return
        generation=(h.connection_id,s.record_generation)
        if generation!=self._view_generation:
            self._view_generation=generation;self._plot_token=None
            for group in self.lines:
                for line in group:line.clear()
            for line in self.filtered_target_lines:line.clear()
            for plot,line in self.guides:plot.removeItem(line)
            self.guides=[]
            self.metrics.setPlainText(f'当前实验 run={s.active_run}；等待本次记录，不复用上次测量结果' if s.active_run else '等待实验记录')
        samples=[s.samples[i] for i in sorted(s.samples)] if s.samples else [v for run,v in s.live]
        if not samples:return
        token=(h.connection_id,s.record_generation,s.info['run'] if s.info else None,
               s.info['revision'] if s.info else None,len(samples),samples[-1].tick,len(s.samples),
               self.guides_enabled.isChecked(),s.meta['switch_tick'] if s.meta else None)
        if token==self._plot_token:return
        for line in self.estimated_lines:line.clear()
        self._plot_token=token;origin=s.meta['switch_tick'] if s.meta and s.samples else samples[0].tick
        times=[(((v.tick-origin+0x80000000)&0xffffffff)-0x80000000)/1000 for v in samples]
        actual_pwm=bool(s.info and s.samples and s.info['kind']==2) or (not s.samples and h.latest and h.latest.control_mode==4)
        for i,side in enumerate(('left','right')):
            target=[getattr(v,'target_'+side)/1000 for v in samples] if not actual_pwm else []
            for line,ys in zip((self.lines[0][i*3],self.lines[0][i*3+2]),(target,[getattr(v,'measured_'+side) for v in samples])):
                if ys:line.setData(times,ys)
                else:line.clear()
            # 实时与下载记录共用五点平均，指标也调用相同函数；与理论值无关。
            points=smooth_speed(samples,side,origin)
            smooth_times=[p['time_s'] for p in points]
            if points:self.estimated_lines[i].setData(smooth_times,[p['velocity_mps'] for p in points])
            # 原始目标与进入PI的滤波目标分别显示；实测性能仍以原始目标为准。
            if actual_pwm:self.filtered_target_lines[i].clear()
            else:self.filtered_target_lines[i].setData(times,[getattr(v,'filtered_'+side) for v in samples])
            for line,key in zip(self.lines[1][i*2:i*2+2],('raw_','pwm_')):line.setData(times,[getattr(v,key+side) for v in samples])
            if not actual_pwm:self.lines[2][i].setData(smooth_times,[getattr(samples[p['index']],'filtered_'+side)-p['velocity_mps'] for p in points])
            else:self.lines[2][i].clear()
        if s.info and s.meta and s.samples:
            criteria=design_spec_from_context(s.result_context)
            result=analyze(s.info,s.samples,s.caps,s.meta,criteria)
            if s.info['kind']==1 and s.info['config']:
                cfg=SpeedConfig(**s.info['config'])
                for i,side in enumerate(('left','right')):
                    row=result['wheels'].get(side,{})
                    if 'u0' in row:row['actual_step_range']=step_range(cfg,i,row['u0'],row['r0']-row['v0'],row['r0'],target_filter=bool(s.caps.get('target_filter')))
            if s.complete and s.info['kind']==2 and self._last_points_run!=s.info['run']:
                self._last_points_run=s.info['run']
                for side,row in result['wheels'].items():
                    if 'K' not in row:continue
                    key=(self.condition.currentText(),side,self.reference_voltage.text(),1 if row['u_infinity']>0 else -1)
                    points=self._open_points.setdefault(key,[])
                    points.extend([(row['u0'],row['v0']),(row['u_infinity'],row['v_infinity'])]);del points[:-32]
                    self._deadzone_results[(s.info['run'],side)]=rolling_deadzone(points)
            for side,row in result.get('wheels',{}).items():
                if (s.info['run'],side) in self._deadzone_results:
                    row['rolling_deadzone']=self._deadzone_results[(s.info['run'],side)]
            self.metrics.setPlainText(describe_analysis(result)+'\n\n详细数值\n'+
                                     json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
            self._show_guides(result,s.meta)

    def closeEvent(self,event):
        self.model_drafts[self._condition]=(self._raw_model(),self._model_source)
        self.preview_timer.stop()
        super().closeEvent(event)

