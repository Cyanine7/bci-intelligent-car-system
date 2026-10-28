"""角速度实验固定工作页；界面只调用服务，不接触传输。"""
from dataclasses import asdict
import json
import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QPushButton,
    QTabWidget,QScrollArea,QSpinBox,QComboBox,QPlainTextEdit)
import pyqtgraph as pg
from .speed_control_panel import FloatEdit
from .motor_models import MotorModel
from .yaw_models import YawConfig
from .yaw_analysis import design,predict,equivalent_model,stability_margin
from .yaw_metrics import describe_analysis,smooth_samples

class YawPanel(QWidget):
    def __init__(self,host):
        super().__init__();self.host=host;self.service=host.yaw_control
        self.design_context=None;self.predictions=None;self._last_token=None;self._stale=True;self._updating=True
        layout=QVBoxLayout(self);self.status=QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        row=QHBoxLayout();layout.addLayout(row);self.buttons={}
        self.button(row,'caps','查询角速度能力',self.service.query_capabilities)
        self.button(row,'status','读取IMU状态',self.service.read_status)
        self.button(row,'stop','STOP',lambda:host.send_command('stop'))
        self.tabs=QTabWidget();layout.addWidget(self.tabs);forms=[]
        for name in ('模型与内环','外环 PI','阶跃执行','曲线与指标'):
            widget=QWidget();scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(widget)
            self.tabs.addTab(scroll,name);forms.append(QVBoxLayout(widget))
        form=forms[0];form.addWidget(QLabel('负载正向模型；W=0.16m。平均参数用于课程建模，各轮PI分别执行。'))
        self.model_boxes=[];self.inner_boxes=[];model=MotorModel.default('负载');cfg=YawConfig()
        grid=QGridLayout();form.addLayout(grid)
        values=(model.k_left,model.tau_left,model.k_right,model.tau_right,cfg.kp_left,cfg.ki_left,cfg.kp_right,cfg.ki_right)
        labels=('左K (m/s)/PWM','左开环τm (s)','右K (m/s)/PWM','右开环τm (s)','左轮Kp','左轮连续Ki','右轮Kp','右轮连续Ki')
        for i,(label,value) in enumerate(zip(labels,values)):
            box=FloatEdit(value);grid.addWidget(QLabel(label),2*(i//2),i%2);grid.addWidget(box,2*(i//2)+1,i%2)
            (self.model_boxes if i<4 else self.inner_boxes).append(box);box.textChanged.connect(self.dirty)
        self.model_text=QPlainTextEdit();self.model_text.setReadOnly(True);form.addWidget(self.model_text);form.addStretch()
        form=forms[1];grid=QGridLayout();form.addLayout(grid)
        self.tv_source=QComboBox();self.tv_source.addItems(['由模型与内环PI等效','手动二阶 Tv(s)','手动一阶 Tv(s)'])
        self.tv_a=FloatEdit(40.00033235384933);self.tv_b=FloatEdit(839.9067892799715)
        self.tv_tau=FloatEdit(.05982522275724787)
        for i,(label,box) in enumerate((('Tv来源（单位直流增益）',self.tv_source),('一阶等效τv (s)',self.tv_tau),
                                      ('二阶系数a (1/s)',self.tv_a),('二阶系数b (1/s²)',self.tv_b))):
            grid.addWidget(QLabel(label),i//2*2,i%2);grid.addWidget(box,i//2*2+1,i%2)
        self.tv_text=QLabel();self.tv_text.setWordWrap(True);form.addWidget(self.tv_text)
        grid=QGridLayout();form.addLayout(grid)
        self.zeta=FloatEdit(1.5);self.settling=FloatEdit(.7);self.kp=FloatEdit(cfg.kp);self.ki=FloatEdit(cfg.ki);self.delta=FloatEdit(.16)
        self.pwm=QSpinBox();self.pwm.setRange(1,16500);self.pwm.setValue(16500)
        self.sign=QLabel('IMU正值对应左转（固定）')
        for i,(label,box) in enumerate((('设计ζ (>1)',self.zeta),('设计ts (s，2%)',self.settling),('外环Kp (m/rad)',self.kp),
                ('外环连续Ki (m/(rad·s))',self.ki),('差速幅值上限 (m/s)',self.delta),('PWM幅值上限',self.pwm),('陀螺仪方向',self.sign))):
            grid.addWidget(QLabel(label),i//2*2,i%2);grid.addWidget(box,i//2*2+1,i%2)
        for box in (self.zeta,self.settling):box.textChanged.connect(self.dirty)
        for box in (self.tv_a,self.tv_b,self.tv_tau):box.textChanged.connect(self.dirty)
        self.tv_source.currentIndexChanged.connect(self.tv_changed)
        row=QHBoxLayout();form.addLayout(row)
        self.button(row,'calculate','按课程公式计算外环PI（不发送）',self.calculate)
        self.button(row,'preview','按当前草稿刷新预测（含时延）',self.preview)
        row=QHBoxLayout();form.addLayout(row)
        self.button(row,'read','读取内外环RAM',self.service.read_config)
        self.button(row,'copy','RAM复制到草稿',self.copy_config)
        self.button(row,'apply','应用内外环草稿并回读',self.apply)
        self.actual=QLabel('实际RAM：尚未读取');self.actual.setWordWrap(True);form.addWidget(self.actual)
        self.prediction_text=QPlainTextEdit();self.prediction_text.setReadOnly(True);form.addWidget(self.prediction_text)
        form=forms[2];row=QHBoxLayout();form.addLayout(row)
        self.button(row,'calibrate','静止采集陀螺仪零偏（2s）',self.service.calibrate)
        self.imu=QLabel('等待IMU状态');self.imu.setWordWrap(True);form.addWidget(self.imu)
        self.kind=QComboBox();self.kind.addItems(['角速度闭环：目标rad/s','横向开环：目标轮速差m/s'])
        self.motion=QComboBox();self.motion.addItems(['前进转弯','原地转向'])
        form.addWidget(self.kind);form.addWidget(self.motion);self.motion.currentIndexChanged.connect(self.preset)
        self.kind.currentIndexChanged.connect(self.preset)
        grid=QGridLayout();form.addLayout(grid);self.stage_boxes=[]
        for j,label in enumerate(('阶段','线速度 v (m/s)','角速度 / 轮速差目标','时长 (ms，每段≤5000)')):grid.addWidget(QLabel(label),0,j)
        for i in range(2):
            v=FloatEdit(.1);target=FloatEdit(0 if i==0 else .3);duration=QSpinBox();duration.setRange(10,5000);duration.setSingleStep(10);duration.setValue(2000)
            grid.addWidget(QLabel(str(i+1)),i+1,0)
            for j,box in enumerate((v,target,duration),1):grid.addWidget(box,i+1,j)
            v.setText(format(v.value(),'.9g'));target.setText(format(target.value(),'.9g'))
            self.stage_boxes.append((v,target,duration))
        form.addWidget(QLabel('默认2s＋2s；上限5s＋5s。非零切段保持控制状态；STOP取消后续阶段。'))
        row=QHBoxLayout();form.addLayout(row)
        self.button(row,'arm','使能角速度实验',self.arm)
        self.button(row,'run','执行当前两段',self.run)
        self.button(row,'info','读取实验概要',self.service.read_log_info)
        self.button(row,'meta','读取阶段信息',self.service.read_log_meta)
        self.button(row,'download','完整下载并导出',self.service.download)
        self.arm_hint=QLabel();self.arm_hint.setWordWrap(True);form.addWidget(self.arm_hint)
        form.addStretch()
        self.plots=[]
        row=QHBoxLayout();forms[3].addLayout(row)
        self.button(row,'export','保存当前已收到记录（含不完整记录）',self.service.export)
        self.metrics=QPlainTextEdit();self.metrics.setReadOnly(True);self.metrics.setMinimumHeight(230)
        forms[3].addWidget(self.metrics)
        for title,ylabel in (('角速度：五点平滑与目标','rad/s'),('差速：限幅前后、积分','m/s'),
                ('双轮：目标、前置滤波目标与反馈','m/s'),('双轮PWM：限幅前后','PWM')):
            plot=pg.PlotWidget(title=title);plot.setMinimumHeight(400);plot.addLegend(colCount=3);plot.setLabel('bottom','设备时间','s');plot.setLabel('left',ylabel)
            forms[3].addWidget(plot);plot.getAxis('left').enableAutoSIPrefix(False)
            self.plots.append(plot)
        self._updating=False;self.calculate();self.timer=QTimer(self);self.timer.setInterval(200);self.timer.timeout.connect(self.refresh);self.timer.start();self.refresh()
        for box in (self.kp,self.ki,self.delta):box.textChanged.connect(self.prediction_dirty)
        self.pwm.valueChanged.connect(self.prediction_dirty)
        for v,t,d in self.stage_boxes:
            v.textChanged.connect(self.prediction_dirty);t.textChanged.connect(self.prediction_dirty);d.valueChanged.connect(self.prediction_dirty)
        self.kind.currentIndexChanged.connect(self.prediction_dirty)
    def button(self,row,key,label,callback):
        b=QPushButton(label);row.addWidget(b);self.buttons[key]=b
        def invoke():
            try:
                result=callback()
                if hasattr(result,'message'):self.service.status=result.message
            except (ValueError,TypeError,KeyError,OSError,OverflowError) as exc:self.service.status=str(exc)
            self.refresh()
        b.clicked.connect(invoke);return b
    def dirty(self):
        if not self._updating:
            self._stale=True;self.predictions=None;self._last_token=None
    def prediction_dirty(self):
        if not self._updating:
            self.predictions=None;self._last_token=None
    def tv_changed(self):
        self.tv_a.setEnabled(self.tv_source.currentIndex()==1);self.tv_b.setEnabled(self.tv_source.currentIndex()==1)
        self.tv_tau.setEnabled(self.tv_source.currentIndex()==2);self.dirty()
    def tv_override(self):
        if self.tv_source.currentIndex()==1:return dict(order=2,a=self.tv_a.value(),b=self.tv_b.value())
        if self.tv_source.currentIndex()==2:return dict(order=1,tau_v_s=self.tv_tau.value())
        return None
    def prediction_options(self):
        return dict(tv_override=self.tv_override(),feedback_delay_s=.005)
    def show_tv(self,ctx):
        self._updating=True
        if self.tv_source.currentIndex()==0:
            self.tv_a.setValue(ctx['tv_denominator'][1]);self.tv_b.setValue(ctx['tv_denominator'][2])
        if self.tv_source.currentIndex()!=2:self.tv_tau.setValue(ctx['tau_v_s'])
        self._updating=False
        if ctx['tv_order']==2:
            _,a,b=ctx['tv_denominator'];formula=f'{b:.10g} / (s² + {a:.10g} s + {b:.10g})'
        else:formula=f'1 / ({ctx["tau_v_s"]:.10g} s + 1)'
        self.tv_text.setText(f'当前 Tv(s) = {formula}\n来源：{ctx["tv_source"]}；τv={ctx["tau_v_s"]:.10g} s；Gdiffω(s)=Tv(s)/0.16')
        self.tv_changed()
    def model(self):return MotorModel('负载',*[x.value() for x in self.model_boxes],name='角速度实验草稿',source='当前界面冻结模型').validate()
    def config(self):
        return YawConfig(sign=1,pwm_limit=self.pwm.value(),kp=self.kp.value(),ki=self.ki.value(),delta_limit=self.delta.value(),
            **dict(zip(('kp_left','ki_left','kp_right','ki_right'),[x.value() for x in self.inner_boxes]))).validate()
    def segments(self):return [dict(v=v.value(),target=t.value(),duration_ms=d.value()) for v,t,d in self.stage_boxes]
    def preset(self):
        for v,t,d in self.stage_boxes:v.setValue(0 if self.motion.currentIndex() else .1)
        self.stage_boxes[1][1].setValue(.08 if self.kind.currentIndex() else .3)
    def calculate(self):
        cfg,ctx=design(self.model(),self.config(),self.zeta.value(),self.settling.value(),
                       tv_override=self.tv_override())
        self.kp.setValue(cfg.kp);self.ki.setValue(cfg.ki);self.design_context=ctx;self.show_tv(ctx);self._stale=False
        self.model_text.setPlainText(self.tv_text.text()+'\n'+json.dumps(ctx,ensure_ascii=False,indent=2))
        self.prediction_text.setPlainText(f'课程原值：Kp={ctx["course_kp"]:.12g}，Ki={ctx["course_ki"]:.12g}\n当前草稿：Kp={cfg.kp:.12g}，连续Ki={cfg.ki:.12g}\nτv={ctx["tau_v_s"]:.9g}s；ωn={ctx["wn"]:.9g}rad/s\n目标ts是起算指标；以含时延预测及实测比较。')
        self.predictions=None;self._last_token=None
    def preview(self):
        if self._stale:raise ValueError('模型或内环设计已变化，请重新计算外环PI')
        options=self.prediction_options();cfg=self.config()
        self.predictions=predict(cfg,self.model(),self.segments(),self.kind.currentIndex()+1,
                                 gyro_hz=self.service.gyro_rate(),**options)
        ctx=dict(self.design_context,prediction_options=options,
                 executed_draft=asdict(cfg))
        margins=stability_margin(cfg,equivalent_model(self.model(),cfg,options['tv_override']),options['feedback_delay_s'])
        ctx['stability_margin']=margins
        self.prediction_text.setPlainText('当前Tv连续模型裕度：'+json.dumps(margins,ensure_ascii=False)+'\n'+
            '\n'.join(name+'：'+json.dumps(x['metrics'],ensure_ascii=False) for name,x in self.predictions.items()))
        self.service.context=dict(design=ctx,model=self.model().snapshot(),prediction_options=options,
                                  predictions=self.predictions,executed_draft=asdict(cfg))
        self._last_token=None
    def apply(self):
        if self._stale:raise ValueError('模型或内环已变化，请重新计算')
        self.preview();return self.service.apply_config(self.config())
    def copy_config(self):
        cfg=self.service.config
        if cfg is None:raise ValueError('尚未读取有效配置')
        self._updating=True
        for box,value in zip(self.inner_boxes,(cfg.kp_left,cfg.ki_left,cfg.kp_right,cfg.ki_right)):box.setValue(value)
        self.kp.setValue(cfg.kp);self.ki.setValue(cfg.ki);self.delta.setValue(cfg.delta_limit);self.pwm.setValue(cfg.pwm_limit)
        self._updating=False
        self.design_context=equivalent_model(self.model(),cfg,self.tv_override())
        self.design_context.update(zeta=self.zeta.value(),design_ts_s=self.settling.value(),source='RAM复制，保留实际PI')
        self.show_tv(self.design_context);self._stale=False;self.prediction_dirty()
    def match(self):
        try:
            self.prediction_options()
            return not self._stale and self.service.config is not None and self.config().pack()==self.service.config.pack()
        except ValueError:return False
    def arm(self):
        if not self.match():raise ValueError('请先应用当前内外环草稿并确认回读')
        return self.service.arm()
    def run(self):
        if not self.match():raise ValueError('请先应用当前内外环草稿并确认回读')
        # 冻结执行时最新Tv及预测时延；不沿用应用参数前的旧预测上下文。
        self.preview()
        return self.service.run_step(self.segments(),self.kind.currentIndex()+1,self.config())
    def refresh(self):
        s=self.service;h=self.host;fresh=h.project_state_fresh;idle=fresh and not h.latest.armed and not h.project_motion_active and not h.project_request_pending
        self.status.setText(s.status+('；模型已改，待重算' if self._stale else ''))
        self.actual.setText('实际RAM：'+(f'rev={s.revision}；'+json.dumps(asdict(s.config),ensure_ascii=False) if s.config else '尚未读取有效配置'))
        if s.imu:
            names=('未校准','采集中','校准完成','校准中断或失败');im=s.imu
            self.imu.setText(f'IMU：{names[im["calibration"]]}；'+('数据有效' if im['valid'] else '数据过期或不可用')+
                f'；零偏 {im["bias"]:.6g} rad/s；校正角速度 {im["omega"]:.6g} rad/s；样本 {im["count"]}；数据年龄 {im["age_ms"]} ms')
        else:self.imu.setText('IMU：尚未读取；采集零偏后自动更新校准状态')
        self.buttons['caps'].setEnabled(bool(idle and not s.download_running));self.buttons['stop'].setEnabled(h.connected)
        self.buttons['status'].setEnabled(bool(fresh and s.caps and not h.project_request_pending))
        for key in ('read','calibrate','info','meta'):self.buttons[key].setEnabled(bool(idle and s.caps and not s.result_fetching))
        self.buttons['apply'].setEnabled(bool(idle and s.caps and s.caps['ready'] and not self._stale and not s.result_fetching))
        self.buttons['copy'].setEnabled(s.config is not None)
        busy=bool((h.latest and h.latest.armed) or h.project_motion_active or s.result_fetching)
        editors=[*self.model_boxes,*self.inner_boxes,self.zeta,self.settling,self.kp,self.ki,self.delta,self.pwm,self.sign,
                 self.tv_source,self.kind,self.motion,
                 *[box for stage in self.stage_boxes for box in stage]]
        for box in editors:box.setEnabled(not busy)
        self.tv_a.setEnabled(not busy and self.tv_source.currentIndex()==1)
        self.tv_b.setEnabled(not busy and self.tv_source.currentIndex()==1)
        self.tv_tau.setEnabled(not busy and self.tv_source.currentIndex()==2)
        for key in ('calculate','preview'):self.buttons[key].setEnabled(not busy)
        self.buttons['copy'].setEnabled(not busy and s.config is not None)
        # 与服务共用IMU就绪判据，避免按钮亮着却因同一份状态过期被拒绝。
        if not idle:arm_reason='等待设备失能、新鲜STATE及在途请求结束'
        elif not s.caps or not s.caps['ready']:arm_reason='请先查询并确认角速度实验能力'
        elif not self.match():arm_reason='请先计算、应用内外环草稿并确认RAM回读'
        elif not h.latest.local_enable:arm_reason='设备本地禁止使能'
        elif s.result_fetching:arm_reason='请等待本次实验记录读取及指标计算完成'
        else:arm_reason=s.imu_unavailable_reason
        self.buttons['arm'].setEnabled(not arm_reason)
        self.buttons['arm'].setToolTip(arm_reason or '参数、零偏与IMU新鲜度已确认，点击使能')
        self.arm_hint.setText('使能条件：'+(arm_reason or '已就绪'))
        self.buttons['run'].setEnabled(bool(fresh and self.match() and h.latest.armed and h.latest.control_mode==5 and not h.project_motion_active and not h.project_request_pending))
        self.buttons['download'].setEnabled(bool(idle and s.info and s.meta and s.info['status'] in (3,4) and not s.result_fetching))
        self.buttons['export'].setEnabled(bool(s.info and s.meta and s.samples and not s.download_running))
        if s.caps:
            for v,t,d in self.stage_boxes:d.setMaximum(min(5000,s.caps['max_segment_ms']))
        data=[x for i,x in sorted(s.samples.items())] if s.samples else list(s.live)
        token=(h.connection_id,s.active_run,len(data),data[-1].tick if data else None,id(self.predictions),id(s.predictions),
               len(s.verified_indices),len(s.integrity_errors),id(s.info),id(s.meta),s.result_fetching)
        if token==self._last_token:return
        self._last_token=token
        if s.result_fetching:
            text=(f'本次实验 run={s.active_run} 正在运行；结束后自动读取100Hz记录并计算超调、静差、ts和tr。'
                  if h.latest and h.latest.armed else
                  f'本次实验 run={s.active_run} 已结束，正在读取记录并计算指标。\n已确认样本：{len(s.verified_indices)} / '+
                  (str(s.info['count']) if s.info else '等待概要'))
        else:
            text=describe_analysis(s.analysis())
            if s.active_run and not s.samples:text=f'本次实验 run={s.active_run}，尚未取得完整结果。\n{s.status}\n可手动读取概要、阶段和记录。'
        self.metrics.setPlainText(text)
        for plot in self.plots:plot.clear()
        if not data:return
        anchor=s.meta['start'] if s.meta else data[0].tick
        times=np.array([((x.tick-anchor+0x80000000)&0xffffffff)-0x80000000 for x in data])/1000
        def line(plot,field,color,label):plot.plot(times,[getattr(x,field) for x in data],name=label,pen=pg.mkPen(color,width=1.5))
        target=[x.target if not s.meta or s.meta['kind']==1 else x.target/.16 for x in data]
        self.plots[0].plot(times,target,name='角速度目标 / 开环几何值',pen='y')
        # 图与超调/tr/ts共享同一五点平均，跨正常切段连续，缺点或失效时清窗。
        smoothed=smooth_samples(data,anchor)
        self.plots[0].plot([p[1] for p in smoothed],[p[2] for p in smoothed],name='五点平滑',pen=pg.mkPen('g',width=2))
        for field,color,label in (('raw_delta','y','限幅前差速'),('delta','c','执行差速'),('integral','m','积分状态')):line(self.plots[1],field,color,label)
        for field,color,label in (('left_ref','y','左目标'),('right_ref','m','右目标'),('left_filtered','w','左滤波目标'),('right_filtered','b','右滤波目标'),('left','c','左反馈'),('right','g','右反馈')):line(self.plots[2],field,color,label)
        for field,color,label in (('pwm_left','c','左PWM'),('pwm_right','g','右PWM'),('raw_left','y','左限幅前'),('raw_right','m','右限幅前')):line(self.plots[3],field,color,label)
