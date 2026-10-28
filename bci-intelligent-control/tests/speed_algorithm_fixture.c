/* TEST DOUBLE ONLY. Deliberately no P/PI algorithm; production file is untouched. */
#include "../BALANCE/speed_control.h"
#include <string.h>
static int output_override;
__declspec(dllexport) void test_algorithm_output(int pwm) { output_override=pwm; }
int SpeedControl_Configure(SpeedControlState *s,const SpeedControlConfig *c)
{
    s->config=*c;s->configured=1;SpeedControl_Reset(s);return 1;
}
void SpeedControl_Reset(SpeedControlState *s)
{ s->previous_error=s->previous_output=s->filtered_target=0; }
SpeedControlOutput SpeedControl_Step(SpeedControlState *s,double target,double measured,double dt)
{
    SpeedControlOutput out={0};(void)s;(void)measured;(void)dt;
    out.filtered_target=target;out.pwm=target?output_override:0;out.raw_output=out.pwm;
    return out;
}

