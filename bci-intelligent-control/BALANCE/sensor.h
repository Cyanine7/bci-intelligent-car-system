#ifndef __SENSOR_H
#define __SENSOR_H

#include "imu.h"
#include "system.h"

/* Keep software I2C and attitude math below the motor control task (priority 4). */
#define SENSOR_TASK_PRIO 3
#define SENSOR_STK_SIZE 1024

typedef struct
{
    ATTITUDE_DATA_t attitude;
    float gyro_z_rad_s;
    uint32_t tick_ms;
    uint8_t has_sample;
    uint8_t calibrated;
    uint32_t gyro_tick_ms, gyro_seq;
    uint8_t has_gyro;
} SensorSnapshot;

extern ATTITUDE_DATA_t imu_attitude;
void sensor_task(void *pvParameters);
void sensor_get_snapshot(SensorSnapshot *result);

#endif
