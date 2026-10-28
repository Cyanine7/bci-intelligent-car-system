#include "project_link.h"
#include "speed_experiment.h"
#include "yaw_control.h"
#include <string.h>

/* Portable protocol/control core; only the board adapter touches registers. */
#define RX_SIZE 512
#define TX_SIZE 1024
#define MAX_PAYLOAD 64
#define FRAME_SIZE (16 + MAX_PAYLOAD)

static volatile uint16_t rx_head, rx_tail, tx_head, tx_tail;
static uint8_t rx[RX_SIZE], tx[TX_SIZE];
static volatile uint32_t rx_dropped;
static uint32_t tx_dropped;
static volatile uint8_t overflow;
static uint8_t candidate[FRAME_SIZE];
static uint16_t used;
static uint32_t candidate_started;
static uint8_t reset_count;
static uint32_t reset_started;
static uint8_t binary_seen;
static uint32_t last_binary_byte;
static uint32_t session, last_seq, report_seq, revision;
/* Never recreate a prior session in the same boot. Exhaustion fails closed. */
static uint32_t session_history[128];
static uint16_t session_count;
static uint32_t gains[4];
static uint8_t armed, mode, stop_reason, active;
static int targets[2], outputs[2];
static float previous_error[2], pi_output[2];
static uint32_t end_tick, idle_end_tick, next_report;
static uint8_t local_ok;
/* Session negotiation changes only telemetry shape, never motion limits. */
static uint8_t profile_enabled, profile_index=255, profile_status;
static uint32_t profile_seq, profile_start_tick, profile_switch_tick;
static uint32_t profile_actual_switch_tick;
static int profile_second[2];
/* Boot-lifetime modulo-2^64 totals, owned exclusively by the control task.
   HELLO/ARM/STOP and display/recording changes never clear these counters. */
static uint64_t encoder_total[2];
static uint32_t encoder_epoch, encoder_tick, encoder_sample_seq;
static uint32_t encoder_interval, encoder_delayed, encoder_report_seq;
static uint8_t encoder_initialized, encoder_enabled;
static uint32_t control_now;

void Project_EncoderSample(uint32_t now, uint32_t interval,
                           int32_t left, int32_t right)
{
    control_now=now;
    if(!encoder_initialized) { encoder_epoch=now; encoder_initialized=1; }
    encoder_total[0]+=(uint64_t)(int64_t)left;
    encoder_total[1]+=(uint64_t)(int64_t)right;
    encoder_tick=now; encoder_interval=interval; encoder_sample_seq++;
    if(!interval || interval>30U) encoder_delayed++;
}

static void clear_profile(void)
{
    profile_seq=profile_start_tick=profile_switch_tick=profile_actual_switch_tick=0;
    profile_index=255; profile_status=0;
    profile_second[0]=profile_second[1]=0;
}

static uint16_t read16(const uint8_t *p)
{ return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }
static uint32_t read32(const uint8_t *p)
{ return (uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24); }
static void write16(uint8_t *p, uint16_t n)
{ p[0]=(uint8_t)n; p[1]=(uint8_t)(n>>8); }
static void write32(uint8_t *p, uint32_t n)
{ p[0]=(uint8_t)n; p[1]=(uint8_t)(n>>8); p[2]=(uint8_t)(n>>16); p[3]=(uint8_t)(n>>24); }
static void write64(uint8_t *p, uint64_t n)
{ write32(p,(uint32_t)n); write32(p+4,(uint32_t)(n>>32)); }
static uint16_t crc16(const uint8_t *p, unsigned length)
{
    uint16_t crc=0xffff;
    unsigned i;
    while(length--) {
        crc^=(uint16_t)(*p++)<<8;
        for(i=0;i<8;i++) crc=(crc&0x8000)?(uint16_t)((crc<<1)^0x1021):(uint16_t)(crc<<1);
    }
    return crc;
}

void Project_Stop(uint8_t reason)
{
    SpeedExperiment_Stop(control_now,reason);
    YawExperiment_Stop(control_now,reason);
    if(profile_status==1) profile_status=2;
    profile_second[0]=profile_second[1]=0;
    armed=mode=active=0;
    targets[0]=targets[1]=outputs[0]=outputs[1]=0;
    previous_error[0]=previous_error[1]=pi_output[0]=pi_output[1]=0;
    stop_reason=reason;
}
void Project_GetOutput(int *left, int *right)
{ *left=outputs[0]; *right=outputs[1]; }
void Project_RxByte(uint8_t byte)
{
    uint16_t next=(uint16_t)((rx_head+1)&(RX_SIZE-1));
    if(next==rx_tail) { rx_dropped++; overflow=1; return; }
    rx[rx_head]=byte;
    rx_head=next;
}
void Project_RxError(void)
{ rx_dropped++; overflow=1; }
int Project_TxByte(uint8_t *byte)
{
    if(tx_tail==tx_head) return 0;
    *byte=tx[tx_tail]; tx_tail=(uint16_t)((tx_tail+1)&(TX_SIZE-1)); return 1;
}
static void send_frame(uint8_t type, uint32_t identity, uint32_t seq,
                       const uint8_t *payload, uint16_t length)
{
    uint8_t frame[FRAME_SIZE];
    uint16_t size=(uint16_t)(length+16), free_bytes, i;
    uint32_t lock;
    frame[0]=0xa5; frame[1]=0x5a; frame[2]=1; frame[3]=type;
    write16(frame+4,length); write32(frame+6,identity); write32(frame+10,seq);
    if(length) memcpy(frame+14,payload,length);
    write16(frame+14+length,crc16(frame+2,(unsigned)(length+12)));
    lock=Project_IrqLock();
    free_bytes=(uint16_t)((tx_tail-tx_head-1)&(TX_SIZE-1));
    if(free_bytes<size) { tx_dropped++; overflow=1; Project_IrqUnlock(lock); return; }
    for(i=0;i<size;i++) { tx[tx_head]=frame[i]; tx_head=(uint16_t)((tx_head+1)&(TX_SIZE-1)); }
    Project_TxKick();
    Project_IrqUnlock(lock);
}
static void acknowledge(uint8_t type, uint8_t status, uint32_t identity, uint32_t seq)
{ uint8_t p[2]; p[0]=type; p[1]=status; send_frame(0x82,identity,seq,p,2); }
void Project_SpeedReply(uint8_t type,uint32_t seq,const uint8_t *p,uint16_t length)
{ send_frame(type,session,seq,p,length); }
static void parameters(uint32_t identity, uint32_t seq)
{
    uint8_t p[20]; unsigned i;
    write32(p,revision);
    for(i=0;i<4;i++) write32(p+4+4*i,gains[i]);
    send_frame(0x84,identity,seq,p,20);
}
static void capabilities(uint32_t seq)
{
    uint8_t p[13], car, enable;
    int32_t a,b; float x,y,v;
    Project_ReadSensors(&a,&b,&x,&y,&v,&car,&enable);
    write16(p,15); write16(p+2,PROJECT_PWM_LIMIT); write16(p+4,PROJECT_SPEED_LIMIT);
    write16(p+6,PROJECT_MAX_DURATION); write16(p+8,100); write16(p+10,20); p[12]=car;
    send_frame(0x81,session,seq,p,13);
}
static void profile_capabilities(uint32_t seq)
{
    uint8_t p[12];
    write16(p,3); p[2]=PROJECT_PROFILE_VERSION; p[3]=PROJECT_PROFILE_SEGMENTS;
    write16(p+4,PROJECT_PWM_LIMIT); write16(p+6,PROJECT_MAX_DURATION);
    write16(p+8,100); write16(p+10,20);
    send_frame(0x85,session,seq,p,12);
}
static void encoder_capabilities(uint32_t seq)
{
    uint8_t p[20];
    uint32_t counts,diameter,spacing;
    Project_ReadEncoderGeometry(&counts,&diameter,&spacing);
    p[0]=1; p[1]=64; write16(p+2,100); write16(p+4,20); write16(p+6,0);
    write32(p+8,counts); write32(p+12,diameter); write32(p+16,spacing);
    send_frame(0x88,session,seq,p,20);
}
static void encoder_state(void)
{
    uint8_t p[40];
    write16(p,1); write16(p+2,encoder_initialized?1:0);
    write32(p+4,encoder_epoch); write32(p+8,encoder_tick);
    write32(p+12,encoder_sample_seq);
    write64(p+16,encoder_total[0]); write64(p+24,encoder_total[1]);
    write32(p+32,encoder_interval); write32(p+36,encoder_delayed);
    /* Independent report sequence: do not create false STATE sequence gaps. */
    send_frame(0x87,session,++encoder_report_seq,p,40);
}
static void state(uint32_t now)
{
    uint8_t p[64], car, enable;
    int32_t a,b; float x,y,v;
    int battery;
    Project_ReadSensors(&a,&b,&x,&y,&v,&car,&enable);
    battery=(int)(((v-10.0f)/2.6f)*100.0f);
    write32(p,now); write32(p+4,last_seq); write32(p+8,(uint32_t)a); write32(p+12,(uint32_t)b);
    if(x>32.767f)x=32.767f; if(x< -32.768f)x= -32.768f;
    if(y>32.767f)y=32.767f; if(y< -32.768f)y= -32.768f;
    write16(p+16,(uint16_t)(int16_t)(x*1000)); write16(p+18,(uint16_t)(int16_t)(y*1000));
    write16(p+20,(uint16_t)(int16_t)outputs[0]); write16(p+22,(uint16_t)(int16_t)outputs[1]);
    write16(p+24,(uint16_t)(v*1000)); write16(p+26,(uint16_t)(int16_t)battery);
    p[28]=mode; p[29]=armed; p[30]=stop_reason; p[31]=enable;
    write32(p+32,rx_dropped); write32(p+36,tx_dropped); write32(p+40,revision);
    /* Reserved bytes 44..47 are zero, retained for v1 fixed payload compatibility. */
    write32(p+44,0);
    if(profile_enabled) {
        write32(p+48,profile_seq); write32(p+52,profile_start_tick);
        write32(p+56,profile_actual_switch_tick);
        p[60]=profile_index; p[61]=profile_status; write16(p+62,0);
        send_frame(0x86,session,++report_seq,p,64);
    } else send_frame(0x83,session,++report_seq,p,48);
}
static void dispatch(uint32_t now)
{
    uint8_t type=candidate[3], status=0;
    uint16_t length=read16(candidate+4), duration, first_ms;
    uint32_t identity=read32(candidate+6), seq=read32(candidate+10), deadline, values[4];
    const uint8_t *p=candidate+14;
    int left,right,left_second,right_second; unsigned i;
    if(type==4 && length==0) {
        Project_Stop(1);
        if(identity==session && seq>last_seq) last_seq=seq;
        acknowledge(type,0,identity,seq); return;
    }
    if(type==1) {
        if(length || !identity || seq!=1) status=1;
        else {
            Project_Stop(0);
            for(i=0;i<session_count;i++) if(session_history[i]==identity) status=3;
            if(session_count==128) status=1;
            if(!status) {
                session_history[session_count++]=identity;
                session=identity; last_seq=seq; report_seq=0;
                encoder_enabled=0; encoder_report_seq=0;
                SpeedExperiment_ResetSession();
                YawExperiment_ResetSession();
                profile_enabled=0; clear_profile(); capabilities(seq);
            }
        }
        acknowledge(type,status,identity,seq); return;
    }
    if(!session || identity!=session) { acknowledge(type,2,identity,seq); return; }
    if(seq<=last_seq) { acknowledge(type,3,identity,seq); return; }
    /* Consume sequence even on a rejection. Retrying cannot renew motion. */
    last_seq=seq;
    if(type==11 || type==12 || type==13 || type==15 || type==16 || type==18) {
        status=YawExperiment_Busy()?4:SpeedExperiment_Command(type,p,length,now,seq,armed);
        acknowledge(type,status,identity,seq); return;
    }
    if(type>=19 && type<=27 && type!=24) {
        status=SpeedExperiment_Busy()?4:YawExperiment_Command(type,p,length,now,seq,armed);
        acknowledge(type,status,identity,seq); return;
    }
    switch(type) {
    case 2:
        if(length!=1 || (p[0]!=1 && p[0]!=2 && p[0]!=3 && p[0]!=4 && p[0]!=5)) { status=1; break; }
        if(SpeedExperiment_Busy() || YawExperiment_Busy()) { status=4; break; }
        if(p[0]==5 && !YawExperiment_ArmAllowed(now)) { status=7; break; }
        if((p[0]==3 || p[0]==4) && !SpeedExperiment_Negotiated()) { status=9; break; }
        if((p[0]==3 || p[0]==4) && !SpeedExperiment_ArmAllowed(p[0])) { status=7; break; }
        if(!local_ok) { status=5; break; }
        if(armed) { status=4; break; }
        if(p[0]==2 && (!(gains[0]||gains[1]) || !(gains[2]||gains[3]))) { status=7; break; }
        Project_Stop(0); clear_profile(); mode=p[0]; armed=1; idle_end_tick=now+5000; break;
    case 24:
        if(!local_ok) { status=5; break; }
        if(!armed || active || mode!=5) { status=4; break; }
        status=YawExperiment_Start(p,length,now,seq,&end_tick);if(!status)active=1;break;
    case 14: case 17:
        if(!SpeedExperiment_Negotiated()) { status=9; break; }
        if(!local_ok) { status=5; break; }
        if(!armed || active || mode!=(type==14?3:4)) { status=4; break; }
        status=SpeedExperiment_Start(type,p,length,now,seq,&end_tick);
        if(!status)active=1;
        break;
    case 3: case 5:
        if(length!=10) { status=1; break; }
        left=(int16_t)read16(p); right=(int16_t)read16(p+2); duration=read16(p+4); deadline=read32(p+6);
        if(duration<1 || duration>(type==3?PROJECT_MAX_DURATION:PROJECT_SPEED_MAX_DURATION) ||
           left< -(type==3?PROJECT_PWM_LIMIT:PROJECT_SPEED_LIMIT) || left>(type==3?PROJECT_PWM_LIMIT:PROJECT_SPEED_LIMIT) ||
           right< -(type==3?PROJECT_PWM_LIMIT:PROJECT_SPEED_LIMIT) || right>(type==3?PROJECT_PWM_LIMIT:PROJECT_SPEED_LIMIT)) { status=1; break; }
        if(!local_ok) { status=5; break; }
        if(!armed || active || mode!=(type==3?1:(type==14?3:2))) { status=4; break; }
        if((int32_t)(deadline-now)<=0 || (uint32_t)(deadline-now)>(uint32_t)(type==3?PROJECT_MAX_DURATION:PROJECT_SPEED_MAX_DURATION)) { status=8; break; }
        targets[0]=left; targets[1]=right; active=1;
        end_tick=now+duration;
        if((int32_t)(deadline-end_tick)<0) end_tick=deadline;
        break;
    case 8:
        if(!profile_enabled) { status=9; break; }
        if(length!=20 || p[0]!=PROJECT_PROFILE_VERSION || p[1]!=PROJECT_PROFILE_SEGMENTS || read16(p+2)) { status=1; break; }
        left=(int16_t)read16(p+4); right=(int16_t)read16(p+6);
        left_second=(int16_t)read16(p+8); right_second=(int16_t)read16(p+10);
        first_ms=read16(p+12); duration=read16(p+14); deadline=read32(p+16);
        if(duration<2 || duration>PROJECT_MAX_DURATION || first_ms<1 || first_ms>=duration ||
           left< -PROJECT_PWM_LIMIT || left>PROJECT_PWM_LIMIT || right< -PROJECT_PWM_LIMIT || right>PROJECT_PWM_LIMIT ||
           left_second< -PROJECT_PWM_LIMIT || left_second>PROJECT_PWM_LIMIT || right_second< -PROJECT_PWM_LIMIT || right_second>PROJECT_PWM_LIMIT ||
           (left<0 && left_second>0) || (left>0 && left_second<0) ||
           (right<0 && right_second>0) || (right>0 && right_second<0)) { status=1; break; }
        if(!local_ok) { status=5; break; }
        if(!armed || active || mode!=1) { status=4; break; }
        if((int32_t)(deadline-now)<=0 || (uint32_t)(deadline-now)>PROJECT_MAX_DURATION) { status=8; break; }
        targets[0]=left; targets[1]=right;
        profile_second[0]=left_second; profile_second[1]=right_second;
        profile_seq=seq; profile_start_tick=now; profile_switch_tick=now+first_ms;
        profile_actual_switch_tick=0; profile_index=255; profile_status=1; active=1;
        end_tick=now+duration;
        if((int32_t)(deadline-end_tick)<0) end_tick=deadline;
        break;
    case 9:
        if(length) { status=1; break; }
        if(armed || active) { status=4; break; }
        profile_enabled=1; profile_capabilities(seq); break;
    case 10:
        if(length) { status=1; break; }
        if(armed || active) { status=4; break; }
        encoder_enabled=1; encoder_capabilities(seq); break;
    case 6:
        if(length) status=1; else parameters(identity,seq); break;
    case 7:
        if(length!=16) { status=1; break; }
        if(armed) { status=6; break; }
        for(i=0;i<4;i++) values[i]=read32(p+4*i);
        for(i=0;i<4;i++) if(values[i]>2000000) status=1;
        if(!status) { memcpy(gains,values,sizeof(gains)); revision++; parameters(identity,seq); }
        break;
    case 4: status=1; break;
    default: status=9; break;
    }
    acknowledge(type,status,identity,seq);
}
/* Preserve only a potential binary header on corruption; never scan a failed
   binary candidate for the bootloader's ASCII reset command. */
static void resync(void)
{
    uint16_t i;
    for(i=1;i+1<used;i++) if(candidate[i]==0xa5 && candidate[i+1]==0x5a) {
        memmove(candidate,candidate+i,used-i); used=(uint16_t)(used-i); return;
    }
    if(used && candidate[used-1]==0xa5) { candidate[0]=0xa5; used=1; }
    else used=0;
}
static void consume(uint8_t byte, uint32_t now)
{
    static const char reset_word[]="reset";
    uint16_t length;
    if(!used) {
        if(byte==0xa5) { candidate[used++]=byte; candidate_started=now; reset_count=0; binary_seen=1; last_binary_byte=now; return; }
        if(binary_seen && (uint32_t)(now-last_binary_byte)<=100) { reset_count=0; return; }
        if(reset_count && (uint32_t)(now-reset_started)>100) reset_count=0;
        if(byte==(uint8_t)reset_word[reset_count]) {
            if(!reset_count) reset_started=now;
            if(++reset_count==5) { reset_count=0; Project_Stop(1); Project_ResetBoard(); }
        } else { reset_count=(byte=='r')?1:0; reset_started=now; }
        return;
    }
    last_binary_byte=now;
    if(used==1 && byte!=0x5a) { used=0; reset_count=0; if(byte==0xa5) {candidate[used++]=byte;candidate_started=now;} return; }
    candidate[used++]=byte;
    while(used>=6) {
        if(candidate[0]!=0xa5 || candidate[1]!=0x5a) { resync(); continue; }
        length=read16(candidate+4);
        if(candidate[2]!=1 || length>MAX_PAYLOAD) { resync(); continue; }
        if(used<(uint16_t)(length+16)) return;
        if(read16(candidate+14+length)==crc16(candidate+2,(unsigned)(length+12))) {
            dispatch(now);
            /* Usually no remainder; resync may leave multiple complete frames. */
            used=(uint16_t)(used-length-16);
            if(used) memmove(candidate,candidate+length+16,used);
        } else resync();
    }
}

void Project_ControlTick(uint32_t now, int local_allowed)
{
    unsigned processed=0, i;
    uint8_t byte, car, enable;
    int32_t a,b; float speed[2], v, error, increment, value;
    uint32_t lock;
    control_now = now;
    local_ok = (uint8_t)(local_allowed != 0);

    if(!local_ok && armed) Project_Stop(3);

    if(armed && ((active && (int32_t)(now-end_tick)>=0) || (!active && (int32_t)(now-idle_end_tick)>=0)))
        Project_Stop(active?2:5);

    if(overflow) {
        lock = Project_IrqLock(); overflow = 0; rx_tail = rx_head; Project_IrqUnlock(lock);
        used = reset_count = 0; Project_Stop(4);
        local_ok = 0;
    }

    if(used && (uint32_t)(now-candidate_started)>100) { used=0; reset_count=0; }

    while(rx_tail!=rx_head && processed++<RX_SIZE) {
        byte=rx[rx_tail]; rx_tail=(uint16_t)((rx_tail+1)&(RX_SIZE-1)); consume(byte,now);
    }

    if(overflow) Project_Stop(4);

    /* PWM命令保持PWM语义；只有旧mode2进入旧PI，新mode3/4由实验层处理。 */
    if(armed && active && !overflow && mode==1) {
        if(profile_status==1) {
            if(profile_index==255) profile_index=0;
            if(profile_index==0 && (int32_t)(now-profile_switch_tick)>=0) {
                targets[0]=profile_second[0]; targets[1]=profile_second[1];
                profile_index=1; profile_actual_switch_tick=now;
            }
        }
        outputs[0]=targets[0]; outputs[1]=targets[1];
    }
    if(armed && active && !overflow && mode==2) {
        Project_ReadSensors(&a, &b, &speed[0], &speed[1], &v, &car, &enable);
        for(i=0; i<2; i++) {
            if(!targets[i]) { 
                outputs[i]=0; 
                pi_output[i] = previous_error[i] = 0; 
                continue; 
            }

            error = targets[i]/1000.0f - speed[i];
            increment = (gains[2*i]/100.0f)*(error-previous_error[i]) + (gains[2*i+1]/100.0f)*error;
            value = pi_output[i] + increment;

            if(value > PROJECT_PI_PWM_LIMIT) 
            value = PROJECT_PI_PWM_LIMIT;
            if(value < -PROJECT_PI_PWM_LIMIT) 
            value = -PROJECT_PI_PWM_LIMIT;

            pi_output[i] = value; previous_error[i] = error; outputs[i] = (int)value;
        }
    }

    if(!SpeedExperiment_Tick(now,encoder_interval,armed && active && (mode==3 || mode==4),
        &outputs[0],&outputs[1]))Project_Stop(6);

    if(!YawExperiment_Tick(now,encoder_interval,armed && active && mode==5,
        &outputs[0],&outputs[1]))Project_Stop(6);
    SpeedExperiment_Pump();
    YawExperiment_Pump();

    if((int32_t)(now-next_report)>=0) {
        next_report = now+50; state(now);
        if(encoder_enabled) encoder_state();
        SpeedExperiment_Report();
        YawExperiment_Report();
    }

    if(overflow) Project_Stop(4);
}
