/* 直接测试生产PI代码，不访问硬件。 */
#include "../BALANCE/speed_control.h"
#include <assert.h>
#include <float.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static void check_close(double actual, double expected)
{
    assert(fabs(actual - expected) < 1e-9);
}

int main(void)
{
    SpeedControlConfig config = {100.0, 1000.0, 16500};
    SpeedControlConfig bad;
    SpeedControlState state = {0}, saved;
    SpeedControlOutput out;
    double invalid;
    uint64_t nan_bits = UINT64_C(0x7ff8000000000000);
    memcpy(&invalid, &nan_bits, sizeof(invalid));

    /* 未配置或输入非法：报告故障且不产生PWM。 */
    out = SpeedControl_Step(&state, 0.1, 0.0, 0.01);
    assert(out.fault && out.pwm == 0);
    assert(SpeedControl_Configure(&state, &config));
    out = SpeedControl_Step(&state, 0.1, 0.0, 0.01);
    assert(!out.fault && !out.saturated && out.pwm == (int)out.raw_output);
    check_close(out.filtered_target, 0.1 / 11.0);
    check_close(out.delta_output, 1.0); /* 滤波取消目标阶跃的比例突跳，只剩Ki*dt*r。 */

    /* 实际dt只乘一次；限幅前的诊断值保留。 */
    out = SpeedControl_Step(&state, 0.1, 0.0, 0.02);
    check_close(out.filtered_target, (0.1 / 11.0) + (0.1 - 0.1 / 11.0) / 6.0);
    check_close(state.previous_output, 3.0);
    saved = state;
    bad = config; bad.pwm_limit = 16501;
    assert(!SpeedControl_Configure(&state, &bad));
    assert(memcmp(&state, &saved, sizeof(state)) == 0);

    config.kp = 1000000.0; config.ki = 1000.0;
    assert(SpeedControl_Configure(&state, &config));
    check_close(state.previous_output, 0.0);
    out = SpeedControl_Step(&state, 0.1, -1.0, 0.01);
    assert(out.saturated && !out.fault && out.pwm == 16500);
    check_close(out.raw_output, 1000011.0);
    check_close(state.previous_output, 16500.0);
    out = SpeedControl_Step(&state, 0.1, 0.0, 0.01);
    assert(out.saturated && out.pwm == -16500);
    out = SpeedControl_Step(&state, 0.1, 0.0, 0.01);
    assert(!out.saturated && out.pwm == (int)out.raw_output);
    check_close(state.previous_output, -16499.0);

    /* 零目标停车，同时清除误差和输出历史。 */
    out = SpeedControl_Step(&state, 0.0, 0.2, 0.01);
    assert(!out.fault && !out.saturated && out.pwm == 0);
    check_close(state.previous_error, 0.0);
    check_close(state.previous_output, 0.0);
    check_close(state.filtered_target, 0.0);

    out = SpeedControl_Step(&state, invalid, 0.0, 0.01);
    assert(out.fault && out.pwm == 0 && !out.saturated);
    out = SpeedControl_Step(&state, 0.1, 0.0, 0.0);
    assert(out.fault && out.pwm == 0);
    assert(SpeedControl_Configure(&state, &config));
    state.filtered_target = invalid;
    out = SpeedControl_Step(&state, 0.1, 0.0, 0.01);
    assert(out.fault && out.pwm == 0 && state.filtered_target == 0.0);
    out = SpeedControl_Step(&state, 0.1, 0.0, 0.031);
    assert(out.fault && out.pwm == 0);
    out = SpeedControl_Step(&state, DBL_MAX, -DBL_MAX, 0.01);
    assert(out.fault && out.pwm == 0);
    check_close(out.raw_output, 0.0);

    /* 小数增量保留在状态中，整数输出向零截断。 */
    config.kp = 0.0; config.ki = 1000.0;
    assert(SpeedControl_Configure(&state, &config));
    out = SpeedControl_Step(&state, -0.03, 0.0, 0.01);
    assert(out.pwm == 0); check_close(state.previous_output, -0.3);
    SpeedControl_Step(&state, -0.03, 0.0, 0.01);
    SpeedControl_Step(&state, -0.03, 0.0, 0.01);
    out = SpeedControl_Step(&state, -0.03, 0.0, 0.01);
    assert(out.pwm == -1); check_close(state.previous_output, -1.2);
    out = SpeedControl_Step(0, 0.1, 0.0, 0.01);
    assert(out.fault && out.pwm == 0);
    /* 非零切段不复位：与独立I-P递推校核，覆盖变dt、反馈变化和正反向。
       I-P等价关系不是对实现公式的复制，能发现滤波/积分dt多乘或状态丢失。 */
    {
        double reference_output = 0.0, previous_measured = 0.0;
        double targets[] = {0.1, 0.1, 0.2, 0.2, 0.08};
        double measured[] = {0.0, 0.02, 0.04, 0.07, 0.1};
        double periods[] = {0.01, 0.02, 0.01, 0.03, 0.01};
        unsigned i;
        config.kp=100.0; config.ki=1000.0;
        assert(SpeedControl_Configure(&state, &config));
        for(i=0; i<5; ++i) {
            reference_output += -config.kp*(measured[i]-previous_measured)
                              + config.ki*periods[i]*(targets[i]-measured[i]);
            out=SpeedControl_Step(&state,targets[i],measured[i],periods[i]);
            assert(!out.fault && !out.saturated);
            check_close(out.raw_output,reference_output);
            previous_measured=measured[i];
        }
        SpeedControl_Reset(&state);
        out=SpeedControl_Step(&state,-0.1,0.0,0.01);
        check_close(out.raw_output,-1.0);
        check_close(out.filtered_target,-0.1/11.0);
    }
    puts("Production incremental PI + target prefilter: all assertions passed.");
    return 0;
}
