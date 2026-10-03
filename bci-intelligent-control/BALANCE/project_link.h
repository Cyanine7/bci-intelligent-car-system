#ifndef PROJECT_LINK_H
#define PROJECT_LINK_H
#include <stdint.h>

#define PROJECT_PWM_LIMIT 6000
#define PROJECT_SPEED_LIMIT 300
#define PROJECT_MAX_DURATION 1000

/* One producer (USART2 IRQ), one consumer (100 Hz control task). */
void Project_RxByte(uint8_t byte);
void Project_RxError(void);
int Project_TxByte(uint8_t *byte);
void Project_ControlTick(uint32_t now_ms, int local_allowed);
void Project_GetOutput(int *left, int *right);
void Project_Stop(uint8_t reason);

/* Board adapter. No hardware initialization is performed here. */
void Project_TxKick(void);
void Project_ResetBoard(void);
uint32_t Project_IrqLock(void);
void Project_IrqUnlock(uint32_t previous);
void Project_ReadSensors(int32_t *encoder_left, int32_t *encoder_right,
                         float *speed_left, float *speed_right,
                         float *battery_voltage, uint8_t *car_mode,
                         uint8_t *local_enable);
#endif
