#ifndef SPEED_CAPTURE_H
#define SPEED_CAPTURE_H
#include <stdint.h>
#define SPEED_CAPTURE_CAPACITY 384
#define SPEED_SAMPLE_BYTES 52
enum { SPEED_PRE=1,SPEED_ACTIVE=2,SPEED_POST=4,SPEED_SAT_LEFT=8,
 SPEED_SAT_RIGHT=16,SPEED_FAULT=128,SPEED_BAD_DT=256,SPEED_SECOND=512 };
typedef struct {
 uint32_t tick;uint16_t dt,flags;int16_t target[2];
 float filtered[2],measured[2],raw[2];int16_t pwm[2];
 uint16_t battery;int32_t encoder[2];
} SpeedSample;
void SpeedCapture_Reset(void);
void SpeedCapture_Begin(uint32_t,uint32_t,uint32_t,uint8_t,const uint8_t[36],const uint8_t[20]);
void SpeedCapture_Segment(uint8_t,uint32_t);
void SpeedCapture_Stop(uint32_t,uint8_t);
void SpeedCapture_Push(const SpeedSample *);
uint8_t SpeedCapture_Status(void);
uint16_t SpeedCapture_Count(void);
uint32_t SpeedCapture_Run(void);
void SpeedCapture_Info(uint8_t[60]);
void SpeedCapture_Meta(uint8_t[40]);
int SpeedCapture_Read(uint16_t,SpeedSample *);
void SpeedSample_Encode(uint8_t[52],const SpeedSample *);
#endif

