#include "../BALANCE/project_link.c"

static float fixture_left, fixture_right, fixture_voltage=12.0f;
static int board_resets;
static int32_t fixture_count_left=12,fixture_count_right=-13;
void Project_TxKick(void) {}
void Project_ResetBoard(void) { board_resets++; }
uint32_t Project_IrqLock(void) { return 0; }
void Project_IrqUnlock(uint32_t p) { (void)p; }
void Project_ReadSensors(int32_t *a,int32_t *b,float *x,float *y,float *v,uint8_t *car,uint8_t *enable)
{ *a=fixture_count_left; *b=fixture_count_right; *x=fixture_left; *y=fixture_right; *v=fixture_voltage; *car=1; *enable=1; }
void Project_ReadEncoderGeometry(uint32_t *counts,uint32_t *diameter,uint32_t *spacing)
{ *counts=1560; *diameter=66000; *spacing=160000; }

__declspec(dllexport) void test_reset(void)
{
    rx_head=rx_tail=tx_head=tx_tail=0; rx_dropped=tx_dropped=overflow=0;
    used=reset_count=binary_seen=0; candidate_started=reset_started=last_binary_byte=0;
    session=last_seq=report_seq=revision=0; memset(gains,0,sizeof(gains));
    session_count=0; memset(session_history,0,sizeof(session_history));
    end_tick=idle_end_tick=next_report=0; local_ok=0;
    profile_enabled=0; clear_profile();
    encoder_total[0]=encoder_total[1]=0;
    encoder_epoch=encoder_tick=encoder_sample_seq=encoder_interval=0;
    encoder_delayed=encoder_report_seq=0; encoder_initialized=encoder_enabled=0;
    fixture_left=fixture_right=0; fixture_voltage=12; board_resets=0;
    fixture_count_left=12;fixture_count_right=-13;
    Project_Stop(0);
    SpeedExperiment_ResetSession();
    YawExperiment_ResetSession();
}
__declspec(dllexport) void test_rx(const uint8_t *p,unsigned n)
{ while(n--) Project_RxByte(*p++); }
__declspec(dllexport) void test_error(void) { Project_RxError(); }
__declspec(dllexport) void test_tick(uint32_t n,int ok) { Project_ControlTick(n,ok); }
__declspec(dllexport) void test_output(int *a,int *b) { Project_GetOutput(a,b); }
__declspec(dllexport) unsigned test_drain(uint8_t *p,unsigned n)
{ unsigned used_bytes=0; while(used_bytes<n && Project_TxByte(p+used_bytes))used_bytes++; return used_bytes; }
__declspec(dllexport) int test_resets(void) { return board_resets; }
__declspec(dllexport) void test_speed(float a,float b) { fixture_left=a;fixture_right=b; }
__declspec(dllexport) void test_sensor_counts(int32_t a,int32_t b) { fixture_count_left=a;fixture_count_right=b; }
__declspec(dllexport) void test_encoder(uint32_t tick,uint32_t interval,int32_t a,int32_t b)
{ Project_EncoderSample(tick,interval,a,b); }

static float fixture_gyro;
static uint32_t fixture_gyro_seq,fixture_gyro_tick;
static int fixture_gyro_valid;
int Project_ReadGyro(float *raw,uint32_t *seq,uint32_t *tick)
{ *raw=fixture_gyro;*seq=fixture_gyro_seq;*tick=fixture_gyro_tick;return fixture_gyro_valid; }
__declspec(dllexport) void test_gyro(float raw,uint32_t seq,uint32_t tick,int valid)
{ fixture_gyro=raw;fixture_gyro_seq=seq;fixture_gyro_tick=tick;fixture_gyro_valid=valid; }
