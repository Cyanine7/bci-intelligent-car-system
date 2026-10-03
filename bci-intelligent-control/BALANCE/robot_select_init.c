#include "robot_select_init.h"

//Initialize the robot parameter structure
//初始化机器人参数结构体
Robot_Parament_InitTypeDef  Robot_Parament; 
/**************************************************************************
Function: According to the potentiometer switch needs to control the car type
Input   : none
Output  : none
函数功能：根据电位器切换需要控制的小车类型
入口参数：无
返回  值：无
**************************************************************************/
void Robot_Select(void)
{
	//The ADC value is variable in segments, depending on the number of car models. Currently there are 6 car models, CAR_NUMBER=2
  //ADC值分段变量，取决于小车型号数量，目前有2种小车型号，CAR_NUMBER=2
	Divisor_Mode=2048/CAR_NUMBER+CAR_NUMBER;
	Car_Mode=(int) ((Get_adc_Average(Potentiometer,10))/Divisor_Mode); //Collect the pin information of potentiometer //采集电位器引脚信息	
  if(Car_Mode>(CAR_NUMBER-1))Car_Mode=CAR_NUMBER-1;

	switch(Car_Mode)
	{
		case Akm_Car:       Robot_Init(Akm_wheelspacing,         Akm_axlespacing, HALL_30F, Hall_13, Small_WheelDiameter);   break; //阿克曼小车
		case Diff_Car:      Robot_Init(Diff_wheelSpacing,        0,               HALL_30F, Hall_13, Great_WheelDiameter);   break; //两轮差速小车
	}
	
}

/**************************************************************************
Function: Initialize cart parameters
Input   : wheelspacing, axlespacing, motor_gear_ratio, Number_of_encoder_lines, tyre_diameter
Output  : none
函数功能：初始化小车参数
入口参数：轮距 轴距 电机减速比 电机编码器精度 轮胎直径
返回  值：无
**************************************************************************/
void Robot_Init(double wheelspacing, float axlespacing,float gearratio,float Accuracy,float tyre_diameter) // 
{
	//wheelspacing
	//轮距 
  Robot_Parament.WheelSpacing=wheelspacing; 
	//axlespacing
  //轴距
  Robot_Parament.AxleSpacing=axlespacing;   
	//motor_gear_ratio
	//电机减速比
  Robot_Parament.GearRatio=gearratio; 
	//Number_of_encoder_lines
  //编码器精度(编码器线数)	
  Robot_Parament.EncoderAccuracy=Accuracy;
	//Diameter of driving wheel
  //主动轮直径	
  Robot_Parament.WheelDiameter=tyre_diameter;       
	
	//Encoder value corresponding to 1 turn of motor (wheel)
	//电机(车轮)转1圈对应的编码器数值
	Encoder_precision=EncoderMultiples*Robot_Parament.EncoderAccuracy*Robot_Parament.GearRatio;
	//Driving wheel circumference
  //主动轮周长	
	Wheel_perimeter=Robot_Parament.WheelDiameter*PI;
	//wheelspacing
  //轮距 
  Wheel_spacing=Robot_Parament.WheelSpacing; 
  //axlespacing	
  //轴距 
	Axle_spacing=Robot_Parament.AxleSpacing; 
	
}


