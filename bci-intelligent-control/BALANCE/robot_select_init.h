#ifndef __ROBOTSELECTINIT_H
#define __ROBOTSELECTINIT_H
#include "sys.h"
#include "system.h"

//Parameter structure of robot
//机器人参数结构体
typedef struct  
{
  float WheelSpacing;      //Wheelspacing, Mec_Car is half wheelspacing //轮距 麦轮车为半轮距
  float AxleSpacing;       //Axlespacing, Mec_Car is half axlespacing //轴距 麦轮车为半轴距	
  int GearRatio;           //Motor_gear_ratio //电机减速比
  int EncoderAccuracy;     //Number_of_encoder_lines //编码器精度(编码器线数)
  float WheelDiameter;     //Diameter of driving wheel //主动轮直径	
}Robot_Parament_InitTypeDef;

// Encoder structure
//编码器结构体
typedef struct  
{
  int A;      
  int B; 
	int C; 
	int D; 
}Encoder;

//The minimum turning radius of Ackermann models is determined by the mechanical structure: 
//the maximum Angle of the wheelbase, wheelbase and front wheels
//阿克曼车型的最小转弯半径，由机械结构决定：轮距、轴距、前轮最大转角
#define MINI_AKM_MIN_TURN_RADIUS 0.350f 


//请根据自己小车的实际情况测量并修改此处参数值
//Wheelspacing
//轮距 
#define Akm_wheelspacing         0.162f
#define Diff_wheelSpacing        0.177f
//Axlespacing
//轴距 
#define Akm_axlespacing           0.144f
#define Diff_axlespacing          0.155f

//Motor_gear_ratio
//电机减速比
#define   HALL_30F    30

//Number_of_encoder_lines
//编码器精度
#define	  Hall_13           13

//Black tire, tank_car wheel diameter
//黑色轮胎车轮直径
#define	  Small_WheelDiameter   0.065
#define	  Great_WheelDiameter   0.085

//The encoder octave depends on the encoder initialization Settings
//编码器倍频数，取决于编码器初始化设置
#define   EncoderMultiples  4
//Encoder data reading frequency
//编码器数据读取频率
#define   CONTROL_FREQUENCY 100

//#define PI 3.1415f  //PI //圆周率

void Robot_Select(void);
void Robot_Init(double wheelspacing, float axlespacing, float gearratio,float Accuracy,float tyre_diameter);

#endif
