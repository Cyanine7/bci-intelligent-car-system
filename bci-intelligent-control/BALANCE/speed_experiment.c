#include "speed_experiment.h"
#include "speed_control.h"
#include "yaw_control.h"
#include "project_link.h"
#include <string.h>
#include <float.h>
typedef char require_binary64[(sizeof(double)==8 && DBL_MANT_DIG==53)?1:-1];
typedef char require_binary32[(sizeof(float)==4 && FLT_MANT_DIG==24)?1:-1];
static uint8_t negotiated,configured,wire_config[36],run_kind;
static uint32_t revision,diag_seq,download_seq,run_switch;
static uint16_t download_index,download_remaining;
static int run_targets[4];
static SpeedControlState controllers[2];
static SpeedSample latest;
static uint16_t r16(const uint8_t *p) { return (uint16_t)(p[0]|((uint16_t)p[1]<<8)); }
static uint32_t r32(const uint8_t *p) { return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24); }
static void w16(uint8_t *p,uint16_t n) { p[0]=(uint8_t)n;p[1]=(uint8_t)(n>>8); }
static void w32(uint8_t *p,uint32_t n) { unsigned i;for(i=0;i<4;i++)p[i]=(uint8_t)(n>>(8*i)); }
static double rd(const uint8_t *p) { uint64_t n=(uint64_t)r32(p)|((uint64_t)r32(p+4)<<32);double x;memcpy(&x,&n,8);return x; }
static void wd(uint8_t *p,double x) { uint64_t n;memcpy(&n,&x,8);w32(p,(uint32_t)n);w32(p+4,(uint32_t)(n>>32)); }
static int finite_double(double x) { return x==x && x<=DBL_MAX && x>=-DBL_MAX; }
static int implementation_ready(void) { return SPEED_CONTROL_IMPLEMENTED; }
int SpeedExperiment_Negotiated(void) { return negotiated; }
int SpeedExperiment_Busy(void) { return SpeedCapture_Status()==1 || SpeedCapture_Status()==2 || download_remaining; }
void SpeedExperiment_ResetSession(void)
{
 negotiated=0;diag_seq=0;download_remaining=0;run_kind=0;
 SpeedCapture_Reset();memset(&latest,0,sizeof(latest));
 SpeedControl_Reset(&controllers[0]);SpeedControl_Reset(&controllers[1]);
}
void SpeedExperiment_Stop(uint32_t now,uint8_t reason)
{
 SpeedCapture_Stop(now,reason);SpeedControl_Reset(&controllers[0]);SpeedControl_Reset(&controllers[1]);
}
int SpeedExperiment_ArmAllowed(uint8_t mode)
{ return negotiated && !SpeedExperiment_Busy() && (mode==4 || (implementation_ready() && configured)); }
uint8_t SpeedExperiment_Command(uint8_t type,const uint8_t *p,uint16_t length,uint32_t now,uint32_t seq,int armed)
{
 uint8_t reply[60];uint32_t counts,diameter,spacing;unsigned i;
 SpeedControlState next[2];SpeedControlConfig cfg;(void)now;
 if(armed || SpeedExperiment_Busy())return 4;
 if(type==11) {
  if(length)return 1;
  negotiated=1;Project_ReadEncoderGeometry(&counts,&diameter,&spacing);
  /* flags bit0=算法已启用，bit1=目标前置滤波；旧固件只有bit0。
     上位机必须读取实际能力，不能把未滤波固件当成本版算法执行。 */
  reply[0]=2;reply[1]=(uint8_t)(implementation_ready()?(1|(SPEED_CONTROL_TARGET_FILTER<<1)):0);
  w16(reply+2,100);w16(reply+4,16500);w16(reply+6,PROJECT_SPEED_LIMIT);w16(reply+8,3000);
  w16(reply+10,SPEED_CAPTURE_CAPACITY);w32(reply+12,counts);w32(reply+16,diameter);
  wd(reply+20,1000000.0);wd(reply+28,40000000.0);w16(reply+36,100);w16(reply+38,200);
  Project_SpeedReply(0x89,seq,reply,40);return 0;
 }
 if(!negotiated)return 9;
 if(type==12 || type==13) {
  if(type==12 && length)return 1;
  if(type==13) {
   /* 保留36字节协议布局，算法字段固定为2（PI），其他值明确拒绝。 */
   if(length!=36 || p[0]!=2 || p[1] || r16(p+2)<1 || r16(p+2)>16500)return 1;
   cfg.pwm_limit=r16(p+2);
   for(i=0;i<2;i++) {
    cfg.kp=rd(p+4+16*i);cfg.ki=rd(p+12+16*i);
    if(!finite_double(cfg.kp) || !finite_double(cfg.ki) || cfg.kp<0 || cfg.kp>1000000 ||
       cfg.ki<=0 || cfg.ki>40000000)return 1;
    next[i]=controllers[i];
    if(!implementation_ready() || !SpeedControl_Configure(&next[i],&cfg))return 7;
   }
   memcpy(controllers,next,sizeof(next));memcpy(wire_config,p,36);revision++;configured=1;
  }
  w32(reply,revision);memcpy(reply+4,wire_config,36);Project_SpeedReply(0x8a,seq,reply,40);return 0;
 }
 if(type==15) { if(length)return 1;SpeedCapture_Info(reply);Project_SpeedReply(0x8c,seq,reply,60);return 0; }
 if(type==18) { if(length)return 1;SpeedCapture_Meta(reply);Project_SpeedReply(0x8e,seq,reply,40);return 0; }
 if(type==16) {
  if(length!=8 || r32(p)!=SpeedCapture_Run() || !SpeedCapture_Run() || SpeedCapture_Status()<3 ||
     r16(p+6)<1 || r16(p+6)>8 || (uint32_t)r16(p+4)+r16(p+6)>SpeedCapture_Count())return 1;
  download_seq=seq;download_index=r16(p+4);download_remaining=r16(p+6);return 0;
 }
 return 9;
}
uint8_t SpeedExperiment_Start(uint8_t type,const uint8_t *p,uint16_t length,uint32_t now,uint32_t seq,uint32_t *end)
{
 unsigned i;int targets[4];uint16_t first,total;uint32_t deadline;uint8_t empty[36]={0};
 if(length!=20 || p[0]!=2 || p[1]!=2 || r16(p+2))return 1;
 first=r16(p+12);total=r16(p+14);deadline=r32(p+16);
 if(first<10 || total<=first || total>3000 || first%10 || total%10)return 1;
 if((int32_t)(deadline-now)<=0 || (uint32_t)(deadline-now)>3000U)return 8;
 for(i=0;i<4;i++) {
  targets[i]=(int16_t)r16(p+4+2*i);
  if(targets[i]>(type==17?16500:PROJECT_SPEED_LIMIT) || targets[i]< -(type==17?16500:PROJECT_SPEED_LIMIT))return 1;
 }
 for(i=0;i<2;i++)if((targets[i]<0 && targets[i+2]>0) || (targets[i]>0 && targets[i+2]<0))return 1;
 if(targets[0]==targets[2] && targets[1]==targets[3])return 1;
 *end=now+total;if((int32_t)(deadline-*end)<0)*end=deadline;
 YawExperiment_ReleaseCapture();
 run_kind=(uint8_t)(type==17?2:1);run_switch=now+first;memcpy(run_targets,targets,sizeof(targets));
 SpeedCapture_Begin(seq,revision,now,run_kind,type==17?empty:wire_config,p);return 0;
}
int SpeedExperiment_Tick(uint32_t now,uint32_t dt,int active,int *out_l,int *out_r)
{
 SpeedControlOutput result;float unused[2],battery;uint8_t car,enable;unsigned i;
 uint32_t counts,diameter,spacing;double measured[2];int valid=1,target;uint8_t segment;
 memset(&latest,0,sizeof(latest));latest.tick=now;latest.dt=(uint16_t)(dt>65535?65535:dt);
 Project_ReadSensors(&latest.encoder[0],&latest.encoder[1],&unused[0],&unused[1],&battery,&car,&enable);
 Project_ReadEncoderGeometry(&counts,&diameter,&spacing);
 latest.battery=(uint16_t)(finite_double(battery)&&battery>=0 && battery<=65.535f?battery*1000:0);
 if(!dt || dt>30 || !counts || !diameter)latest.flags|=SPEED_BAD_DT;
 segment=(uint8_t)((int32_t)(now-run_switch)>=0);
 if(active) { SpeedCapture_Segment(segment,now);if(segment)latest.flags|=SPEED_SECOND; }
 for(i=0;i<2;i++) {
  measured[i]=(dt && counts)?latest.encoder[i]*(diameter*0.000001*3.14159265358979323846)*1000.0/(counts*(double)dt):0;
  latest.measured[i]=(float)measured[i];if(!active)continue;
  target=run_targets[2*segment+i];latest.target[i]=(int16_t)target;
  if(run_kind==2) { latest.raw[i]=(float)target;latest.pwm[i]=(int16_t)target;continue; }
  result=SpeedControl_Step(&controllers[i],target/1000.0,measured[i],dt/1000.0);
  if(latest.flags&SPEED_BAD_DT || result.fault || !finite_double(result.raw_output) ||
     result.raw_output>FLT_MAX || result.raw_output< -FLT_MAX ||
     !finite_double(result.filtered_target) || !finite_double(result.error) || !finite_double(result.delta_output) ||
     result.pwm>controllers[i].config.pwm_limit || result.pwm< -controllers[i].config.pwm_limit ||
     result.pwm>16500 || result.pwm< -16500 || (!target && result.pwm)) { valid=0;latest.flags|=SPEED_FAULT; }
  else {
   latest.filtered[i]=(float)result.filtered_target;latest.raw[i]=(float)result.raw_output;latest.pwm[i]=(int16_t)result.pwm;
   if(result.saturated)latest.flags|=(i?SPEED_SAT_RIGHT:SPEED_SAT_LEFT);
  }
 }
 if(active && latest.flags&SPEED_BAD_DT)valid=0;
 if(!valid) { latest.pwm[0]=latest.pwm[1]=0;latest.flags|=SPEED_FAULT;SpeedCapture_Stop(now,6); }
 SpeedCapture_Push(&latest);if(active && SpeedCapture_Status()==4)valid=0;
 if(active) { *out_l=valid?latest.pwm[0]:0;*out_r=valid?latest.pwm[1]:0; }return valid;
}
void SpeedExperiment_Report(void)
{
 uint8_t p[56];if(!negotiated || !latest.dt)return;
 w32(p,SpeedCapture_Run());SpeedSample_Encode(p+4,&latest);Project_SpeedReply(0x8b,++diag_seq,p,56);
}
void SpeedExperiment_Pump(void)
{
 uint8_t p[60];SpeedSample sample;if(!download_remaining)return;
 if(!SpeedCapture_Read(download_index,&sample)) { download_remaining=0;return; }
 w32(p,SpeedCapture_Run());w16(p+4,download_index);w16(p+6,0);
 SpeedSample_Encode(p+8,&sample);Project_SpeedReply(0x8d,download_seq,p,60);download_index++;download_remaining--;
}

