#ifndef __BALANCE_H
#define __BALANCE_H			  	 
#include "sys.h"
#include "system.h"
#include "lidar.h"

#define BALANCE_TASK_PRIO		4     //Task priority //任务优先级
#define BALANCE_STK_SIZE 		512   //Task stack size //任务堆栈大小

extern uint8_t Mode;

void Balance_task(void *pvParameters);
void Set_Pwm(int motor_a,int motor_b,int servo);
void Limit_Pwm(int amplitude);
float target_limit_float(float insert,float low,float high);
int target_limit_int(int insert,int low,int high);
uint8_t Turn_Off( int voltage);
uint32_t myabs(long int a);
void Drive_Motor(float Vx,float Vz);
void Get_Velocity_From_Encoder(void);

#endif  

