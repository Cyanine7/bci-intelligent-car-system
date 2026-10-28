#ifndef __BSP_IMU_H
#define __BSP_IMU_H

#include <stdint.h>
#include <math.h>

#define RAD2DEG    57.2958f

typedef struct{ //中间私有变量
	float x;
	float y;
	float z;
}PrivateBuf_t;
	
typedef struct{
	PrivateBuf_t gyro; //3轴角速度
	PrivateBuf_t accel;//3轴加速度
	PrivateBuf_t magn;//3轴磁力计
}IMU_DATA_t;

typedef struct{
	float roll;
	float pitch;
	float yaw;
}ATTITUDE_DATA_t;


extern uint8_t calib_done;
extern uint8_t imu_init_status;
extern uint32_t imu_sample_seq;
extern uint32_t imu_gyro_seq;
extern IMU_DATA_t imu_data, imu_raw_data; 
extern float car_ax,car_ay;

int ICM20948_ReadGyroZNow(float *z);

uint8_t ICM20948_DMP_Init(void);
void ICM20948_DMP_Update(ATTITUDE_DATA_t *attitude);
void IMU_Calib_Window_Reset(void);

#endif /* __BSP_IMU_H */
