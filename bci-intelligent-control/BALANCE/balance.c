#include "balance.h"
#include "project_link.h"

extern int Buzzer_count;
static uint32_t Encoder_sample_period_ms = 10U;

int Time_count=0; //Time variable //计时变量 
Encoder OriginalEncoder; //Encoder raw data //编码器原始数据    

/**************************************************************************
Function: The inverse kinematics solution is used to calculate the target speed of each wheel according to the target speed of three axes
Input   : X and Z axis direction of the target movement speed
Output  : none
函数功能：运动学逆解，根据三轴目标速度计算各车轮目标转速
入口参数：X和Z轴方向的目标运动速度
返回  值：无
**************************************************************************/
void Drive_Motor(float Vx, float Vz)
{
		float amplitude=3.5; //Wheel target speed limit //车轮目标速度限幅
		if (Car_Mode==Akm_Car) 
		{
			//Ackerman car specific related variables //阿克曼小车专用相关变量
			float R, Ratio=636.56, AngleR, Angle_Servo;
			
			// For Ackerman small car, Vz represents the front wheel steering Angle
			//对于阿克曼小车Vz代表右前轮转向角度
			AngleR=Vz;
			R=Axle_spacing/tan(AngleR)-0.5f*Wheel_spacing;
			
			// Front wheel steering Angle limit (front wheel steering Angle controlled by steering engine), unit: rad
			//前轮转向角度限幅(舵机控制前轮转向角度)，单位：rad
			AngleR=target_limit_float(AngleR,-0.49f,0.32f);
			
			//Inverse kinematics //运动学逆解
			if(AngleR!=0)
			{
				MOTOR_A.Target = Vx*(R-0.5f*Wheel_spacing)/R;
				MOTOR_B.Target = Vx*(R+0.5f*Wheel_spacing)/R;			
			}
			else 
			{
				MOTOR_A.Target = Vx;
				MOTOR_B.Target = Vx;
			}
			// The PWM value of the servo controls the steering Angle of the front wheel
			//舵机PWM值，舵机控制前轮转向角度
			Angle_Servo    =  -0.628f*pow(AngleR, 3) + 1.269f*pow(AngleR, 2) - 1.772f*AngleR + 1.755f;
			Servo=SERVO_INIT + (Angle_Servo - 1.755f)*Ratio;
			
			//Wheel (motor) target speed limit //车轮(电机)目标速度限幅
			MOTOR_A.Target=target_limit_float(MOTOR_A.Target,-amplitude,amplitude); 
			MOTOR_B.Target=target_limit_float(MOTOR_B.Target,-amplitude,amplitude); 
			Servo=target_limit_int(Servo,800,2200);	//Servo PWM value limit //舵机PWM值限幅
			}
		
		//Differential car
		//差速小车
		else if (Car_Mode==Diff_Car) 
		{
			//Inverse kinematics //运动学逆解
			MOTOR_A.Target  = Vx - Vz * Wheel_spacing / 2.0f; //计算出左轮的目标速度
		  MOTOR_B.Target =  Vx + Vz * Wheel_spacing / 2.0f; //计算出右轮的目标速度
			//Wheel (motor) target speed limit //车轮(电机)目标速度限幅
		  MOTOR_A.Target=target_limit_float( MOTOR_A.Target,-amplitude,amplitude); 
	    MOTOR_B.Target=target_limit_float( MOTOR_B.Target,-amplitude,amplitude); 
			
		}
}
/**************************************************************************
Function: FreerTOS task, core motion control task
Input   : none
Output  : none
函数功能：FreeRTOS任务，核心运动控制任务
入口参数：无
返回  值：无
**************************************************************************/
void Balance_task(void *pvParameters)
{
    uint32_t lastWakeTime = getSysTickCnt();
    uint32_t lastSampleTime = lastWakeTime;
    int left_pwm, right_pwm;

    /* Runtime only: preserve systemInit(), pin maps and timer parameters. */
    Set_Pwm(0, 0, SERVO_INIT);
    /* Discard counts accumulated before the first timed sample. */
    Read_Encoder(2); Read_Encoder(3); Read_Encoder(4); Read_Encoder(5);
    lastSampleTime = lastWakeTime = getSysTickCnt();
    while(1)
    {
        uint32_t now_ms;
        uint32_t elapsed_ms;
        uint8_t key_event;
        int local_allowed;

        vTaskDelayUntil(&lastWakeTime, F2T(RATE_100_HZ));
        now_ms = getSysTickCnt();
        elapsed_ms = now_ms - lastSampleTime;
        lastSampleTime = now_ms;
        Encoder_sample_period_ms = elapsed_ms ? elapsed_ms : 1U;
        if(Time_count < 3000) Time_count++;
        Get_Velocity_From_Encoder();
        local_allowed = (Turn_Off(Voltage) == 0);

        /* Do not execute catch-up control ticks after a scheduler overrun. */
        if(elapsed_ms > 30U)
        {
            Project_Stop(3);
            local_allowed = 0;
            lastWakeTime = now_ms;
        }
        /* Even shorter delays must not produce immediate duplicate samples. */
        if((int32_t)(now_ms - lastWakeTime) > 0) lastWakeTime = now_ms;
        key_event = click_N_Double(50);
        if(key_event == 1)
        {
            Project_Stop(1);
            local_allowed = 0;
        }
        else if(key_event == 2)
        {
            Buzzer_count = 0;
        }

        Project_ControlTick(now_ms, local_allowed);
        Project_GetOutput(&left_pwm, &right_pwm);
        MOTOR_A.Motor_Pwm = left_pwm;
        MOTOR_B.Motor_Pwm = right_pwm;
        if(!local_allowed || (left_pwm == 0 && right_pwm == 0))
        {
            Set_Pwm(0, 0, SERVO_INIT);
        }
        else if(Car_Mode == Akm_Car)
        {
            Set_Pwm(-left_pwm, right_pwm, Servo);
        }
        else
        {
            Set_Pwm(-left_pwm, right_pwm, 0);
        }
    }
}
/**************************************************************************
Function: Assign a value to the PWM register to control wheel speed and direction
Input   : PWM
Output  : none
函数功能：赋值给PWM寄存器，控制车轮转速与方向
入口参数：PWM
返回  值：无
**************************************************************************/
void Set_Pwm(int motor_a,int motor_b,int servo)
{
	if(motor_a<0)			PWMA2=16800,PWMA1=16800+motor_a;
	else 	            PWMA1=16800,PWMA2=16800-motor_a;
			
	if(motor_b<0)			PWMB1=16800,PWMB2=16800+motor_b;
	else 	            PWMB2=16800,PWMB1=16800-motor_b;

	
	//Servo control
	//舵机控制
	Servo_PWM =servo;
}

/**************************************************************************
Function: Limit PWM value
Input   : Value
Output  : none
函数功能：限制PWM值 
入口参数：幅值
返回  值：无
**************************************************************************/
void Limit_Pwm(int amplitude)
{	
	    MOTOR_A.Motor_Pwm=target_limit_float(MOTOR_A.Motor_Pwm,-amplitude,amplitude);
	    MOTOR_B.Motor_Pwm=target_limit_float(MOTOR_B.Motor_Pwm,-amplitude,amplitude);
}	    
/**************************************************************************
Function: Limiting function
Input   : Value
Output  : none
函数功能：限幅函数
入口参数：幅值
返回  值：无
**************************************************************************/
float target_limit_float(float insert,float low,float high)
{
    if (insert < low)
        return low;
    else if (insert > high)
        return high;
    else
        return insert;	
}
int target_limit_int(int insert,int low,int high)
{
    if (insert < low)
        return low;
    else if (insert > high)
        return high;
    else
        return insert;	
}
/**************************************************************************
Function: Check the battery voltage, enable switch status, software failure flag status
Input   : Voltage
Output  : Whether control is allowed, 1: not allowed, 0 allowed
函数功能：检查电池电压、使能开关状态、软件失能标志位状态
入口参数：电压
返回  值：是否允许控制，1：不允许，0允许
**************************************************************************/
u8 Turn_Off( int voltage)
{
	    u8 temp;
			if(voltage<10||EN==0||Flag_Stop==1)
			{	                                                
				temp=1; 
					
      }
			else
			temp=0;
			return temp;			
}
/**************************************************************************
Function: Calculate absolute value
Input   : long int
Output  : unsigned int
函数功能：求绝对值
入口参数：long int
返回  值：unsigned int
**************************************************************************/
u32 myabs(long int a)
{ 		   
	  u32 temp;
		if(a<0)  temp=-a;  
	  else temp=a;
	  return temp;
}


/**************************************************************************
Function: Read the encoder value and calculate the wheel speed, unit m/s
Input   : none
Output  : none
函数功能：读取编码器数值并计算车轮速度，单位m/s
入口参数：无
返回  值：无
**************************************************************************/
void Get_Velocity_From_Encoder(void)
{
	  //Retrieves the original data of the encoder
	  //获取编码器的原始数据
		float Encoder_A_pr,Encoder_B_pr; 
		OriginalEncoder.A=Read_Encoder(2);	
		OriginalEncoder.B=Read_Encoder(3);
		OriginalEncoder.C=Read_Encoder(4);
		OriginalEncoder.D=Read_Encoder(5);		

	  //Decide the encoder numerical polarity according to different car models
		//根据不同小车型号决定编码器数值极性
		switch(Car_Mode)
		{
			case Akm_Car:       Encoder_A_pr=OriginalEncoder.A; Encoder_B_pr=-OriginalEncoder.B; break;
			case Diff_Car:      Encoder_A_pr=OriginalEncoder.A; Encoder_B_pr=-OriginalEncoder.B; break;  
		}
		
		//The encoder converts the raw data to wheel speed in m/s
		//编码器原始数据转换为车轮速度，单位m/s
		MOTOR_A.Encoder= Encoder_A_pr*(1000.0f/Encoder_sample_period_ms)*Wheel_perimeter/Encoder_precision;  
		MOTOR_B.Encoder= Encoder_B_pr*(1000.0f/Encoder_sample_period_ms)*Wheel_perimeter/Encoder_precision;  
}

/**************************************************************************
Function: Floating-point data calculates the absolute value
Input   : float
Output  : The absolute value of the input number
函数功能：浮点型数据计算绝对值
入口参数：浮点数
返回  值：输入数的绝对值
**************************************************************************/
float float_abs(float insert)
{
	if(insert>=0) return insert;
	else return -insert;
}





