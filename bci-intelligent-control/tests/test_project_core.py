"""Run the actual portable MCU C core via a native DLL; no hardware is touched.

Compile project_core_fixture.c with MSVC /LD, then pass the DLL path here.
Wire encoding and CRC verification use independent Python struct/binascii.
"""
import binascii
import ctypes as c
from pathlib import Path
import struct
import re
import sys
import unittest

dll = c.CDLL(str(Path(sys.argv.pop(1)).resolve()))
dll.test_rx.argtypes = (c.c_void_p, c.c_uint)
dll.test_tick.argtypes = (c.c_uint32, c.c_int)
dll.test_output.argtypes = (c.POINTER(c.c_int), c.POINTER(c.c_int))
dll.test_drain.argtypes = (c.c_void_p, c.c_uint)
dll.test_drain.restype = c.c_uint
dll.test_speed.argtypes = (c.c_float, c.c_float)
dll.test_encoder.argtypes = (c.c_uint32, c.c_uint32, c.c_int32, c.c_int32)

def frame(t, seq, payload=b'', session=1234):
    body = struct.pack('<BBHII', 1, t, len(payload), session, seq) + payload
    return b'\xa5\x5a' + body + struct.pack('<H', binascii.crc_hqx(body, 0xffff))

class CoreTests(unittest.TestCase):
    def setUp(self):
        dll.test_reset()

    def output(self):
        a,b = c.c_int(),c.c_int()
        dll.test_output(c.byref(a),c.byref(b))
        return a.value,b.value

    def drain(self):
        b = c.create_string_buffer(4096)
        count = dll.test_drain(b,4096)
        raw = b.raw[:count]
        packets=[]
        while raw:
            self.assertEqual(raw[:2],b'\xa5\x5a')
            version,t,n,session,seq = struct.unpack('<BBHII',raw[2:14])
            self.assertEqual(version,1)
            self.assertLessEqual(n,64)
            self.assertGreaterEqual(len(raw),n+16)
            self.assertEqual(struct.unpack('<H',raw[14+n:16+n])[0],binascii.crc_hqx(raw[2:14+n],0xffff))
            packets.append((t,session,seq,raw[14:14+n]))
            raw=raw[n+16:]
        return packets

    def send(self,t,seq,p=b'',tick=100,ok=1,session=1234):
        raw=frame(t,seq,p,session)
        dll.test_rx(raw,len(raw)); dll.test_tick(tick,ok)
        return self.drain()

    def ack(self,packets,seq,status):
        replies=[p for p in packets if p[0]==0x82 and p[2]==seq]
        self.assertEqual(len(replies),1)
        self.assertEqual(replies[0][3][1],status)

    def hello(self,tick=100):
        packets=self.send(1,1,tick=tick)
        self.ack(packets,1,0)
        self.assertEqual([p[3] for p in packets if p[0]==0x81][0],struct.pack('<6HB',15,10000,1180,4000,100,20,1))

    def start(self,tick=100,deadline=300,duration=200):
        self.hello(tick)
        self.ack(self.send(2,2,b'\x01',tick=tick),2,0)
        self.ack(self.send(3,3,struct.pack('<hhHI',1000,-1000,duration,deadline),tick=tick),3,0)
        self.assertEqual(self.output(),(1000,-1000))

    def test_golden_hello_split_noise_and_state_layout(self):
        # Literal vector is independent of C, independently CRC checked here.
        golden=bytes.fromhex('a55a01010000d204000001000000f451')
        self.assertEqual(frame(1,1),golden)
        raw=b'noise+CONNECTED\r\n'+golden
        for n in raw:
            dll.test_rx(bytes([n]),1)
            dll.test_tick(100,1)
        packets=self.drain()
        self.ack(packets,1,0)
        dll.test_tick(150,1)
        p=[p[3] for p in self.drain() if p[0]==0x83][0]
        self.assertEqual(len(p),48)
        self.assertEqual(struct.unpack_from('<ii',p,8),(12,-13))
        self.assertEqual(struct.unpack_from('<I',p,44)[0],0)

    def test_startup_and_local_gate(self):
        dll.test_tick(0,1); self.assertEqual(self.output(),(0,0)); self.drain()
        self.hello()
        self.ack(self.send(2,2,b'\x01',ok=0),2,5)
        self.assertEqual(self.output(),(0,0))

    def test_deadline_and_repeat_cannot_extend(self):
        self.start()
        self.ack(self.send(2,4,b'\x01',tick=110),4,4)
        self.ack(self.send(3,5,struct.pack('<hhHI',2000,2000,1000,1110),tick=110),5,4)
        dll.test_tick(299,1); self.drain();self.assertEqual(self.output(),(1000,-1000))
        dll.test_tick(300,1);self.assertEqual(self.output(),(0,0))
        dll.test_tick(350,1)
        packets=self.drain();p=[p[3] for p in packets if p[0]==0x83][0]
        self.assertEqual(p[29:31],b'\x00\x02')

    def test_shorter_absolute_deadline_and_expired_rejection(self):
        self.start(deadline=120)
        dll.test_tick(120,1); self.drain();self.assertEqual(self.output(),(0,0))
        self.ack(self.send(2,4,b'\x01',tick=130),4,0)
        self.ack(self.send(3,5,struct.pack('<hhHI',1000,1000,200,129),tick=130),5,8)
        self.assertEqual(self.output(),(0,0))

    def test_wrong_session_duplicate_and_corrupt_crc(self):
        self.start()
        self.ack(self.send(3,3,struct.pack('<hhHI',2000,2000,200,320),tick=120),3,3)
        self.ack(self.send(2,4,b'\x01',tick=120,session=999),4,2)
        broken=bytearray(frame(4,4));broken[-1]^=1
        dll.test_rx(bytes(broken),len(broken));dll.test_tick(120,1)
        self.assertFalse(any(p[0]==0x82 for p in self.drain()))
        self.assertEqual(self.output(),(1000,-1000))
        # STOP with a different identity is deliberately always accepted.
        self.ack(self.send(4,99,tick=130,session=999),99,0)
        self.assertEqual(self.output(),(0,0))

    def test_local_disable_disarms_and_recovery_needs_arm(self):
        self.start();dll.test_tick(110,0);self.drain()
        self.assertEqual(self.output(),(0,0))
        self.ack(self.send(3,4,struct.pack('<hhHI',1000,1000,200,330),tick=130),4,4)

    def test_stop_watermark_and_replayed_hello(self):
        self.start()
        self.ack(self.send(4,6,tick=110),6,0)
        self.ack(self.send(2,4,b'\x01',tick=120),4,3)
        self.ack(self.send(3,5,struct.pack('<hhHI',1000,1000,100,220),tick=120),5,3)
        self.ack(self.send(1,1,tick=120),1,3)
        self.assertEqual(self.output(),(0,0))
        self.ack(self.send(2,4,b'\x01',tick=120),4,3)
        self.ack(self.send(1,1,tick=130,session=5678),1,0)
        self.ack(self.send(1,1,tick=140),1,3)
        self.assertEqual(self.output(),(0,0))

    def test_atomic_ram_parameters_and_pi(self):
        self.hello()
        self.ack(self.send(2,2,b'\x02'),2,7)
        values=(100000,10000,100000,10000)
        packets=self.send(7,3,struct.pack('<4I',*values))
        self.ack(packets,3,0)
        self.assertEqual([p[3] for p in packets if p[0]==0x84][0],struct.pack('<5I',1,*values))
        self.ack(self.send(7,4,struct.pack('<4I',1,2,3,2000001)),4,1)
        packets=self.send(6,5)
        self.assertEqual([p[3] for p in packets if p[0]==0x84][0],struct.pack('<5I',1,*values))
        self.ack(self.send(2,6,b'\x02'),6,0)
        self.ack(self.send(7,7,struct.pack('<4I',0,0,0,0)),7,6)
        self.ack(self.send(5,8,struct.pack('<hhHI',100,-100,200,300)),8,0)
        self.assertEqual(self.output(),(110,-110))
        dll.test_speed(.1,-.1);dll.test_tick(110,1);self.drain()
        self.assertEqual(self.output(),(10,-10))
        dll.test_tick(300,1);self.assertEqual(self.output(),(0,0))

    def test_range_bad_length_and_unconfigured_pi(self):
        self.hello();self.ack(self.send(2,2,b'\x01'),2,0)
        self.ack(self.send(3,3,struct.pack('<hhHI',10001,0,100,200)),3,1)
        self.ack(self.send(3,4,b'\x00'),4,1)
        # 工作区的旧开环上限已扩为4秒；拒绝用例应超过实际常量，而非旧的2秒。
        header=(Path(__file__).resolve().parents[1]/'BALANCE'/'project_link.h').read_text(encoding='utf-8')
        limit=int(re.search(r'#define\s+PROJECT_MAX_DURATION\s+(\d+)',header)[1])
        self.ack(self.send(3,5,struct.pack('<hhHI',1,1,limit+1,limit+101)),5,1)
        self.assertEqual(self.output(),(0,0))

    def test_arm_idle_timeout(self):
        self.hello();self.ack(self.send(2,2,b'\x01'),2,0)
        dll.test_tick(5100,1);packets=self.drain()
        self.assertEqual([p[3][29:31] for p in packets if p[0]==0x83],[b'\x00\x05'])

    def test_binary_reset_isolation_and_bare_boot_reset(self):
        self.hello()
        self.ack(self.send(99,2,b'reset'),2,9)
        self.assertEqual(dll.test_resets(),0)
        bad=frame(99,3,b'reset')[:-1]+b'\x00'
        dll.test_rx(bad,len(bad));dll.test_tick(110,1);self.drain()
        self.assertEqual(dll.test_resets(),0)
        dll.test_rx(b'reset',5);dll.test_tick(300,1);self.drain()
        self.assertEqual(dll.test_resets(),1)

    def test_overflow_stops_and_discards_commands(self):
        self.start()
        raw=frame(2,4,b'\x01')+b'x'*600
        dll.test_rx(raw,len(raw));dll.test_tick(110,1);self.drain()
        self.assertEqual(self.output(),(0,0))
        self.ack(self.send(3,5,struct.pack('<hhHI',1000,1000,100,220),tick=120),5,4)
        dll.test_error();dll.test_tick(130,1);self.drain()
        self.assertEqual(self.output(),(0,0))

    def test_tx_overflow_stops_and_can_recover(self):
        self.start()
        # Flood acknowledged reads without draining the TX ring.
        for seq in range(4,45):
            raw=frame(6,seq);dll.test_rx(raw,len(raw));dll.test_tick(110,1)
        self.assertEqual(self.output(),(0,0));self.drain()
        dll.test_tick(120,1);self.drain()
        self.ack(self.send(2,46,b'\x01',tick=130),46,0)

    def test_tick_wrap(self):
        self.start(tick=0xfffffff0,deadline=0x54,duration=100)
        dll.test_tick(0x53,1);self.drain();self.assertEqual(self.output(),(1000,-1000))
        dll.test_tick(0x54,1);self.assertEqual(self.output(),(0,0))

    def test_partial_timeout_then_valid_frame(self):
        raw=frame(1,1)[:9];dll.test_rx(raw,len(raw));dll.test_tick(100,1);self.drain()
        self.hello(tick=210)

    def profile_query(self, tick=100):
        packets=self.send(9,2,tick=tick)
        self.ack(packets,2,0)
        self.assertEqual([p[3] for p in packets if p[0]==0x85],
                         [struct.pack('<HBBHHHH',3,1,2,10000,4000,100,20)])

    def profile(self, *, tick=100, deadline=1100, first=400, total=1000,
                left=1400, second=900):
        self.hello(tick);self.profile_query(tick)
        self.ack(self.send(2,3,b'\x01',tick=tick),3,0)
        body=struct.pack('<BBHhhhhHHI',1,2,0,left,0,second,0,first,total,deadline)
        packets=self.send(8,4,body,tick=tick);self.ack(packets,4,0)
        return body

    def profile_state(self,tick,ok=1):
        dll.test_tick(tick,ok)
        packets=self.drain()
        states=[p[3] for p in packets if p[0]==0x86]
        self.assertEqual(len(states),1)
        self.assertEqual(len(states[0]),64)
        self.assertEqual(states[0][44:48],bytes(4))
        return states[0],struct.unpack('<IIIBBH',states[0][48:])

    def test_extended_profile_upper_limit_and_expiry(self):
        self.profile(tick=100,deadline=2100,first=1000,total=2000,left=9000,second=10000)
        self.assertEqual(self.output(),(9000,0))
        dll.test_tick(1100,1);self.drain();self.assertEqual(self.output(),(10000,0))
        dll.test_tick(2099,1);self.drain();self.assertEqual(self.output(),(10000,0))
        dll.test_tick(2100,1);self.drain();self.assertEqual(self.output(),(0,0))

    def test_extended_profile_stop_cancels_high_second_segment(self):
        self.profile(tick=100,deadline=2100,first=1000,total=2000,left=4000,second=10000)
        self.ack(self.send(4,5,tick=500),5,0)
        dll.test_tick(1100,1);self.drain();self.assertEqual(self.output(),(0,0))

    def test_four_second_profile_switch_expiry_and_stop(self):
        self.profile(tick=100,deadline=4100,first=2000,total=4000,left=4000,second=10000)
        dll.test_tick(2099,1);self.drain();self.assertEqual(self.output(),(4000,0))
        dll.test_tick(2100,1);self.drain();self.assertEqual(self.output(),(10000,0))
        dll.test_tick(4099,1);self.drain();self.assertEqual(self.output(),(10000,0))
        dll.test_tick(4100,1);self.drain();self.assertEqual(self.output(),(0,0))
        dll.test_reset()
        self.profile(tick=100,deadline=4100,first=2000,total=4000,left=4000,second=10000)
        self.ack(self.send(4,5,tick=500),5,0)
        dll.test_tick(2200,1);self.drain();self.assertEqual(self.output(),(0,0))

    def test_profile_golden_and_default_old_host_state_compatibility(self):
        body=struct.pack('<BBHhhhhHHI',1,2,0,1400,0,900,0,400,1000,1100)
        self.assertEqual(frame(8,4,body).hex(),
            'a55a01081400d2040000040000000102000078050000840300009001e8034c040000e22f')
        self.assertEqual(frame(9,2).hex(),'a55a01090000d204000002000000a668')
        self.hello()
        dll.test_tick(150,1)
        self.assertEqual([p[0] for p in self.drain()],[0x83])
        # Without explicit negotiation a profile is not executable.
        self.ack(self.send(8,2,body,tick=160),2,9)
        self.assertEqual(self.output(),(0,0))

    def test_profile_two_outputs_same_pwm_metadata_and_no_renewal(self):
        body=self.profile(second=1400)
        self.assertEqual(self.output(),(1400,0))
        self.ack(self.send(8,5,body,tick=110),5,4)
        self.ack(self.send(3,6,struct.pack('<hhHI',900,0,1000,1110),tick=110),6,4)
        self.ack(self.send(2,7,b'\x01',tick=110),7,4)
        _,meta=self.profile_state(450)
        self.assertEqual(meta,(4,100,0,0,1,0))
        state,meta=self.profile_state(507)
        self.assertEqual(meta,(4,100,507,1,1,0))
        self.assertEqual(struct.unpack_from('<h',state,20)[0],1400)
        _,meta=self.profile_state(1100)
        self.assertEqual(self.output(),(0,0));self.assertEqual(meta,(4,100,507,1,2,0))
        self.ack(self.send(2,8,b'\x01',tick=1110),8,0)
        _,meta=self.profile_state(1150)
        self.assertEqual(meta,(0,0,0,255,0,0))

    def test_profile_short_deadline_stop_and_local_disable_cancel_second(self):
        self.profile(deadline=300)
        _,meta=self.profile_state(300)
        self.assertEqual(meta,(4,100,0,0,2,0));self.assertEqual(self.output(),(0,0))
        _,meta=self.profile_state(550)
        self.assertEqual(meta,(4,100,0,0,2,0));self.assertEqual(self.output(),(0,0))
        for stop_type in ('protocol','local','overflow'):
            dll.test_reset();self.profile()
            if stop_type=='protocol': self.ack(self.send(4,5,tick=200),5,0)
            elif stop_type=='local': dll.test_tick(200,0);self.drain()
            else: dll.test_error();dll.test_tick(200,1);self.drain()
            _,meta=self.profile_state(550)
            self.assertEqual(meta,(4,100,0,0,2,0));self.assertEqual(self.output(),(0,0))

    def test_profile_accepted_then_stop_before_output_is_not_execution(self):
        self.hello();self.profile_query();self.ack(self.send(2,3,b'\x01'),3,0)
        body=struct.pack('<BBHhhhhHHI',1,2,0,1400,0,900,0,400,1000,1100)
        raw=frame(8,4,body)+frame(4,5)
        dll.test_rx(raw,len(raw));dll.test_tick(110,1)
        packets=self.drain();self.ack(packets,4,0);self.ack(packets,5,0)
        state,meta=self.profile_state(150)
        self.assertEqual(meta,(4,110,0,255,2,0));self.assertEqual(self.output(),(0,0))
        self.assertEqual(state[29:31],b'\x00\x01')

    def test_profile_bad_fields_atomic_rejection_and_wrong_mode(self):
        self.hello();self.profile_query()
        valid=(1,2,0,1400,0,900,0,400,1000,1100)
        for seq,(slot,value) in enumerate(((0,2),(1,3),(2,1),(3,10001),(5,-1),(7,0),(7,1000),(8,4001)),start=3):
            bad=list(valid);bad[slot]=value
            self.ack(self.send(8,seq,struct.pack('<BBHhhhhHHI',*bad)),seq,1)
            self.assertEqual(self.output(),(0,0))
        self.ack(self.send(8,11,struct.pack('<BBHhhhhHHI',*valid)),11,4)
        self.ack(self.send(2,12,b'\x01'),12,0)
        self.ack(self.send(9,13),13,4)
        self.ack(self.send(8,14,struct.pack('<BBHhhhhHHI',*valid)[:-1]),14,1)
        expired=list(valid);expired[-1]=99
        self.ack(self.send(8,15,struct.pack('<BBHhhhhHHI',*expired)),15,8)

    def test_profile_wrap_replay_and_tx_overflow_cancel(self):
        self.profile(tick=0xfffffff0,deadline=0x54,first=40,total=100)
        _,meta=self.profile_state(0x28)
        self.assertEqual(meta,(4,0xfffffff0,0x28,1,1,0))
        body=struct.pack('<BBHhhhhHHI',1,2,0,1400,0,900,0,40,100,0x80)
        self.ack(self.send(8,4,body,tick=0x30),4,3)
        _,meta=self.profile_state(0x5a)
        self.assertEqual(meta[-2],2);self.assertEqual(self.output(),(0,0))
        dll.test_reset();self.profile()
        for seq in range(5,45):
            raw=frame(6,seq);dll.test_rx(raw,len(raw));dll.test_tick(110,1)
        self.assertEqual(self.output(),(0,0));self.drain()
        state,meta=self.profile_state(550)
        self.assertEqual(meta[-2],2);self.assertEqual(meta[3],0)
        self.assertGreater(struct.unpack_from('<I',state,36)[0],0)

    def test_encoder_all_control_samples_while_disarmed_and_separate_sequence(self):
        self.hello()
        packets=self.send(10,2,tick=110)
        self.ack(packets,2,0)
        self.assertEqual([p[3] for p in packets if p[0]==0x88],[struct.pack('<BBHHHIII',1,64,100,20,0,1560,66000,160000)])
        for tick,a,b in [(110,1,-2),(120,3,-4),(130,5,-6),(140,7,-8),(150,9,-10)]:
            dll.test_encoder(tick,10,a,b)
            dll.test_tick(tick,1)
        packets=self.drain()
        cumulative=[p for p in packets if p[0]==0x87]
        self.assertEqual(len(cumulative),1)
        self.assertEqual(struct.unpack('<HHIIIqqII',cumulative[0][3]),(1,1,110,150,5,25,-30,10,0))
        self.assertEqual(self.output(),(0,0))
        self.assertEqual([p[2] for p in packets if p[0]==0x83],[2])

    def test_encoder_totals_survive_stop_hello_and_need_explicit_query(self):
        dll.test_encoder(10,10,7800,-7800)
        packets=self.send(1,1,tick=100)
        self.ack(packets,1,0)
        self.assertFalse(any(p[0]==0x87 for p in packets))
        self.ack(self.send(10,2,tick=110),2,0)
        self.ack(self.send(4,3,tick=120),3,0)
        self.ack(self.send(1,1,tick=160,session=5678),1,0)
        packets=self.send(10,2,tick=210,session=5678)
        self.ack(packets,2,0)
        total=[p for p in packets if p[0]==0x87][0]
        self.assertEqual(total[2],1)
        self.assertEqual(struct.unpack('<HHIIIqqII',total[3])[5:7],(7800,-7800))

    def test_encoder_query_rejects_armed_and_long_sample_is_diagnosed(self):
        self.hello()
        self.ack(self.send(2,2,b'\x01',tick=110),2,0)
        self.ack(self.send(10,3,tick=120),3,4)
        self.ack(self.send(4,4,tick=130),4,0)
        dll.test_encoder(150,31,-3,4)
        packets=self.send(10,5,tick=150)
        self.ack(packets,5,0)
        fields=struct.unpack('<HHIIIqqII',[p[3] for p in packets if p[0]==0x87][0])
        self.assertEqual(fields[-2:],(31,1))
        self.assertEqual(self.output(),(0,0))

if __name__=='__main__':
    unittest.main(verbosity=2)
