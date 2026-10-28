#include "show.h"
int Voltage_Show;
unsigned char i;          
unsigned char Send_Count; 
extern int Time_count;
/**************************************************************************
Function: Read the battery voltage, buzzer alarm, start the self-test, send data to APP, OLED display task
Input   : none
Output  : none
函数功能：读取电池电压、蜂鸣器报警、开启自检、向APP发送数据、OLED显示屏显示任务
入口参数：无
返回  值：无
**************************************************************************/
int Buzzer_count=25;
void show_task(void *pvParameters)
{
   u32 lastWakeTime = getSysTickCnt();
   while(1)
   {	
		int i=0;
		static int LowVoltage_1=0, LowVoltage_2=0;
		vTaskDelayUntil(&lastWakeTime, F2T(RATE_20_HZ));//This task runs at 20Hz //此任务以20Hz的频率运行
		
		//开机时蜂鸣器短暂蜂鸣，开机提醒
		//The buzzer will beep briefly when the machine is switched on
		if(Time_count<50)Buzzer=1; 
		else if(Time_count>=50 && Time_count<100)Buzzer=0;
		 
		if(LowVoltage_1==1 || LowVoltage_2==1)Buzzer_count=0;
		if(Buzzer_count<5)Buzzer_count++;
		if(Buzzer_count<5)Buzzer=1; //The buzzer is buzzing //蜂鸣器蜂鸣
		else if(Buzzer_count==5)Buzzer=0;
		
		//Read the battery voltage //读取电池电压
		for(i=0;i<10;i++)
		{
			Voltage_All+=Get_battery_volt(); 
		}
		Voltage=Voltage_All/10;
		Voltage_All=0;
		 
		if(LowVoltage_1==1)LowVoltage_1++; //Make sure the buzzer only rings for 0.5 seconds //确保蜂鸣器只响0.5秒
		if(LowVoltage_2==1)LowVoltage_2++; //Make sure the buzzer only rings for 0.5 seconds //确保蜂鸣器只响0.5秒
		if(Voltage>=12.6f)Voltage=12.6f;
		else if(10<=Voltage && Voltage<10.5f && LowVoltage_1<2)LowVoltage_1++; //10.5V, first buzzer when low battery //10.5V，低电量时蜂鸣器第一次报警
		else if(Voltage<10 && LowVoltage_2<2)LowVoltage_2++; //10V, when the car is not allowed to control, the buzzer will alarm the second time //10V，小车禁止控制时蜂鸣器第二次报警
					
		/* PROJECT_V1 STATE is emitted by the control task; do not mix text frames. */
		oled_show(); //Tasks are displayed on the screen //显示屏显示任务
   }
}  

/**************************************************************************
Function: The OLED display displays tasks
Input   : none
Output  : none
函数功能：OLED显示屏显示任务
入口参数：无
返回  值：无
**************************************************************************/
void oled_show(void)
{
    SensorSnapshot imu;
    char imu_line[17];
    const char *imu_status;
    int voltage_cv = (int)(Voltage * 100.0f);
    int left_mm_s = (int)(MOTOR_A.Encoder * 1000.0f);
    int right_mm_s = (int)(MOTOR_B.Encoder * 1000.0f);

    /* Display the model selected at boot, not a live potentiometer guess. */
    OLED_ShowString(0, 10, "L:");
    OLED_ShowString(15, 10, (const u8*)(MOTOR_A.Motor_Pwm < 0 ? "-" : "+"));
    OLED_ShowNumber(20, 10, myabs((long)MOTOR_A.Motor_Pwm), 5, 12);
    OLED_ShowString(60, 10, (const u8*)(left_mm_s < 0 ? "-" : "+"));
    OLED_ShowNumber(75, 10, myabs(left_mm_s), 5, 12);
    OLED_ShowString(0, 20, "R:");
    OLED_ShowString(15, 20, (const u8*)(MOTOR_B.Motor_Pwm < 0 ? "-" : "+"));
    OLED_ShowNumber(20, 20, myabs((long)MOTOR_B.Motor_Pwm), 5, 12);
    OLED_ShowString(60, 20, (const u8*)(right_mm_s < 0 ? "-" : "+"));
    OLED_ShowNumber(75, 20, myabs(right_mm_s), 5, 12);
    sensor_get_snapshot(&imu);
    if (imu_init_status != 0) imu_status = "ERR";
    else if (!imu.has_sample) imu_status = "WAIT";
    else if ((uint32_t)(getSysTickCnt() - imu.tick_ms) > 200u) imu_status = "OLD";
    else imu_status = imu.calibrated ? "OK" : "CAL";
    snprintf(imu_line, sizeof(imu_line), "%s IMU:%-4s  ",
             Car_Mode == Akm_Car ? "Akm " : "Diff", imu_status);
    OLED_ShowString(0, 0, (const u8*)imu_line);
    if (imu_init_status == 0 && imu.has_sample &&
        (uint32_t)(getSysTickCnt() - imu.tick_ms) <= 200u)
    {
        long yaw_cdeg = (long)(imu.attitude.yaw * 100.0f);
        long gyro_mrad = (long)(imu.gyro_z_rad_s * 1000.0f);
        long yaw_abs = myabs(yaw_cdeg), gyro_abs = myabs(gyro_mrad);
        snprintf(imu_line, sizeof(imu_line), "Yaw:%c%3ld.%02lddeg ",
                 yaw_cdeg < 0 ? '-' : '+', yaw_abs / 100, yaw_abs % 100);
        OLED_ShowString(0, 30, (const u8*)imu_line);
        snprintf(imu_line, sizeof(imu_line), "Gz :%c%2ld.%03ldrad ",
                 gyro_mrad < 0 ? '-' : '+', gyro_abs / 1000, gyro_abs % 1000);
        OLED_ShowString(0, 40, (const u8*)imu_line);
    }
    else
    {
        OLED_ShowString(0, 30, "Yaw: ---        ");
        OLED_ShowString(0, 40, "Gz : ---        ");
    }
    /* EN is the physical switch, independent of protocol arming. */
    OLED_ShowString(0, 50, "EN:");
    OLED_ShowNumber(26, 50, EN ? 1 : 0, 1, 12);
    OLED_ShowString(40, 50, (const u8*)(Turn_Off(Voltage) == 0 ? "OK " : "OFF"));
    if(voltage_cv < 0) voltage_cv = 0;
    OLED_ShowNumber(75, 50, voltage_cv / 100, 2, 12);
    OLED_ShowString(88, 50, ".");
    OLED_ShowNumber(98, 50, voltage_cv % 100, 2, 12);
    OLED_ShowString(110, 50, "V");
    OLED_Refresh_Gram();
}
/**************************************************************************
Function: Send data to the APP
Input   : none
Output  : none
函数功能：向APP发送数据
入口参数：无
返回  值：无
**************************************************************************/
void APP_Show(void)
{    
	 int Left_Figure,Right_Figure,Voltage_Show;
	
	 //The battery voltage is processed as a percentage
	 //对电池电压处理成百分比形式
	 Voltage_Show=(Voltage*1000-10000)/27;
	 if(Voltage_Show>100)Voltage_Show=100; 
	
	 //Wheel speed unit is converted to 0.01m/s for easy display in APP
	 //车轮速度单位转换为0.01m/s，方便在APP显示
	 Left_Figure=MOTOR_A.Encoder*100;  if(Left_Figure<0)Left_Figure=-Left_Figure;	
	 Right_Figure=MOTOR_B.Encoder*100; if(Right_Figure<0)Right_Figure=-Right_Figure; 		 
	 
	 printf("{C%d:%d:%d}$",(int)Left_Figure,(int)Right_Figure,(int)Voltage_Show);
}


