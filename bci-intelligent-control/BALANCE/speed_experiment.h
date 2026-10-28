#ifndef SPEED_EXPERIMENT_H
#define SPEED_EXPERIMENT_H
#include "speed_capture.h"
void SpeedExperiment_ResetSession(void);
void SpeedExperiment_Stop(uint32_t,uint8_t);
int SpeedExperiment_ArmAllowed(uint8_t);
int SpeedExperiment_Busy(void);
int SpeedExperiment_Negotiated(void);
uint8_t SpeedExperiment_Command(uint8_t,const uint8_t *,uint16_t,uint32_t,uint32_t,int);
uint8_t SpeedExperiment_Start(uint8_t,const uint8_t *,uint16_t,uint32_t,uint32_t,uint32_t *);
int SpeedExperiment_Tick(uint32_t,uint32_t,int,int *,int *);
void SpeedExperiment_Report(void);
void SpeedExperiment_Pump(void);
void Project_SpeedReply(uint8_t,uint32_t,const uint8_t *,uint16_t);
#endif

