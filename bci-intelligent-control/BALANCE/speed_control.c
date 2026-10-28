#include "speed_control.h"
#include <float.h>

/* 检查有限数，兼容现有Keil工具链。 */
static int SpeedControl_IsFinite(double value)
{
    return value == value && value <= DBL_MAX && value >= -DBL_MAX;
}

static int SpeedControl_ConfigValid(const SpeedControlConfig *config)
{
    return config != 0 &&
           SpeedControl_IsFinite(config->kp) &&
           SpeedControl_IsFinite(config->ki) &&
           config->kp >= 0.0 && config->kp <= 1000000.0 &&
           config->ki > 0.0 && config->ki <= 40000000.0 &&
           config->pwm_limit >= 1 && config->pwm_limit <= SPEED_CONTROL_PWM_MAX;
}

int SpeedControl_Configure(SpeedControlState *s, const SpeedControlConfig *c)
{
    /* 校验失败保留原实例，成功后清除上一轮运行状态。 */
    if (s == 0 || !SpeedControl_ConfigValid(c)) {
        return 0;
    }
    s->config = *c;
    s->configured = 1;
    SpeedControl_Reset(s);
    return 1;
}

void SpeedControl_Reset(SpeedControlState *s)
{
    if (s == 0) {
        return;
    }
    s->previous_error = 0.0;
    s->previous_output = 0.0;
    s->filtered_target = 0.0;
}

SpeedControlOutput SpeedControl_Step(SpeedControlState *s, double r, double v, double dt)
{
    SpeedControlOutput out={0};
    double target, error, delta, raw, limit;
    double integral_step, filter_denominator, filter_alpha;

    // 在计算及浮点转整数前检查
    if (s == 0 || !s->configured || !SpeedControl_ConfigValid(&s->config) ||
        !SpeedControl_IsFinite(r) || !SpeedControl_IsFinite(v) ||
        !SpeedControl_IsFinite(dt) || dt <= 0.0 || dt > 0.030 ||
        !SpeedControl_IsFinite(s->previous_error) ||
        !SpeedControl_IsFinite(s->previous_output) ||
        !SpeedControl_IsFinite(s->filtered_target)) {
        out.fault = 1;
        SpeedControl_Reset(s);
        return out;
    }

    // 零目标则归零复位
    if (r == 0.0) {
        SpeedControl_Reset(s);
        return out;
    }

    /* 目标前置滤波 F(s)=1/(Tf*s+1)，Tf=Kp/Ki，用于抵消参考通道的PI零点。
       后向欧拉：rf += dt/(Tf+dt)*(r-rf)。下面使用等价的Ki形式，
       避免直接计算Kp/Ki时的小Ki溢出；积分增益仍只乘一次实际dt。
       非零目标切段保持滤波/PI状态连续；零目标和STOP已经在前面立即复位。 */
    integral_step = s->config.ki * dt;
    filter_denominator = s->config.kp + integral_step;
    if (!SpeedControl_IsFinite(filter_denominator) || filter_denominator <= 0.0) {
        out.fault = 1;
        SpeedControl_Reset(s);
        return out;
    }
    filter_alpha = integral_step / filter_denominator;
    target = s->filtered_target + filter_alpha * (r - s->filtered_target);

    /* 只对目标做滤波，反馈速度及PWM输出保持原采样和限幅语义。 */
    error = target - v;
    delta = s->config.kp * (error - s->previous_error) + s->config.ki * error * dt;
    raw = s->previous_output + delta;
    limit = (double)s->config.pwm_limit;

    // 故障时保持输出全零
    if (!SpeedControl_IsFinite(target) || !SpeedControl_IsFinite(error) || !SpeedControl_IsFinite(delta) ||
        !SpeedControl_IsFinite(raw)) {
        out.fault = 1;
        SpeedControl_Reset(s);
        return out;
    }

    out.error = error;
    out.delta_output = delta;
    out.raw_output = raw;
    out.filtered_target = target;  // 记录进入PI的滤波目标，原始目标由实验层单独记录

    if (raw > limit) {
        out.saturated = 1;
        raw = limit;
    } else if (raw < -limit) {
        out.saturated = 1;
        raw = -limit;
    }

    // 输出向零截断
    out.pwm = (int)raw;
    s->previous_error = error;
    s->previous_output = raw;
    s->filtered_target = target;

    return out;
}

