#ifndef EXPERIMENT_STORAGE_H
#define EXPERIMENT_STORAGE_H
#include "speed_capture.h"
#include "yaw_control.h"
/* 两种实验互斥，共用存储，避免10秒记录再叠加一份轮速缓存。 */
typedef union { SpeedSample speed[SPEED_CAPTURE_CAPACITY];
 uint8_t yaw[YAW_CAPTURE_CAPACITY][YAW_SAMPLE_BYTES]; } ExperimentStorage;
extern ExperimentStorage experiment_storage;
#endif
