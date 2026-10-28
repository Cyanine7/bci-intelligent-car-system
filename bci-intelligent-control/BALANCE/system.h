#ifndef __SYSTEM_H
#define __SYSTEM_H

// Refer to all header files you need
//����������Ҫ�õ���ͷ�ļ�
#include "FreeRTOSConfig.h"
//FreeRTOS���ͷ�ļ� 
//FreeRTOS related header files
#include "FreeRTOS.h"
#include "stm32f4xx.h"
#include "task.h"
#include "queue.h"
#include "timers.h"
#include "semphr.h"
//The associated header file for the peripheral 
//��������ͷ�ļ�
#include "sys.h"
#include "delay.h"
#include "usart.h"
#include "balance.h"
#include "led.h"
#include "oled.h"
#include "usart.h"
#include "usartx.h"
#include "adc.h"
#include "motor.h"
#include "encoder.h"
#include "show.h"								   
#include "key.h"
#include "lidar.h"
#include "pstwo.h"
#include "robot_select_init.h"
#include "sensor.h"
#include "imu.h"

// Enumeration of car types
//С���ͺŵ�ö�ٶ���
typedef enum 
{ 
	Akm_Car=0, 
	Diff_Car, 
} CarMode;

//Motor speed control related parameters of the structure
//����ٶȿ�����ز����ṹ��
typedef struct  
{
	float Encoder;     //Read the real time speed of the motor by encoder //��������ֵ����ȡ���ʵʱ�ٶ�
	float Motor_Pwm;   //Motor PWM value, control the real-time speed of the motor //���PWM��ֵ�����Ƶ��ʵʱ�ٶ�
	float Target;      //Control the target speed of the motor //���Ŀ���ٶ�ֵ�����Ƶ��Ŀ���ٶ�
}Motor_parameter;


/****** external variable definition. When system.h is referenced in other C files, 
        other C files can also use the variable defined by system.c           ******/
/****** �ⲿ�������壬������c�ļ�����system.hʱ��Ҳ����ʹ��system.c����ı��� ******/
extern uint8_t Flag_Stop;
extern int Divisor_Mode;
extern uint8_t Car_Mode;
extern int Servo;
extern float RC_Velocity;
extern float Move_X, Move_Z; 	
extern Motor_parameter MOTOR_A, MOTOR_B;
extern float Encoder_precision;
extern float Wheel_perimeter;
extern float Wheel_spacing; 
extern float Axle_spacing; 

extern uint8_t Flag_Left, Flag_Right, Flag_Direction, Turn_Flag; 
extern uint8_t PID_Send;  

void systemInit(void);

/***Macros define***/ /***�궨��***/
//The number of robot types to determine the value of Divisor_Mode. There are currently 6 car types
//�������ͺ�����������Divisor_Mode��ֵ��Ŀǰ��6��С������
#define CAR_NUMBER    2      
#define RATE_1_HZ		  1
#define RATE_5_HZ		  5
#define RATE_10_HZ		10
#define RATE_20_HZ		20
#define RATE_25_HZ		25
#define RATE_50_HZ		50
#define RATE_100_HZ		100
#define RATE_200_HZ 	200
#define RATE_250_HZ 	250
#define RATE_500_HZ 	500
#define RATE_1000_HZ 	1000
/***Macros define***/ /***�궨��***/

//C library function related header file
//C�⺯�������ͷ�ļ�
#include <stdio.h> 
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "stdarg.h"
#endif 
