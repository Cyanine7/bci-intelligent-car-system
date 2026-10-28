"""Actual v2 C protocol/capture, once with user placeholder and once with a test double."""
import sys
import struct
import unittest
import ctypes
implemented=int(sys.argv.pop(2))
import test_project_core as base
dll=base.dll

class SpeedCore(unittest.TestCase):
    output=base.CoreTests.output
    drain=base.CoreTests.drain
    ack=base.CoreTests.ack
    def setUp(self):
        dll.test_reset()
        if implemented and hasattr(dll,'test_algorithm_output'):dll.test_algorithm_output(0)
        self.seq=0;self.now=90
    def tick(self,now):
        dll.test_encoder(now,10,0,0);dll.test_tick(now,1);return self.drain()
    def send(self,kind,payload=b'',now=None,session=1234):
        self.seq+=1;now=self.now+10 if now is None else now;self.now=now
        raw=base.frame(kind,self.seq,payload,session)
        dll.test_encoder(now,10,0,0);dll.test_rx(raw,len(raw));dll.test_tick(now,1)
        return self.drain()
    def setup_caps(self):
        self.ack(self.send(1),1,0)
        packets=self.send(11)
        self.ack(packets,2,0)
        cap=[p[3] for p in packets if p[0]==0x89][0]
        self.assertEqual(struct.unpack('<BBHHHHHIIddHH',cap),
                         (2,3 if implemented else 0,100,16500,1180,3000,384,1560,66000,1e6,4e7,100,200))
    def profile(self,a,b,first=1500,total=3000,deadline=3200):
        return struct.pack('<BBHhhhhHHI',2,2,0,a,0,b,0,first,total,deadline)

    def test_caps_and_unimplemented_gate(self):
        self.setup_caps()
        cfg=struct.pack('<BBHdddd',2,0,16500,19995.842514629873,702884.1611204201,44048.72133689123,1193376.705723456)
        packets=self.send(13,cfg)
        self.ack(packets,self.seq,0 if implemented else 7)
        packets=self.send(2,b'\x03')
        self.ack(packets,self.seq,0 if implemented else 7)
        if not implemented:self.assertEqual(self.output(),(0,0))

    def test_binary64_readback_and_atomic_rejection(self):
        self.setup_caps()
        values=(19995.842514629873,702884.1611204201,44048.72133689123,1193376.705723456)
        cfg=struct.pack('<BBHdddd',2,0,16500,*values)
        packets=self.send(13,cfg);self.ack(packets,self.seq,0 if implemented else 7)
        if implemented:
            self.assertEqual([p[3][4:] for p in packets if p[0]==0x8a],[cfg])

            for bad in (float('nan'),float('inf'),-1.,1e7):
                rejected=struct.pack('<BBHdddd',2,0,16500,bad,*values[1:])
                self.ack(self.send(13,rejected),self.seq,1)
            packets=self.send(12)
            self.assertEqual([p[3][4:] for p in packets if p[0]==0x8a],[cfg])

    def test_only_pi_algorithm_is_accepted(self):
        self.setup_caps()
        for algorithm in (0, 1, 3, 255):
            cfg=struct.pack('<BBHdddd',algorithm,0,16500,1000,10000,1000,10000)
            self.ack(self.send(13,cfg),self.seq,1)

    def test_open_three_seconds_switch_samples_and_paged_read(self):
        self.setup_caps();self.ack(self.send(2,b'\x04'),self.seq,0)
        self.ack(self.send(17,self.profile(4000,16500),now=200),self.seq,0)
        run=self.seq;self.assertEqual(self.output(),(4000,0))
        for now in range(210,3401,10):
            self.tick(now)
            if now==1690:self.assertEqual(self.output(),(4000,0))
            if now==1700:self.assertEqual(self.output(),(16500,0))
            if now==3200:self.assertEqual(self.output(),(0,0))
        p=self.send(15,now=3410);info=[x[3] for x in p if x[0]==0x8c][0]
        head=struct.unpack_from('<IIIIHBBBBH',info)
        self.assertEqual((head[0],head[2],head[3],*head[5:]),(run,200,3200,3,2,2,1,0))
        count=head[4];self.assertGreater(count,320);self.assertLessEqual(count,384)
        meta=[p[3] for p in self.send(18) if p[0]==0x8e][0]
        self.assertEqual(struct.unpack_from('<IIIIBBH',meta),(run,200,1700,3200,2,1,0))
        payload=struct.pack('<IHH',run,count-8,8)
        packets=self.send(16,payload)
        for i in range(7):packets+=self.tick(self.now+10*(i+1))
        data=[p[3] for p in packets if p[0]==0x8d]
        self.assertEqual(len(data),8)
        self.assertEqual([struct.unpack_from('<IHH',p) for p in data],[(run,i,0) for i in range(count-8,count)])
        self.assertTrue(all(len(p)==60 for p in data))

    def test_limits_bad_phase_and_stop_before_second(self):
        self.setup_caps();self.ack(self.send(2,b'\x04'),self.seq,0)
        for a,b,first,total in ((0,16501,1500,3000),(100,-100,1500,3000),(0,1,1501,3000),(0,1,1500,3010),(1,1,1500,3000)):
            self.ack(self.send(17,self.profile(a,b,first,total,3180),now=180),self.seq,1)
            self.assertEqual(self.output(),(0,0))
        self.ack(self.send(17,self.profile(1000,16500),now=200),self.seq,0)
        self.ack(self.send(4,now=400,session=9999),self.seq,0)
        for t in range(410,1801,10):self.tick(t);self.assertEqual(self.output(),(0,0))
        info=[p[3] for p in self.send(15,now=1810) if p[0]==0x8c][0]
        self.assertEqual((info[18],info[19],info[21]),(3,1,0))

    @unittest.skipUnless(implemented and hasattr(dll,'test_algorithm_output'),'fault injection requires the protocol test double')
    def test_invalid_algorithm_output_never_reaches_pwm(self):
        self.setup_caps();cfg=struct.pack('<BBHdddd',2,0,16500,1000,10000,1000,10000)
        self.ack(self.send(13,cfg),self.seq,0);self.ack(self.send(2,b'\x03'),self.seq,0)
        dll.test_algorithm_output(16501)
        self.ack(self.send(14,self.profile(100,200),now=200),self.seq,0)
        self.assertEqual(self.output(),(0,0))
        state=[p[3] for p in self.tick(250) if p[0]==0x83][0]
        self.assertEqual((state[29],state[30]),(0,6))

    def test_wrap_and_short_deadline_never_execute_second_stage(self):
        self.now=0xfffff9b0
        self.setup_caps()
        self.ack(self.send(2,b'\x04'),self.seq,0)
        start=0xfffffa00
        deadline=(start+1000)&0xffffffff
        self.ack(self.send(17,self.profile(1000,16500,1500,3000,deadline),now=start),self.seq,0)
        for offset in range(10,3220,10):
            self.tick((start+offset)&0xffffffff)
            self.assertEqual(self.output(),(1000,0) if offset<1000 else (0,0))
        info=[p[3] for p in self.send(15,now=(start+3220)&0xffffffff) if p[0]==0x8c][0]
        self.assertEqual((info[18],info[19],info[21]),(3,2,0))
        self.assertEqual(struct.unpack_from('<I',info,12)[0],deadline)

    @unittest.skipUnless(implemented and not hasattr(dll,'test_algorithm_output'),'actual production PI only')
    def test_production_pi_output_and_second_stage_continuity(self):
        self.setup_caps()
        dll.test_sensor_counts(0,0)
        cfg=struct.pack('<BBHdddd',2,0,16500,1000,10000,1000,10000)
        self.ack(self.send(13,cfg),self.seq,0)
        self.ack(self.send(2,b'\x03'),self.seq,0)
        self.ack(self.send(14,self.profile(100,200),now=200),self.seq,0)
        self.assertLessEqual(abs(self.output()[0]-10),1);self.assertEqual(self.output()[1],0)
        for now in range(210,1700,10):self.tick(now)
        self.assertLessEqual(abs(self.output()[0]-1500),1)
        self.tick(1700);self.assertLessEqual(abs(self.output()[0]-1520),1)
        self.ack(self.send(4,session=9999,now=1710),self.seq,0)
        for now in range(1720,1801,10):self.tick(now);self.assertEqual(self.output(),(0,0))

if __name__=='__main__':unittest.main(verbosity=2)

