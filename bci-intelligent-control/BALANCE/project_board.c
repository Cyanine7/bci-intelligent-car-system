#include "system.h"
#include "project_link.h"

extern Encoder OriginalEncoder;

void Project_TxKick(void)
{ USART_ITConfig(USART2, USART_IT_TXE, ENABLE); }
void Project_ResetBoard(void)
{
    Set_Pwm(0,0,SERVO_INIT);
    NVIC_SystemReset();
}
uint32_t Project_IrqLock(void)
{ uint32_t previous=__get_PRIMASK(); __disable_irq(); return previous; }
void Project_IrqUnlock(uint32_t previous)
{ if(!previous) __enable_irq(); }
void Project_ReadSensors(int32_t *left, int32_t *right,
                         float *speed_left, float *speed_right,
                         float *battery, uint8_t *car, uint8_t *enable)
{
    *left=OriginalEncoder.A; *right= -OriginalEncoder.B;
    *speed_left=MOTOR_A.Encoder; *speed_right=MOTOR_B.Encoder;
    *battery=Voltage; *car=Car_Mode; *enable=EN;
}
