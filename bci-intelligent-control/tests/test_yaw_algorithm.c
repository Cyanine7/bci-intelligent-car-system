#include "../BALANCE/yaw_control.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
int main(void)
{
 YawController c;YawConfig cfg={.2,2.,.16,1};YawOutput a,b;
 assert(YawControl_Configure(&c,&cfg));
 a=YawControl_Step(&c,.1,.1,0,.01,0);
 assert(fabs(a.delta-.022)<1e-12 && a.right>a.left);
 b=YawControl_Step(&c,.1,.1,0,.02,0);
 assert(fabs(b.delta-.026)<1e-12); /* Ki*实际dt，不能重复乘周期。 */
 a=YawControl_Step(&c,.1,1,0,.01,0);assert(a.saturated && a.delta==.16);
 b=YawControl_Step(&c,.1,0,.1,.01,0);assert(b.delta<.16 && b.delta!=0);
 a=YawControl_Step(&c,0,.3,0,.01,0);assert(a.left<0 && a.right>0);
 a=YawControl_Step(&c,1.17,1,0,.01,1);assert(a.right<=1.18 && a.left>=-1.18);
 a=YawControl_Step(&c,(double)(float)1.18,1,0,.01,1);assert(!a.fault && a.right<=1.18 && a.delta<1e-6);
 a=YawControl_Step(&c,.1,0,.1,.01,0);assert(a.delta!=0); /* 直行仍纠偏。 */
 a=YawControl_Step(&c,0,0,0,.01,0);assert(a.delta==0 && c.output==0 && c.error==0);
 a=YawControl_Step(&c,.1,.3,0,.031,0);assert(a.fault && c.output==0);
 a=YawControl_Step(&c,.1,NAN,0,.01,0);assert(a.fault);
 puts("PASS: actual yaw PI dt, sign, allocation, antiwindup, straight correction and faults");return 0;
}
