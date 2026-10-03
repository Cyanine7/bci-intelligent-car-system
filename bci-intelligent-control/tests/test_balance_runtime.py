"""Execute actual Balance_task/Get_Velocity source against a fake board and RTOS.

Run with: ../上位机/.venv/Scripts/python.exe tests/test_balance_runtime.py
Requires the installed Visual Studio 2022 x64 C toolchain; no device access.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
VCVARS = Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat")


def extract_function(source: str, name: str) -> str:
    # Match the definition, never an earlier call inside Balance_task.
    match = re.search(r"^void " + re.escape(name) + r"\([^;]*?\)\s*\{", source, re.MULTILINE)
    if match is None:
        raise AssertionError(f"Function not found: {name}")
    brace = source.index("{", match.start())
    depth, end = 1, brace + 1
    while depth:
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
        end += 1
    return source[match.start():end]


HEADER = r'''

#include <stdint.h>
#include <stdio.h>
#include <assert.h>
#include <setjmp.h>
#include <math.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef struct {int A,B,C,D;} Encoder;
typedef struct {float Encoder,Motor_Pwm,Target;} Motor_parameter;
enum {Akm_Car=0,Diff_Car=1};
#define RATE_100_HZ 100
#define F2T(n) (1000/(n))
#define SERVO_INIT 1500
static uint32_t Encoder_sample_period_ms=10;
int Time_count, Buzzer_count=25, Servo=1500;
uint8_t Car_Mode=Diff_Car,Flag_Stop;
float Voltage=12,Wheel_perimeter=1,Encoder_precision=1000;
Encoder OriginalEncoder;
Motor_parameter MOTOR_A,MOTOR_B;
static uint32_t now;
static int ix=-1, count, stop_called, pwm_calls, next_left, next_right;
static int check_schedule=1;
static jmp_buf done;
struct sample {uint32_t time; int counts; uint8_t key; int allowed; int reason;};
static struct sample samples[10];
uint32_t getSysTickCnt(void) {return now;}
int Read_Encoder(uint8_t timer) {
    if(ix<0) return 0;
    if(timer==2 || timer==4) return samples[ix].counts;
    return -samples[ix].counts;
}
uint8_t click_N_Double(uint8_t time) {(void)time; return samples[ix].key;}
void Project_Stop(uint8_t reason) {stop_called=reason;next_left=next_right=0;}
void Project_ControlTick(uint32_t time,int allowed) {
    float expected=(float)samples[ix].counts/(float)(Encoder_sample_period_ms);
    assert(time==samples[ix].time);
    assert(allowed==samples[ix].allowed);
    assert(stop_called==samples[ix].reason);
    assert(fabsf(MOTOR_A.Encoder-expected)<0.00001f);
    assert(fabsf(MOTOR_B.Encoder-expected)<0.00001f);
    assert(OriginalEncoder.C==samples[ix].counts);
    assert(OriginalEncoder.D==-samples[ix].counts);
    next_left=allowed?100:0;next_right=allowed?200:0;
}
void Project_GetOutput(int *left,int *right) {*left=next_left;*right=next_right;}
void Set_Pwm(int left,int right,int servo) {
    pwm_calls++;
    if(ix<0) {assert(left==0&&right==0&&servo==1500);return;}
    if(samples[ix].allowed) {assert(left==-100&&right==200&&servo==0);}
    else {assert(left==0&&right==0&&servo==1500);}
}
void vTaskDelayUntil(uint32_t *last,uint32_t interval) {
    if(check_schedule && ix>=0 && samples[ix].time!=0 && *last!=samples[ix].time) {fprintf(stderr,"ix=%d last=%u expected=%u\n",ix,*last,samples[ix].time);assert(*last==samples[ix].time);}
    *last+=interval;
    if(++ix==count) longjmp(done,1);
    now=samples[ix].time;stop_called=0;
}
uint8_t Turn_Off(int voltage) {(void)voltage;return 0;}
void Get_Velocity_From_Encoder(void);
'''

FOOTER = r'''

static void run(uint32_t initial,const struct sample *values,int n) {
    int i;
    for(i=0;i<n;i++)samples[i]=values[i];
    now=initial;ix=-1;count=n;pwm_calls=0;Time_count=0;Buzzer_count=25;
    if(setjmp(done)==0) Balance_task(0);
    assert(pwm_calls==n+1);
}
int main(void) {
    const struct sample timing[]={
        {10,10,0,1,0},{30,20,0,1,0},{40,10,0,1,0},
        {70,30,0,1,0},{101,31,0,0,3},{111,10,0,1,0}};
    const struct sample keys[]={
        {10,10,1,0,1},{20,10,2,1,0}};
    const struct sample rollover[]={
        {0xfffffffau,10,0,1,0},{4,10,0,1,0},{14,10,0,1,0}};
    const struct sample zero[]={ {0,0,0,1,0},{10,10,0,1,0}};
    run(0,timing,6);
    run(0,keys,2);assert(Buzzer_count==0);
    run(0xfffffff0u,rollover,3);
    check_schedule=0;run(0,zero,2);
    puts("PASS: 13 actual-source runtime samples; 10/20/30/31 ms, zero, tick rollover, key stop, buzzer, PWM polarity.");
    return 0;
}
'''


def main() -> int:
    # MSVC may use a different output code page from the invoking Python.
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
    if os.name != "nt" or not VCVARS.is_file():
        raise RuntimeError("This verification requires the installed Visual Studio 2022 x64 toolchain.")
    source = (PROJECT / "BALANCE" / "balance.c").read_bytes().decode("gbk")
    # Keep failed/successful generated C and executable for debugging; no deletion.
    directory = Path(tempfile.mkdtemp(prefix="bci-balance-"))
    generated = HEADER + extract_function(source, "Balance_task") + "\n" + extract_function(source, "Get_Velocity_From_Encoder") + "\n" + FOOTER
    (directory / "balance_runtime_host.c").write_bytes(generated.encode("utf-8"))
    batch = (
        f'@call "{VCVARS}" >nul\r\n'
        '@cl /nologo /W4 /Od /utf-8 /TC balance_runtime_host.c /Fe:balance_runtime_host.exe\r\n'
        '@if errorlevel 1 exit /b 1\r\n'
        '@balance_runtime_host.exe\r\n'
    )
    (directory / "run_tests.cmd").write_bytes(batch.encode("ascii"))
    result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "run_tests.cmd"], cwd=directory, capture_output=True)
    for output in (result.stdout, result.stderr):
        try:
            decoded = output.decode("utf-8")
        except UnicodeDecodeError:
            decoded = output.decode("gbk", errors="replace")
        print(decoded.replace("\r\n", "\n"), end="")
    print(f"Generated fixture: {directory}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
