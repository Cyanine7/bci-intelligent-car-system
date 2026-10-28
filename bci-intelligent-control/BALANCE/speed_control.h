#ifndef SPEED_CONTROL_H
#define SPEED_CONTROL_H
#include <stdint.h>

#ifndef SPEED_CONTROL_IMPLEMENTED
#define SPEED_CONTROL_IMPLEMENTED 1
#endif
#define SPEED_CONTROL_PWM_MAX 16500
/* 算法语义标记：本版固定启用目标前置滤波，用于CAPS与产物清单核对。 */
#define SPEED_CONTROL_TARGET_FILTER 1

// 控制器配置：Ki为连续积分增益，计算时仅乘一次实际dt。
typedef struct {
    double kp;
    double ki;
    int pwm_limit;
} SpeedControlConfig;

// 控制器当前状态，左右轮各自持有一个实例。
typedef struct {
    SpeedControlConfig config;
    double previous_error;
    double previous_output;  // 保存限幅后的double输出，保留小数增量
    double filtered_target;  // 目标前置滤波的上一周期状态，Tf=Kp/Ki
    uint8_t configured;
} SpeedControlState;

// 控制器输出，同时供电机执行和实验记录使用。
typedef struct {
    double filtered_target;  // 实际进入PI的滤波目标，区别于通信中的原始目标
    double error;
    double delta_output;
    double raw_output;       // 限幅前的候选输出
    int pwm;
    uint8_t saturated;  // 本周期候选输出超过PWM限幅时为1
    uint8_t fault;      // 配置、输入或计算结果非法时为1，PWM输出为0
} SpeedControlOutput;

int SpeedControl_Configure(SpeedControlState *state, const SpeedControlConfig *config);
void SpeedControl_Reset(SpeedControlState *state);
SpeedControlOutput SpeedControl_Step(
    SpeedControlState *state, double target_mps, double measured_mps, double dt_s);

#endif

