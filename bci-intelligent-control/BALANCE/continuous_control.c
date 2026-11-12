#include "continuous_control.h"
#include "yaw_control.h"
#include "speed_experiment.h"
#include "project_link.h"
#include <string.h>
#include <math.h>
#include <float.h>

static uint8_t negotiated,latest[52];
static uint32_t revision,accepted,deadline,report_seq;
static double requested[2],current[2],bias;
static YawController outer;
static SpeedControlState inner[2];
static uint32_t r32(const uint8_t *p) { return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24); }
static void w16(uint8_t *p,uint16_t x) { p[0]=(uint8_t)x;p[1]=(uint8_t)(x>>8); }
static void w32(uint8_t *p,uint32_t x) { unsigned i;for(i=0;i<4;i++)p[i]=(uint8_t)(x>>(8*i)); }
static float rf(const uint8_t *p) { uint32_t n=r32(p);float x;memcpy(&x,&n,4);return x; }
static void wf(uint8_t *p,float x) { uint32_t n;memcpy(&n,&x,4);w32(p,n); }
static int finite_value(double x) { return x==x && x<=DBL_MAX && x>=-DBL_MAX; }
void Continuous_Stop(void)
{
 requested[0]=requested[1]=current[0]=current[1]=0;deadline=0;
 YawControl_Reset(&outer);SpeedControl_Reset(&inner[0]);SpeedControl_Reset(&inner[1]);
 memset(latest+16,0,8);memset(latest+24,0,8);memset(latest+36,0,12);
}
void Continuous_ResetSession(void)
{ Continuous_Stop();negotiated=0;revision=accepted=report_seq=0;memset(latest,0,sizeof(latest)); }
int Continuous_Arm(uint32_t now)
{
 YawConfig yc;SpeedControlConfig wheels[2];unsigned i;
 if(!negotiated || !YawExperiment_ControlSnapshot(now,&yc,wheels,&bias,&revision))return 0;
 if(!YawControl_Configure(&outer,&yc))return 0;
 for(i=0;i<2;i++)if(!SpeedControl_Configure(&inner[i],&wheels[i]))return 0;
 Continuous_Stop();return 1;
}
uint8_t Continuous_Command(uint8_t type,const uint8_t *p,uint16_t n,uint32_t now,uint32_t seq,int enabled,uint32_t *end)
{
 uint8_t reply[24]={0};uint32_t until;float v,w;
 if(type==28) {
  if(n)return 1;if(enabled || SpeedExperiment_Busy() || YawExperiment_Busy())return 4;
  negotiated=1;reply[0]=2;reply[1]=(uint8_t)(SPEED_CONTROL_IMPLEMENTED && SPEED_CONTROL_TARGET_FILTER?7:0);
  w16(reply+2,100);w16(reply+4,20);w16(reply+6,CONTINUOUS_LEASE_MS);
  wf(reply+8,(float)YAW_MAX_WHEEL_SPEED);wf(reply+12,(float)CONTINUOUS_OMEGA_LIMIT);wf(reply+16,.5f);wf(reply+20,2);
  Project_SpeedReply(0x99,seq,reply,24);return 0;
 }
 if(type!=29 || !negotiated)return 9;
 if(n!=20 || p[0]!=2 || (p[1]&~1U) || p[2] || p[3])return 1;
 if(!enabled)return 4;if(r32(p+4)!=revision)return 7;
 until=r32(p+8);if((int32_t)(until-now)<=0 || (uint32_t)(until-now)>CONTINUOUS_LEASE_MS)return 8;
 v=rf(p+12);w=rf(p+16);
 if(!finite_value(v) || !finite_value(w) || fabs(v)>YAW_MAX_WHEEL_SPEED+1e-7 || fabs(w)>CONTINUOUS_OMEGA_LIMIT+1e-7)return 1;
 requested[0]=v;requested[1]=w;accepted=seq;deadline=until;*end=until;
 /* v2普通零目标平滑过渡；flags bit0仅用于保留旧按住模式的松键语义。 */
 if(p[1]&1U) {
  if(v==0)current[0]=0;if(w==0)current[1]=0;
  if(v==0 && w==0) { YawControl_Reset(&outer);SpeedControl_Reset(&inner[0]);SpeedControl_Reset(&inner[1]); }
 }
 return 0;
}
static double approach(double x,double target,double step)
{
 double d=target-x;if(d>step)d=step;if(d< -step)d= -step;return x+d;
}
int Continuous_Tick(uint32_t now,uint32_t dt,int active,int *left,int *right)
{
 float gyro=0,vel[2],battery;uint32_t gs=0,gt=0,counts,diameter,spacing;
 int32_t enc[2];uint8_t car,enable;int fresh;unsigned i;double measured[2],omega;
 uint16_t flags=0;YawOutput output={0};SpeedControlOutput wheel;
 fresh=Project_ReadGyro(&gyro,&gs,&gt) && finite_value(gyro) && (uint32_t)(now-gt)<=60U;
 omega=outer.config.sign*(gyro-bias);if(!finite_value(omega))omega=0;
 memset(latest,0,sizeof(latest));w32(latest,now);w32(latest+4,accepted);w32(latest+8,revision);w32(latest+12,deadline);
 if(fresh)flags|=2;wf(latest+32,(float)omega);
 w16(latest+48,(uint16_t)((uint32_t)(now-gt)>65535U?65535U:now-gt));
 if(active) {
  if(!fresh || !dt || dt>30 || (int32_t)(deadline-now)<=0) { *left=*right=0;return 0; }
  flags|=1;
  for(i=0;i<2;i++) {
   current[i]=approach(current[i],requested[i],(i?2:.5)*dt*.001);
   wf(latest+16+4*i,(float)requested[i]);wf(latest+24+4*i,(float)current[i]);
  }
  Project_ReadSensors(&enc[0],&enc[1],&vel[0],&vel[1],&battery,&car,&enable);
  Project_ReadEncoderGeometry(&counts,&diameter,&spacing);if(!counts) { *left=*right=0;return 0; }
  for(i=0;i<2;i++)measured[i]=enc[i]*(diameter*.000001*3.14159265358979323846)*1000/(counts*(double)dt);
  output=YawControl_Step(&outer,current[0],current[1],omega,dt*.001,0);
  if(output.fault) { *left=*right=0;return 0; }if(output.saturated)flags|=4;
  for(i=0;i<2;i++) {
   double target=i?output.right:output.left;wf(latest+36+4*i,(float)target);
   wheel=SpeedControl_Step(&inner[i],target,measured[i],dt*.001);
   if(wheel.fault) { *left=*right=0;return 0; }
   if(wheel.saturated)flags|=(uint16_t)(i?16:8);
   if(i)*right=wheel.pwm;else *left=wheel.pwm;w16(latest+44+2*i,(uint16_t)(int16_t)wheel.pwm);
  }
 }
 w16(latest+50,flags);return 1;
}
void Continuous_Report(void)
{ if(negotiated)Project_SpeedReply(0x9a,++report_seq,latest,sizeof(latest)); }
