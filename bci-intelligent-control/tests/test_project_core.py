"""Run the actual portable MCU C core via a native DLL; no hardware is touched.

Compile project_core_fixture.c with MSVC /LD, then pass the DLL path here.
Wire encoding and CRC verification use independent Python struct/binascii.
"""
import binascii
import ctypes as c
from pathlib import Path
import struct
import sys
import unittest

dll = c.CDLL(str(Path(sys.argv.pop(1)).resolve()))
dll.test_rx.argtypes = (c.c_void_p, c.c_uint)
dll.test_tick.argtypes = (c.c_uint32, c.c_int)
dll.test_output.argtypes = (c.POINTER(c.c_int), c.POINTER(c.c_int))
dll.test_drain.argtypes = (c.c_void_p, c.c_uint)
dll.test_drain.restype = c.c_uint
dll.test_speed.argtypes = (c.c_float, c.c_float)

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
        self.assertEqual([p[3] for p in packets if p[0]==0x81][0],struct.pack('<6HB',15,6000,300,1000,100,20,1))

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
        self.ack(self.send(3,3,struct.pack('<hhHI',6001,0,100,200)),3,1)
        self.ack(self.send(3,4,b'\x00'),4,1)
        self.ack(self.send(3,5,struct.pack('<hhHI',1,1,1001,1101)),5,1)
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

if __name__=='__main__':
    unittest.main(verbosity=2)
