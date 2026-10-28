#include "yaw_control.h"
#include "speed_experiment.h"
#include "project_link.h"
#include "experiment_storage.h"
#include <string.h>
#include <math.h>
#include <float.h>
static YawController outer;
static SpeedControlState inner[2];
static uint8_t negotiated,configured,config[60],frozen_config[60],command[28];
static uint32_t revision,run,run_revision,start_tick,switch_tick,end_tick,phase_tick;
static uint8_t status,reason,kind,segment=255,latest[84],pre[10][84];
static uint16_t count,pre_count,pre_head;
static float run_bias,targets[4];
static uint32_t diag_seq,download_seq;
static uint16_t download_index,download_remaining;
static uint8_t download_part;
static uint8_t calibration;
static uint16_t calibration_count;
static uint32_t calibration_start,calibration_seq;
static double gyro_bias,calibration_sum,calibration_squares;
static uint16_t r16(const uint8_t *p) { return (uint16_t)(p[0]|((uint16_t)p[1]<<8)); }
static uint32_t r32(const uint8_t *p) { return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24); }
static void w16(uint8_t *p,uint16_t n) { p[0]=(uint8_t)n;p[1]=(uint8_t)(n>>8); }
static void w32(uint8_t *p,uint32_t n) { unsigned i;for(i=0;i<4;i++)p[i]=(uint8_t)(n>>(8*i)); }
static float rf(const uint8_t *p) { uint32_t n=r32(p);float x;memcpy(&x,&n,4);return x; }
static void wf(uint8_t *p,float x) { uint32_t n;memcpy(&n,&x,4);w32(p,n); }
static double rd(const uint8_t *p) { uint64_t n=(uint64_t)r32(p)|((uint64_t)r32(p+4)<<32);double x;memcpy(&x,&n,8);return x; }
static void wd(uint8_t *p,double x) { uint64_t n;memcpy(&n,&x,8);w32(p,(uint32_t)n);w32(p+4,(uint32_t)(n>>32)); }
static int implementation_ready(void) { return SPEED_CONTROL_IMPLEMENTED && SPEED_CONTROL_TARGET_FILTER; }
static int finite_value(double x) { return x==x && x<=DBL_MAX && x>=-DBL_MAX; }
int YawExperiment_Negotiated(void) { return negotiated; }
int YawExperiment_Busy(void) { return status==1 || status==2 || download_remaining || calibration==1; }
void YawExperiment_ReleaseCapture(void)
{
 status=reason=kind=0;count=0;run=run_revision=0;segment=255;
 start_tick=switch_tick=end_tick=0;memset(frozen_config,0,60);memset(command,0,28);
}
void YawExperiment_ResetSession(void)
{
 negotiated=0;download_remaining=0;diag_seq=0;pre_count=pre_head=0;
 if(calibration==1)calibration=3;
 YawExperiment_ReleaseCapture();memset(latest,0,84);YawControl_Reset(&outer);
 SpeedControl_Reset(&inner[0]);SpeedControl_Reset(&inner[1]);
}
void YawExperiment_Stop(uint32_t now,uint8_t why)
{
 if(status==1) { status=2;end_tick=now;reason=why; }
 if(calibration==1)calibration=3;
 YawControl_Reset(&outer);SpeedControl_Reset(&inner[0]);SpeedControl_Reset(&inner[1]);
}
int YawExperiment_ArmAllowed(uint32_t now)
{
 float raw;uint32_t seq,tick;
 return negotiated && configured && !YawExperiment_Busy() && calibration==2 &&
 Project_ReadGyro(&raw,&seq,&tick) && finite_value(raw) && (uint32_t)(now-tick)<=60U;
}
static void calibration_state(uint32_t now,uint32_t seq)
{
 uint8_t p[32]={0};float raw=0;uint32_t gs=0,gt=0;int valid=Project_ReadGyro(&raw,&gs,&gt);
 p[0]=calibration;p[1]=(uint8_t)(valid && finite_value(raw) && (uint32_t)(now-gt)<=60U);
 w16(p+2,calibration_count);w32(p+4,gs);w32(p+8,gt);wf(p+12,raw);wf(p+16,(float)gyro_bias);
 wf(p+20,(float)(outer.config.sign*(raw-gyro_bias)));w32(p+24,calibration_start);w32(p+28,now-gt);
 Project_SpeedReply(0x91,seq,p,32);
}
uint8_t YawExperiment_Command(uint8_t type,const uint8_t *p,uint16_t length,uint32_t now,uint32_t seq,int armed)
{
 uint8_t reply[64]={0};unsigned i;SpeedControlState next[2];SpeedControlConfig c;
 YawController candidate;YawConfig yc;
 if(type==23) { if(length)return 1;calibration_state(now,seq);return 0; }
 if(armed || status==1 || status==2 || download_remaining || calibration==1)return 4;
 if(type==19) {
  if(length)return 1;negotiated=1;reply[0]=1;
  reply[1]=(uint8_t)(SPEED_CONTROL_IMPLEMENTED && SPEED_CONTROL_TARGET_FILTER?3:0);
  w16(reply+2,100);w16(reply+4,16500);w16(reply+6,(uint16_t)(YAW_MAX_WHEEL_SPEED*1000+.5));w16(reply+8,YAW_MAX_SEGMENT_MS);
  w16(reply+10,YAW_MAX_TOTAL_MS);w16(reply+12,YAW_CAPTURE_CAPACITY);w16(reply+14,60);
  wd(reply+16,.16);wd(reply+24,1.0);Project_SpeedReply(0x8f,seq,reply,32);return 0;
 }
 if(!negotiated)return 9;
 if(type==20 || type==21) {
  if(type==20 && length)return 1;
  if(type==21) {
   if(length!=60 || p[0]!=1)return 1;
   yc.sign=(int)(int8_t)p[1];yc.kp=rd(p+4);yc.ki=rd(p+12);yc.delta_limit=rd(p+20);
   candidate=outer;if(!YawControl_Configure(&candidate,&yc))return 1;
   c.pwm_limit=r16(p+2);
   for(i=0;i<2;i++) { c.kp=rd(p+28+16*i);c.ki=rd(p+36+16*i);next[i]=inner[i];
    if(!SpeedControl_Configure(&next[i],&c))return 1; }
   if(!implementation_ready())return 7;
   outer=candidate;memcpy(inner,next,sizeof(next));memcpy(config,p,60);revision++;configured=1;
  }
  w32(reply,revision);memcpy(reply+4,config,60);Project_SpeedReply(0x90,seq,reply,64);return 0;
 }
 if(type==22) {
  if(length)return 1;
  calibration=1;calibration_count=0;calibration_sum=calibration_squares=0;calibration_start=now;
  { float raw;uint32_t tick;Project_ReadGyro(&raw,&calibration_seq,&tick); }
  calibration_state(now,seq);return 0;
 }
 if(type==25) {
  if(length)return 1;w32(reply,run);w32(reply+4,run_revision);w32(reply+8,start_tick);w32(reply+12,end_tick);
  w16(reply+16,count);reply[18]=status;reply[19]=reason;reply[20]=kind;reply[21]=segment;
  wf(reply+24,run_bias);Project_SpeedReply(0x92,seq,reply,28);
  w32(reply,run_revision);memcpy(reply+4,frozen_config,60);Project_SpeedReply(0x96,seq,reply,64);return 0;
 }
 if(type==27) {
  if(length)return 1;w32(reply,run);w32(reply+4,start_tick);w32(reply+8,switch_tick);w32(reply+12,end_tick);
  reply[16]=kind;reply[17]=segment;memcpy(reply+20,command,28);Project_SpeedReply(0x93,seq,reply,48);return 0;
 }
 if(type==26) {
  if(length!=8 || !run || r32(p)!=run || status<3 || r16(p+6)<1 || r16(p+6)>8 ||
    (uint32_t)r16(p+4)+r16(p+6)>count)return 1;
  download_seq=seq;download_index=r16(p+4);download_remaining=r16(p+6);download_part=0;return 0;
 }
 return 9;
}
uint8_t YawExperiment_Start(const uint8_t *p,uint16_t length,uint32_t now,uint32_t seq,uint32_t *end)
{
 uint16_t first,total;uint32_t deadline;unsigned i;float values[4];
 if(length!=28 || p[0]!=1 || (p[1]!=1 && p[1]!=2) || r16(p+2))return 1;
 first=r16(p+20);total=r16(p+22);deadline=r32(p+24);
 if(first<10 || first>5000 || total<=first || total-first>5000 || total>10000 || first%10 || total%10)return 1;
 if((int32_t)(deadline-now)<=0 || (uint32_t)(deadline-now)>10000U)return 8;
 if(!YawExperiment_ArmAllowed(now))return 7;
 for(i=0;i<4;i++) { values[i]=rf(p+4+4*i);
  if(!finite_value(values[i]) || fabs(values[i])>(i%2?(p[1]==1?1.0:.6000001):YAW_MAX_WHEEL_SPEED+1e-7))return 1; }
 if(values[0]==values[2] && values[1]==values[3])return 1;
 /* 从此刻起独占共用记录，旧轮速记录明确失效。 */
 SpeedCapture_Reset();run=seq;run_revision=revision;run_bias=(float)gyro_bias;
 memcpy(frozen_config,config,60);memcpy(command,p,28);memcpy(targets,values,sizeof(values));
 phase_tick=now+first;start_tick=switch_tick=end_tick=0;status=1;segment=255;kind=p[1];reason=0;count=0;
 for(i=0;i<pre_count;i++) { uint8_t *s=pre[(pre_head+10-pre_count+i)%10];
  if((uint32_t)(now-r32(s))<=100U) { memcpy(experiment_storage.yaw[count],s,84);w16(experiment_storage.yaw[count]+6,(uint16_t)((r16(s+6)&~(2|512|1024))|1));count++; } }
 *end=now+total;if((int32_t)(deadline-*end)<0)*end=deadline;return 0;
}
int YawExperiment_Tick(uint32_t now,uint32_t dt,int active,int *out_l,int *out_r)
{
 float raw=0,battery,vel[2];uint32_t gs=0,gt=0;int32_t enc[2];uint8_t car,enable;
 int fresh,valid=1;unsigned i;uint16_t flags=0;double measured[2],corrected,v=0,reference=0;
 uint32_t counts,diameter,spacing;YawOutput out={0};SpeedControlOutput wheel;
 fresh=Project_ReadGyro(&raw,&gs,&gt) && finite_value(raw) && (uint32_t)(now-gt)<=60U;
 Project_ReadSensors(&enc[0],&enc[1],&vel[0],&vel[1],&battery,&car,&enable);
 if(calibration==1) {
  if(!fresh || enc[0] || enc[1])calibration=3;
  else if(gs!=calibration_seq) { calibration_seq=gs;calibration_sum+=raw;calibration_squares+=(double)raw*raw;calibration_count++; }
  if(calibration==1 && (uint32_t)(now-calibration_start)>=2000U) {
   if(calibration_count>=80 && calibration_squares/calibration_count-pow(calibration_sum/calibration_count,2)<=.0004) {
    gyro_bias=calibration_sum/calibration_count;calibration=2;
   } else calibration=3;
  }
 }
 memset(latest,0,84);w32(latest,now);w16(latest+4,(uint16_t)dt);w32(latest+8,gt);w32(latest+12,gs);
 corrected=outer.config.sign*(raw-gyro_bias);wf(latest+24,raw);wf(latest+28,(float)corrected);
 if(fresh)flags|=32;if(calibration==2)flags|=64;
 Project_ReadEncoderGeometry(&counts,&diameter,&spacing);
 for(i=0;i<2;i++) { measured[i]=(dt && counts)?enc[i]*(diameter*.000001*3.14159265358979323846)*1000/(counts*(double)dt):0;
  wf(latest+60+4*i,(float)measured[i]); }
 if(active) {
  if(segment==255) { segment=0;start_tick=now; }
  if(segment==0 && (int32_t)(now-phase_tick)>=0) { segment=1;switch_tick=now; }
  if(segment==1)flags|=512;
  v=targets[2*segment];reference=targets[2*segment+1];wf(latest+16,(float)v);wf(latest+20,(float)reference);
  if(!fresh || calibration!=2 || !dt || dt>30)valid=0;
  if(valid) { out=YawControl_Step(&outer,v,reference,corrected,dt*.001,kind==2);if(out.fault)valid=0; }
  wf(latest+32,(float)out.integral);wf(latest+36,(float)out.raw);wf(latest+40,(float)out.delta);
  if(out.saturated)flags|=8;
  for(i=0;i<2 && valid;i++) {
   double target=i?out.right:out.left;wf(latest+44+4*i,(float)target);
   wheel=SpeedControl_Step(&inner[i],target,measured[i],dt*.001);
   if(wheel.fault || !finite_value(wheel.raw_output) || fabs(wheel.raw_output)>FLT_MAX) { valid=0;break; }
   wf(latest+52+4*i,(float)wheel.filtered_target);wf(latest+68+4*i,(float)wheel.raw_output);
   w16(latest+76+2*i,(uint16_t)(int16_t)wheel.pwm);if(wheel.saturated)flags|=(uint16_t)(i?16:4);
  }
  if(!valid) { flags|=128;w16(latest+76,0);w16(latest+78,0);YawExperiment_Stop(now,6); }
  *out_l=valid?(int16_t)r16(latest+76):0;*out_r=valid?(int16_t)r16(latest+78):0;
 }
 if(!dt || dt>30)flags|=256;
 wf(latest+80,0);w16(latest+80,(uint16_t)(battery>=0 && battery<=65.535f?battery*1000:0));
 if(status==1 || status==2) {
  flags|=(uint16_t)(status==1?2:1024);w16(latest+6,flags);
  if(count==YAW_CAPTURE_CAPACITY) { status=4;reason=6;end_tick=now;valid=0;if(active)*out_l=*out_r=0; }
  else { memcpy(experiment_storage.yaw[count++],latest,84);if(status==2 && (uint32_t)(now-end_tick)>=200U)status=3; }
 }
 w16(latest+6,flags);memcpy(pre[pre_head],latest,84);pre_head=(uint16_t)((pre_head+1)%10);if(pre_count<10)pre_count++;
 return valid;
}
static void sample_reply(uint8_t type,uint32_t seq,uint16_t index,uint8_t part,const uint8_t *s)
{
 uint8_t p[56];uint16_t size=(uint16_t)(part?36:48);
 w32(p,run);w16(p+4,index);p[6]=part;p[7]=0;memcpy(p+8,s+(part?48:0),size);
 Project_SpeedReply(type,seq,p,(uint16_t)(size+8));
}
void YawExperiment_Report(void)
{ if(negotiated && r16(latest+4)) { diag_seq++;sample_reply(0x97,diag_seq,count,0,latest);sample_reply(0x98,diag_seq,count,1,latest); } }
void YawExperiment_Pump(void)
{
 if(!download_remaining)return;
 sample_reply((uint8_t)(download_part?0x95:0x94),download_seq,download_index,download_part,experiment_storage.yaw[download_index]);
 if(download_part) { download_index++;download_remaining--; }download_part^=1;
}
