#include "yaw_control.h"
#include <float.h>
#include <string.h>
#include <math.h>
static int finite_value(double x) { return x==x && x<=DBL_MAX && x>=-DBL_MAX; }
void YawControl_Reset(YawController *s) { if(s) { s->error=0;s->output=0; } }
int YawControl_Configure(YawController *s,const YawConfig *c)
{
 if(!s || !c || !finite_value(c->kp) || !finite_value(c->ki) || !finite_value(c->delta_limit) ||
    c->kp<0 || c->kp>100 || c->ki<=0 || c->ki>1000 || c->delta_limit<=0 || c->delta_limit>.6 ||
    (c->sign!=1 && c->sign!=-1))return 0;
 s->config=*c;YawControl_Reset(s);return 1;
}
YawOutput YawControl_Step(YawController *s,double v,double reference,double measured,double dt,int open)
{
 YawOutput o={0};double error,limit,raw;
 if(!s || !finite_value(v) || !finite_value(reference) || !finite_value(measured) ||
    !finite_value(dt) || dt<=0 || dt>.030 || fabs(v)>YAW_MAX_WHEEL_SPEED+1e-7 ||
    !finite_value(s->error) || !finite_value(s->output)) { o.fault=1;YawControl_Reset(s);return o; }
 /* 线格式为binary32，边界值略大于实际值；只收回表示误差，不扩大轮速上限。 */
 if(v>YAW_MAX_WHEEL_SPEED)v=YAW_MAX_WHEEL_SPEED;if(v< -YAW_MAX_WHEEL_SPEED)v= -YAW_MAX_WHEEL_SPEED;
 /* 零角速度且仍在前进时继续纠偏；整车静止目标才立即清状态。 */
 if(v==0 && reference==0) { YawControl_Reset(s);return o; }
 error=reference-measured;
 raw=open?reference:s->output+s->config.kp*(error-s->error)+s->config.ki*error*dt;
 limit=2*(YAW_MAX_WHEEL_SPEED-fabs(v));if(limit>s->config.delta_limit)limit=s->config.delta_limit;
 if(!finite_value(raw)) { o.fault=1;YawControl_Reset(s);return o; }
 o.raw=raw;o.delta=raw;
 if(o.delta>limit) { o.delta=limit;o.saturated=1; }
 if(o.delta< -limit) { o.delta= -limit;o.saturated=1; }
 o.left=v-o.delta/2;o.right=v+o.delta/2;
 /* 回存可执行的差速，连续Ki只乘一次实际dt。积分状态按实际输出重构。 */
 s->error=error;s->output=o.delta;o.integral=open?0:o.delta-s->config.kp*error;
 return o;
}
