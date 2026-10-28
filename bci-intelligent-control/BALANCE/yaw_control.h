#ifndef YAW_CONTROL_H
#define YAW_CONTROL_H
#include <stdint.h>
#include "speed_control.h"
/* 与PROJECT_SPEED_LIMIT一致：PWM 16500按负载模型折合约1.19m/s，取1.18。 */
#define YAW_MAX_WHEEL_SPEED 1.18
#define YAW_MAX_SEGMENT_MS 5000
#define YAW_MAX_TOTAL_MS 10000
#define YAW_CAPTURE_CAPACITY 1040
#define YAW_SAMPLE_BYTES 84
typedef struct { double kp,ki,delta_limit; int sign; } YawConfig;
typedef struct { YawConfig config; double error,output; } YawController;
typedef struct { double raw,delta,left,right,integral; uint8_t saturated,fault; } YawOutput;
void YawControl_Reset(YawController *);
int YawControl_Configure(YawController *,const YawConfig *);
YawOutput YawControl_Step(YawController *,double,double,double,double,int);
/* 唯一板级入口；序号和时间只在收到新的有效陀螺仪帧时更新。 */
int Project_ReadGyro(float *,uint32_t *,uint32_t *);
void YawExperiment_ResetSession(void);
void YawExperiment_Stop(uint32_t,uint8_t);
int YawExperiment_Busy(void);
int YawExperiment_ArmAllowed(uint32_t);
int YawExperiment_Negotiated(void);
void YawExperiment_ReleaseCapture(void);
uint8_t YawExperiment_Command(uint8_t,const uint8_t *,uint16_t,uint32_t,uint32_t,int);
uint8_t YawExperiment_Start(const uint8_t *,uint16_t,uint32_t,uint32_t,uint32_t *);
int YawExperiment_Tick(uint32_t,uint32_t,int,int *,int *);
void YawExperiment_Report(void);
void YawExperiment_Pump(void);
#endif
