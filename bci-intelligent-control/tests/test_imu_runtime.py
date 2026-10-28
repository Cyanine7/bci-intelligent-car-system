"""Exercise the actual IMU adapter and OLED function against a fake device.

No serial, Bluetooth, firmware upload, or physical sensor access.
"""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VCVARS = Path(r'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat')


def extract_function(source, name):
    match = re.search(r'^void ' + name + r'\([^;]*?\)\s*\{', source, re.MULTILINE)
    start = source.index('{', match.start())
    depth, end = 1, start + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


def main():
    assert VCVARS.exists()
    with tempfile.TemporaryDirectory(prefix='imu_runtime_') as directory:
        temp = Path(directory)
        source = (ROOT/'IMU/imu.c').read_bytes().decode('gbk')
        # Headers resolve against the real driver interfaces; only the I2C board is fake.
        (temp/'imu_under_test.c').write_text(source, encoding='utf-8')
        (temp/'bsp_siic.h').write_text('''#include "icm20948.h"
typedef struct { void (*init)(void); } IICInterface_t;
extern IICInterface_t User_sIICDev;
void icm20948_init_i2c(icm20948_device_t *device);
''')
        (temp/'delay.h').write_text('void delay_ms(unsigned int ms);\n')
        fixture = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <setjmp.h>
#include "imu_under_test.c"

static int calls, fail_at, reads, fifo_resets;
static uint8_t device_id = ICM_20948_WHOAMI;
static icm20948_status_e frame_status;
static icm_20948_DMP_data_t frame;
static uint32_t now, read_cost;
static void fake_init(void) {}
IICInterface_t User_sIICDev = {fake_init};
void icm20948_init_i2c(icm20948_device_t *device) { memset(device,0,sizeof(*device)); }
void delay_ms(unsigned int ms) { assert(ms==100); }
static icm20948_status_e transaction(void) { return ++calls==fail_at ? ICM_20948_STAT_ERR : ICM_20948_STAT_OK; }
icm20948_status_e icm20948_get_who_am_i(icm20948_device_t *d, uint8_t *id) { *id=device_id; return transaction(); }
icm20948_status_e icm20948_sw_reset(icm20948_device_t *d) { return transaction(); }
icm20948_status_e icm20948_sleep(icm20948_device_t *d, bool v) { assert(!v); return transaction(); }
icm20948_status_e icm20948_low_power(icm20948_device_t *d, bool v) { assert(!v); return transaction(); }
icm20948_status_e icm20948_int_pin_cfg(icm20948_device_t *d, icm20948_int_pin_cfg_t *w, icm20948_int_pin_cfg_t *r) {
    unsigned char zero[sizeof(*w)]={0}; assert(memcmp(w,zero,sizeof(*w))==0); return transaction();
}
icm20948_status_e icm20948_int_enable(icm20948_device_t *d, icm20948_int_enable_t *w, icm20948_int_enable_t *r) { return transaction(); }
icm20948_status_e icm20948_init_dmp_sensor_with_defaults(icm20948_device_t *d) { return transaction(); }
icm20948_status_e inv_icm20948_enable_dmp_sensor(icm20948_device_t *d, enum inv_icm20948_sensor s, int state) { assert(state==1); return transaction(); }
icm20948_status_e inv_icm20948_enable_dmp_sensor_int(icm20948_device_t *d, enum inv_icm20948_sensor s, int state) { return transaction(); }
icm20948_status_e inv_icm20948_set_dmp_sensor_period(icm20948_device_t *d, enum DMP_ODR_Registers r, uint16_t i) { assert(i==0); return transaction(); }
icm20948_status_e icm20948_enable_fifo(icm20948_device_t *d, bool v) { assert(v); return transaction(); }
icm20948_status_e icm20948_enable_dmp(icm20948_device_t *d, bool v) { assert(v); return transaction(); }
icm20948_status_e icm20948_reset_dmp(icm20948_device_t *d) { return transaction(); }
icm20948_status_e icm20948_reset_fifo(icm20948_device_t *d) { fifo_resets++; return transaction(); }
icm20948_status_e inv_icm20948_read_dmp_data(icm20948_device_t *d, icm_20948_DMP_data_t *data) { reads++; now+=read_cost; *data=frame; return frame_status; }
icm20948_status_e icm20948_set_bank(icm20948_device_t *d, uint8_t bank) { (void)d; (void)bank; return transaction(); }
icm20948_status_e icm20948_execute_r(icm20948_device_t *d, uint8_t reg, uint8_t *data, uint32_t len) {
    (void)d; (void)reg; assert(len==2); data[0]=0x10; data[1]=0x00; return transaction();
}

typedef unsigned char u8;
typedef struct {float Encoder,Motor_Pwm,Target;} Motor_parameter;
typedef struct { ATTITUDE_DATA_t attitude; float gyro_z_rad_s; uint32_t tick_ms; uint8_t has_sample,calibrated; uint32_t gyro_tick_ms,gyro_seq;uint8_t has_gyro; } SensorSnapshot;
static SensorSnapshot snap;
static float Voltage=11.5f;
static Motor_parameter MOTOR_A,MOTOR_B;
enum {Akm_Car=0,Diff_Car=1};
static int Car_Mode=Diff_Car,Servo=1500,EN=0;
static char lines[6][17];
static long myabs(long value) { return value<0 ? -value:value; }
static uint32_t getSysTickCnt(void) { return now; }
static void sensor_get_snapshot(SensorSnapshot *result) { *result=snap; }
static int Turn_Off(float v) { return 1; }
static void OLED_ShowString(int x,int y,const u8 *s) {
    size_t len=strlen((const char*)s), offset=(size_t)x/8;
    assert(y>=0 && y<=50 && offset+len<=16);
    memcpy(lines[y/10]+offset,s,len);
}
static void OLED_ShowNumber(int x,int y,unsigned long n,int len,int size) {}
static void OLED_Refresh_Gram(void) {}
typedef uint32_t TickType_t;
#define RATE_100_HZ 100
#define F2T(x) (1000/(x))
#define taskENTER_CRITICAL() ((void)0)
#define taskEXIT_CRITICAL() ((void)0)
static ATTITUDE_DATA_t imu_attitude;
static SensorSnapshot snapshot;
static jmp_buf finish_poll;
static int poll_index, slow_poll;
static TickType_t xTaskGetTickCount(void) { return now; }
static void vTaskDelayUntil(TickType_t *last, uint32_t step) {
    assert(step==10);
    if (poll_index>0) {
        assert(*last==(uint32_t)(poll_index*(slow_poll?31:10)));
        /* Direct gyro must be published before the simulated DMP read cost. */
        assert(snapshot.gyro_tick_ms==(uint32_t)(poll_index*(slow_poll?31:10)-(slow_poll?21:0)));
    }
    if (++poll_index>3) longjmp(finish_poll,1);
    *last+=step; now=*last;
}
'''
        show = (ROOT/'BALANCE/show.c').read_bytes().decode('gbk')
        sensor = (ROOT/'BALANCE/sensor.c').read_text(encoding='utf-8')
        fixture += '\n' + extract_function(sensor, 'sensor_task')
        fixture += '\n' + extract_function(show, 'oled_show')
        fixture += r'''
int main(void) {
    ATTITUDE_DATA_t a={11,22,33};
    device_id=0; calls=reads=0; fail_at=0;
    assert(ICM20948_DMP_Init()==1 && imu_init_status==1);
    ICM20948_DMP_Update(&a); assert(reads==0 && a.yaw==33);
    device_id=ICM_20948_WHOAMI;
    for(int i=1;i<=16;i++) {
        fail_at=i; calls=reads=0;
        assert(ICM20948_DMP_Init()==1 && imu_init_status==1);
        ICM20948_DMP_Update(&a); assert(reads==0);
    }
    fail_at=0; calls=0;
    assert(ICM20948_DMP_Init()==0 && imu_init_status==0);
    memset(&frame,0,sizeof(frame));
    frame.header=DMP_header_bitmap_Quat6|DMP_header_bitmap_Gyro;
    frame.Quat6.Data.Q3=759250125; frame.Raw_Gyro.Data.Z=4096;
    frame_status=ICM_20948_STAT_ERR; reads=0;
    int resets_before=fifo_resets;
    ICM20948_DMP_Update(&a);
    assert(a.yaw==33 && imu_sample_seq==0 && fifo_resets==resets_before+1);
    frame_status=ICM_20948_STAT_FIFO_INCOMPLETE_DATA;
    ICM20948_DMP_Update(&a); assert(a.yaw==33 && imu_sample_seq==0);
    frame_status=ICM_20948_STAT_OK;
    ICM20948_DMP_Update(&a);
    assert(fabsf(a.yaw-90)<0.01f && fabsf(imu_data.gyro.z-4.36331f)<0.0001f);
    assert(imu_gyro_seq==1);
    uint32_t old_attitude_seq=imu_sample_seq;
    frame.header=DMP_header_bitmap_Gyro;
    ICM20948_DMP_Update(&a);
    assert(imu_gyro_seq==2 && imu_sample_seq==old_attitude_seq);
    frame.header=DMP_header_bitmap_Quat6;
    ICM20948_DMP_Update(&a);
    assert(imu_gyro_seq==2 && imu_sample_seq==old_attitude_seq+1);
    frame.header=DMP_header_bitmap_Quat6|DMP_header_bitmap_Gyro;
    for(int i=0;i<60;i++) ICM20948_DMP_Update(&a);
    assert(calib_done && fabsf(a.yaw)<0.001f);
    IMU_Calib_Window_Reset(); assert(!calib_done);
    frame_status=ICM_20948_STAT_FIFO_MORE_DATA_AVAIL; reads=0;
    ICM20948_DMP_Update(&a); assert(reads==3);
    memset(lines,' ',sizeof(lines));
    snap.has_sample=1; snap.calibrated=1; snap.attitude.yaw=-123.0f;
    snap.gyro_z_rad_s=4.363f; snap.tick_ms=100; now=100;
    oled_show();
    assert(strstr(lines[0],"IMU:OK") && strstr(lines[3],"-123.00")!=NULL);
    now=301; oled_show(); assert(strstr(lines[0],"IMU:OLD") && strstr(lines[3],"---"));
    imu_init_status=1; oled_show(); assert(strstr(lines[0],"IMU:ERR") && strstr(lines[4],"---"));
    imu_init_status=0; dmp_ready=1; frame_status=ICM_20948_STAT_FIFO_NO_DATA_AVAIL;
    for (slow_poll=0;slow_poll<=1;slow_poll++) {
        poll_index=0; now=0; read_cost=slow_poll?21:3;
        if (!setjmp(finish_poll)) sensor_task(NULL);
    }
    assert(snapshot.has_gyro && snapshot.gyro_seq>0 && fabsf(snapshot.gyro_z_rad_s-4.36331f)<0.0001f);
    printf("IMU runtime: wrong ID, 16 init failure points, FIFO rejection/bound, yaw/gyro, calibration, OLED freshness, direct gyro read, 100Hz schedule/no catch-up passed\n");
}
'''
        # Each captured OLED line must be independently NUL-terminated.
        fixture = fixture.replace("memset(lines,' ',sizeof(lines));", "memset(lines,' ',sizeof(lines)); for(int j=0;j<6;j++) lines[j][16]=0;")
        (temp/'fixture.c').write_text(fixture,encoding='utf-8')
        cmd = f'"{VCVARS}" >nul && cl.exe /nologo /utf-8 /W3 /wd4101 /wd4100 /I"{ROOT / "IMU"}" fixture.c /Fe:fixture.exe'
        (temp/'build.cmd').write_text(cmd+'\n',encoding='utf-8')
        built = subprocess.run(['cmd.exe','/d','/c','build.cmd'],cwd=temp,capture_output=True,text=True,errors='replace')
        assert built.returncode==0, built.stdout+built.stderr
        ran = subprocess.run([str(temp/'fixture.exe')],cwd=temp,capture_output=True,text=True,errors='replace')
        assert ran.returncode==0, ran.stdout+ran.stderr
        print(ran.stdout.strip())


if __name__=='__main__':
    main()
