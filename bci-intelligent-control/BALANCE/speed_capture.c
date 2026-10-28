#include "speed_capture.h"
#include "experiment_storage.h"
#include <string.h>
ExperimentStorage experiment_storage;
#define samples experiment_storage.speed
static SpeedSample pre[10];
static uint16_t count,pre_count,pre_head;
static uint8_t status,reason,kind,segment=255,snapshot[36],command[20];
static uint32_t run,revision,start_tick,switch_tick,end_tick;
static void w16(uint8_t *p,uint16_t v) { p[0]=(uint8_t)v;p[1]=(uint8_t)(v>>8); }
static void w32(uint8_t *p,uint32_t v) { unsigned i;for(i=0;i<4;i++)p[i]=(uint8_t)(v>>(8*i)); }
static void wf(uint8_t *p,float v) { uint32_t n;memcpy(&n,&v,4);w32(p,n); }
void SpeedSample_Encode(uint8_t *p,const SpeedSample *s)
{
 unsigned i;w32(p,s->tick);w16(p+4,s->dt);w16(p+6,s->flags);
 for(i=0;i<2;i++) {
  w16(p+8+2*i,(uint16_t)s->target[i]);
  wf(p+12+4*i,s->filtered[i]);wf(p+20+4*i,s->measured[i]);wf(p+28+4*i,s->raw[i]);
  w16(p+36+2*i,(uint16_t)s->pwm[i]);w32(p+44+4*i,(uint32_t)s->encoder[i]);
 }
 w16(p+40,s->battery);w16(p+42,0);
}
void SpeedCapture_Reset(void)
{
 count=pre_count=pre_head=0;status=reason=kind=0;segment=255;
 run=revision=start_tick=switch_tick=end_tick=0;
 memset(snapshot,0,36);memset(command,0,20);
}
void SpeedCapture_Begin(uint32_t seq,uint32_t rev,uint32_t now,uint8_t type,const uint8_t cfg[36],const uint8_t cmd[20])
{
 unsigned i;count=0;run=seq;revision=rev;
 start_tick=switch_tick=end_tick=0;segment=255;kind=type;status=1;reason=0;
 memcpy(snapshot,cfg,36);memcpy(command,cmd,20);
 for(i=0;i<pre_count;i++) {
  SpeedSample s=pre[(pre_head+10-pre_count+i)%10];
  if((uint32_t)(now-s.tick)<=100U) { s.flags=SPEED_PRE;samples[count++]=s; }
 }
}
void SpeedCapture_Segment(uint8_t index,uint32_t now)
{
 if(status!=1 || index==segment)return;
 if(segment==255)start_tick=now;
 if(index==1)switch_tick=now;
 segment=index;
}
void SpeedCapture_Stop(uint32_t now,uint8_t why)
{ if(status==1) { status=2;end_tick=now;reason=why; } }
void SpeedCapture_Push(const SpeedSample *s)
{
 if(status==1 || status==2) {
  if(count==SPEED_CAPTURE_CAPACITY) { status=4;end_tick=s->tick;reason=6; }
  else {
   samples[count]=*s;samples[count++].flags|=(status==1?SPEED_ACTIVE:SPEED_POST);
   if(status==2 && (uint32_t)(s->tick-end_tick)>=200U)status=3;
  }
 }
 pre[pre_head]=*s;pre_head=(uint16_t)((pre_head+1)%10);if(pre_count<10)pre_count++;
}
uint8_t SpeedCapture_Status(void) { return status; }
uint16_t SpeedCapture_Count(void) { return count; }
uint32_t SpeedCapture_Run(void) { return run; }
void SpeedCapture_Info(uint8_t *p)
{
 w32(p,run);w32(p+4,revision);w32(p+8,start_tick);w32(p+12,end_tick);
 w16(p+16,count);p[18]=status;p[19]=reason;p[20]=kind;p[21]=segment;w16(p+22,0);
 memcpy(p+24,snapshot,36);
}
void SpeedCapture_Meta(uint8_t *p)
{
 w32(p,run);w32(p+4,start_tick);w32(p+8,switch_tick);w32(p+12,end_tick);
 p[16]=kind;p[17]=segment;w16(p+18,0);memcpy(p+20,command,20);
}
int SpeedCapture_Read(uint16_t i,SpeedSample *s)
{ if(i>=count)return 0;*s=samples[i];return 1; }

