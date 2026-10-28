#include "sensor.h"

ATTITUDE_DATA_t imu_attitude = {0};
static SensorSnapshot snapshot;

void sensor_get_snapshot(SensorSnapshot *result)
{
    if (result == NULL) return;
    taskENTER_CRITICAL();
    *result = snapshot;
    taskEXIT_CRITICAL();
}

void sensor_task(void *pvParameters)
{
    TickType_t last_wake = xTaskGetTickCount();
    ATTITUDE_DATA_t attitude = {0};
    uint32_t previous_seq = 0;
    float gyro_z = 0.0f;
    (void)pvParameters;
    while (1)
    {
        vTaskDelayUntil(&last_wake, F2T(RATE_100_HZ));
        /* Encoders remain exclusively owned by Balance_task (read-and-clear). */
        /* 直接读取陀螺Z寄存器，时间戳取读取时刻；不等待55Hz DMP包。 */
        if (ICM20948_ReadGyroZNow(&gyro_z) == 0) {
            taskENTER_CRITICAL();
            snapshot.gyro_z_rad_s = gyro_z;
            snapshot.gyro_tick_ms = getSysTickCnt();
            snapshot.gyro_seq++;
            snapshot.has_gyro = 1;
            taskEXIT_CRITICAL();
        }
        /* Publish the control gyro before FIFO parsing and attitude processing. */
        ICM20948_DMP_Update(&attitude);
        if (imu_sample_seq != previous_seq)
        {
            taskENTER_CRITICAL();
            imu_attitude = attitude;
            snapshot.attitude = attitude;
            snapshot.tick_ms = getSysTickCnt();
            snapshot.has_sample = 1;
            snapshot.calibrated = calib_done;
            taskEXIT_CRITICAL();
            previous_seq = imu_sample_seq;
        }
        /* Do not run catch-up FIFO reads after a delayed polling cycle. */
        if ((TickType_t)(xTaskGetTickCount() - last_wake) >= F2T(RATE_100_HZ))
            last_wake = xTaskGetTickCount();
    }
}
