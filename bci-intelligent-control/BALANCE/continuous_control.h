#ifndef CONTINUOUS_CONTROL_H
#define CONTINUOUS_CONTROL_H
#include <stdint.h>
#define CONTINUOUS_LEASE_MS 2000
/* Maximum outer-loop difference 0.6m/s / wheel track 0.16m. */
#define CONTINUOUS_OMEGA_LIMIT 3.75
void Continuous_ResetSession(void);
void Continuous_Stop(void);
int Continuous_Arm(uint32_t now);
uint8_t Continuous_Command(uint8_t,const uint8_t *,uint16_t,uint32_t,uint32_t,int,uint32_t *);
int Continuous_Tick(uint32_t,uint32_t,int,int *,int *);
void Continuous_Report(void);
#endif
