#include "../BALANCE/project_link.c"

static float fixture_left, fixture_right, fixture_voltage=12.0f;
static int board_resets;
void Project_TxKick(void) {}
void Project_ResetBoard(void) { board_resets++; }
uint32_t Project_IrqLock(void) { return 0; }
void Project_IrqUnlock(uint32_t p) { (void)p; }
void Project_ReadSensors(int32_t *a,int32_t *b,float *x,float *y,float *v,uint8_t *car,uint8_t *enable)
{ *a=12; *b= -13; *x=fixture_left; *y=fixture_right; *v=fixture_voltage; *car=1; *enable=1; }

__declspec(dllexport) void test_reset(void)
{
    rx_head=rx_tail=tx_head=tx_tail=0; rx_dropped=tx_dropped=overflow=0;
    used=reset_count=binary_seen=0; candidate_started=reset_started=last_binary_byte=0;
    session=last_seq=report_seq=revision=0; memset(gains,0,sizeof(gains));
    session_count=0; memset(session_history,0,sizeof(session_history));
    end_tick=idle_end_tick=next_report=0; local_ok=0;
    fixture_left=fixture_right=0; fixture_voltage=12; board_resets=0;
    Project_Stop(0);
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
