#include "imu.h"
#include "bsp_siic.h"          // I2C 
#include "icm20948.h"          // 学长驱动
#include "icm20948_dmp.h"      // DMP 定义
#include "icm20948_registers.h"
#include "delay.h"
#include <math.h>

// 静态设备句柄（只在本文件使用）
static icm20948_device_t icm_dev;
static uint8_t dmp_ready = 0;   // 初始化成功标志

// 校准状态
static uint8_t calib_mode = 1;
static uint8_t zero_ready = 0;
static ATTITUDE_DATA_t zero_attitude = {0};
#define WINDOW_SIZE 50            // 窗口大小（采样次数）
#define STABLE_PKPK_THRESH 0.05f  // 峰峰值阈值（度）
#define STABLE_WINDOW_COUNT 10     // 需要连续满足条件的窗口数
static float yaw_history[WINDOW_SIZE];
static uint8_t history_index = 0;
static uint8_t history_filled = 0;
static uint8_t stable_count = 0;

uint8_t calib_done = 0;
uint8_t imu_init_status = 1;
uint32_t imu_gyro_seq = 0;
uint32_t imu_sample_seq = 0; 

IMU_DATA_t imu_data = { 0 };
IMU_DATA_t imu_raw_data = { 0 };
double q0,q1,q2,q3;


uint8_t ICM20948_DMP_Init(void)
{
    uint8_t whoami = 0;
    icm20948_int_pin_cfg_t int_cfg = {0};
    icm20948_int_enable_t int_use = {0};
    dmp_ready = 0;
    imu_sample_seq = 0;
    imu_gyro_seq = 0;
    imu_init_status = 1;
    IMU_Calib_Window_Reset();
    User_sIICDev.init();
    icm20948_init_i2c(&icm_dev);

    /* Every failed transaction leaves the IMU unavailable; never publish zero as valid data. */
#define IMU_TRY(call) do { if ((call) != ICM_20948_STAT_OK) return 1; } while (0)
    IMU_TRY(icm20948_get_who_am_i(&icm_dev, &whoami));
    if (whoami != ICM_20948_WHOAMI) return 1;
    IMU_TRY(icm20948_sw_reset(&icm_dev));
    delay_ms(100);
    IMU_TRY(icm20948_sleep(&icm_dev, false));
    IMU_TRY(icm20948_low_power(&icm_dev, false));
    IMU_TRY(icm20948_int_pin_cfg(&icm_dev, &int_cfg, NULL));
    IMU_TRY(icm20948_int_enable(&icm_dev, &int_use, NULL));

    /* The supplied DMP defaults configure +/-4g, +/-2000dps and 55Hz sampling.
       Keep their gyro scaling and DMP coefficients consistent. Host polling is 100Hz. */
    IMU_TRY(icm20948_init_dmp_sensor_with_defaults(&icm_dev));
    IMU_TRY(inv_icm20948_enable_dmp_sensor(&icm_dev, INV_ICM20948_SENSOR_GAME_ROTATION_VECTOR, 1));
    IMU_TRY(inv_icm20948_enable_dmp_sensor_int(&icm_dev, INV_ICM20948_SENSOR_GAME_ROTATION_VECTOR, 1));
    IMU_TRY(inv_icm20948_set_dmp_sensor_period(&icm_dev, DMP_ODR_Reg_Quat6, 0));
    IMU_TRY(inv_icm20948_enable_dmp_sensor(&icm_dev, INV_ICM20948_SENSOR_RAW_GYROSCOPE, 1));
    IMU_TRY(inv_icm20948_set_dmp_sensor_period(&icm_dev, DMP_ODR_Reg_Gyro, 0));
    IMU_TRY(icm20948_enable_fifo(&icm_dev, true));
    IMU_TRY(icm20948_enable_dmp(&icm_dev, true));
    IMU_TRY(icm20948_reset_dmp(&icm_dev));
    IMU_TRY(icm20948_reset_fifo(&icm_dev));
#undef IMU_TRY
    dmp_ready = 1;
    imu_init_status = 0;
    return 0;
}

void ICM20948_DMP_Update(ATTITUDE_DATA_t *attitude)
{
    if (!dmp_ready || attitude == NULL) return;

    icm_20948_DMP_data_t data = {0};
    icm20948_status_e status;
    uint8_t got_quat = 0;
	  uint8_t max_frames = 3;  
    float roll = 0, pitch = 0, yaw = 0;

    /* 循环读空 FIFO，只保留最新的 Quat6 和 Gyro.Z */
    while (max_frames--) {
        status = inv_icm20948_read_dmp_data(&icm_dev, &data);
        if (status == ICM_20948_STAT_FIFO_NO_DATA_AVAIL)   break;
        if (status == ICM_20948_STAT_FIFO_INCOMPLETE_DATA) break;

        if (status != ICM_20948_STAT_OK && status != ICM_20948_STAT_FIFO_MORE_DATA_AVAIL) {
            /* Reject failed/unknown FIFO frames before examining their payload. */
            icm20948_reset_fifo(&icm_dev);
            break;
        }

        if (data.header & DMP_header_bitmap_Quat6) {
            q1 = ((double)data.Quat6.Data.Q1) / 1073741824.0;
            q2 = ((double)data.Quat6.Data.Q2) / 1073741824.0;
            q3 = ((double)data.Quat6.Data.Q3) / 1073741824.0;
            double s  = 1.0 - (q1*q1 + q2*q2 + q3*q3);
            if (s < 0) s = 0;
            q0 = sqrt(s);

            roll  = atan2f(2.0f*(q0*q1 + q2*q3), 1.0f - 2.0f*(q1*q1 + q2*q2)) * RAD2DEG;
            float sin_pitch = 2.0f*(q0*q2 - q3*q1);
            if (sin_pitch > 1.0f) sin_pitch = 1.0f;
            if (sin_pitch < -1.0f) sin_pitch = -1.0f;
            pitch = asinf(sin_pitch) * RAD2DEG;
            yaw   = atan2f(2.0f*(q0*q3 + q1*q2), 1.0f - 2.0f*(q2*q2 + q3*q3)) * RAD2DEG;
            got_quat = 1;
        }

        if (data.header & DMP_header_bitmap_Gyro) {
            const float gyro_scale = 34.9065f / 32768.0f;
            imu_raw_data.gyro.z = (float)data.Raw_Gyro.Data.Z;
            imu_data.gyro.z     = imu_raw_data.gyro.z * gyro_scale;
            imu_gyro_seq++;
        }

        if (status != ICM_20948_STAT_FIFO_MORE_DATA_AVAIL) break;
    }

    if (!got_quat) return;
    imu_sample_seq++;

    /* ---------- 校准模式 ---------- */
    if (calib_mode) {
        yaw_history[history_index++] = yaw;
        if (history_index >= WINDOW_SIZE) {
            history_index  = 0;
            history_filled = 1;
        }

        if (history_filled) {
            float yaw_min = yaw_history[0], yaw_max = yaw_history[0];
            for (int i = 1; i < WINDOW_SIZE; i++) {
                if (yaw_history[i] < yaw_min) yaw_min = yaw_history[i];
                if (yaw_history[i] > yaw_max) yaw_max = yaw_history[i];
            }
            if ((yaw_max - yaw_min) < STABLE_PKPK_THRESH) stable_count++;
            else                                          stable_count = 0;

            if (stable_count >= STABLE_WINDOW_COUNT) {
                zero_attitude.roll  = roll;
                zero_attitude.pitch = pitch;
                zero_attitude.yaw   = yaw;
                zero_ready = 1;
                calib_mode = 0;
                calib_done = 1;
            }
        }

        attitude->roll  = roll;
        attitude->pitch = pitch;
        attitude->yaw   = yaw;
    }
    /* ---------- 正常模式 ---------- */
    else {
       if (zero_ready) {
           roll  -= zero_attitude.roll;
           pitch -= zero_attitude.pitch;
           yaw   -= zero_attitude.yaw;
           if (yaw > 180.0f) yaw -= 360.0f;
           if (yaw < -180.0f) yaw += 360.0f;
       }
        attitude->roll  = roll;
        attitude->pitch = pitch;
        attitude->yaw   = yaw;
    }
}

int ICM20948_ReadGyroZNow(float *z)
{
    uint8_t buffer[2];
    icm20948_status_e status;
    if (z == NULL || !dmp_ready) return 1;
    status = icm20948_set_bank(&icm_dev, 0);
    if (status != ICM_20948_STAT_OK) return 1;
    status = icm20948_execute_r(&icm_dev, (uint8_t)AGB0_REG_GYRO_ZOUT_H, buffer, 2);
    if (status != ICM_20948_STAT_OK) return 1;
    /* 2000dps量程；时间戳由调用者取读取时刻。 */
    *z = (float)(int16_t)(((uint16_t)buffer[0] << 8) | buffer[1]) * (34.9065f / 32768.0f);
    return 0;
}

/* ==================== 校准状态复位 ==================== */
void IMU_Calib_Window_Reset(void)
{
    history_index  = 0;
    history_filled = 0;
    stable_count   = 0;
    calib_mode     = 1;
    zero_ready     = 0;
    calib_done     = 0;
}
