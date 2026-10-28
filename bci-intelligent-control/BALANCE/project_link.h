#ifndef PROJECT_LINK_H
#define PROJECT_LINK_H
#include <stdint.h>

#define PROJECT_PWM_LIMIT 10000
#define PROJECT_PI_PWM_LIMIT 6000
/* 单轮轮速上限按负载模型 K≈7.21e-5、PWM 16500 折算约1.19m/s，取1.18留模型误差余量。 */
#define PROJECT_SPEED_LIMIT 1180
#define PROJECT_MAX_DURATION 4000
#define PROJECT_SPEED_MAX_DURATION 1000
#define PROJECT_PROFILE_VERSION 1
#define PROJECT_PROFILE_SEGMENTS 2

/* One producer (USART2 IRQ), one consumer (100 Hz control task). */
void Project_RxByte(uint8_t byte);
void Project_RxError(void);
int Project_TxByte(uint8_t *byte);
void Project_ControlTick(uint32_t now_ms, int local_allowed);
void Project_GetOutput(int *left, int *right);
void Project_Stop(uint8_t reason);
/* Exactly once per actual encoder read, including disarmed/manual turns.
   Counts use the same left/forward and right/forward signs as STATE. */
void Project_EncoderSample(uint32_t now_ms, uint32_t interval_ms,
                           int32_t left, int32_t right);

/* Board adapter. No hardware initialization is performed here. */
void Project_TxKick(void);
void Project_ResetBoard(void);
uint32_t Project_IrqLock(void);
void Project_IrqUnlock(uint32_t previous);
void Project_ReadSensors(int32_t *encoder_left, int32_t *encoder_right,
                         float *speed_left, float *speed_right,
                         float *battery_voltage, uint8_t *car_mode,
                         uint8_t *local_enable);
void Project_ReadEncoderGeometry(uint32_t *nominal_counts_per_turn,
                                 uint32_t *diameter_um, uint32_t *spacing_um);
#endif
