"""Hardware-free transport tests using deterministic clocks and adapters."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from car_host.transport import (
    ConnectionConfig,
    IoEvent,
    SerialTransport,
    SimulationTransport,
    create_adapter,
    list_serial_ports,
)
from car_host.worker import TransportWorker


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class FakeAdapter:
    def __init__(self, failure="", partial_write=False):
        self.failure = failure
        self.partial_write = partial_write
        self.opened = False
        self.closed = False
        self.written = []
        self.thread_ids = []
        self.read_entered = threading.Event()
        self.release_read = threading.Event()
        self.block_read = False

    def open(self):
        self.thread_ids.append(threading.get_ident())
        if self.failure == "open":
            raise OSError("fake open failure")
        self.opened = True

    def read(self, max_bytes=4096):
        self.thread_ids.append(threading.get_ident())
        if self.failure == "read":
            raise OSError("fake read failure")
        self.read_entered.set()
        if self.block_read:
            self.release_read.wait(1)
        else:
            time.sleep(0.002)
        return b""

    def write(self, payload):
        self.thread_ids.append(threading.get_ident())
        if self.failure == "write":
            raise OSError("fake write failure")
        self.written.append(payload)
        return len(payload) - 1 if self.partial_write else len(payload)

    def close(self):
        self.thread_ids.append(threading.get_ident())
        self.opened = False
        self.closed = True


class TransportTests(unittest.TestCase):
    def test_serial_send_failure_retains_request_identity(self):
        adapter = FakeAdapter(failure="write")
        worker = self.make_worker(adapter)
        worker.start()
        self.wait_for_events(worker, "opened")
        self.assertTrue(worker.request_send(b"test", request_id="request-for-failure"))
        events = self.wait_for_events(worker, "closed")
        errors = [item for item in events if item.kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].request_id, "request-for-failure")
        self.assertEqual(errors[0].connection_id, worker.connection_id)

    def wait_for_events(self, worker, wanted, timeout=1):
        result = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result.extend(worker.drain_events())
            if wanted in {event.kind for event in result}:
                return result
            time.sleep(0.002)
        self.fail(f"Event {wanted!r} did not arrive; got {[event.kind for event in result]}")

    def make_worker(self, adapter):
        worker = TransportWorker(ConnectionConfig(), adapter_factory=lambda _: adapter)
        self.addCleanup(self.stop_worker, worker, adapter)
        return worker

    @staticmethod
    def stop_worker(worker, adapter=None):
        worker.request_stop()
        if adapter is not None:
            adapter.release_read.set()
        if not worker.wait(1500):
            raise AssertionError("worker did not stop")

    def test_models_are_frozen(self):
        config = ConnectionConfig()
        self.assertEqual(config.baudrate, 230400)
        with self.assertRaises(FrozenInstanceError):
            config.port = "COM3"
        with self.assertRaises(FrozenInstanceError):
            IoEvent("rx").kind = "tx"

    def test_simulation_rate_split_and_no_echo(self):
        clock = FakeClock()
        adapter = SimulationTransport(clock=clock, sleeper=clock.sleep)
        adapter.open()
        self.assertEqual(adapter.read(), b"")
        self.assertEqual(adapter.read(), b"")
        first = adapter.read()
        self.assertEqual(first, b"{C4:6:90}$")
        packets = [first]
        for number in range(2, 6):
            clock.now = 100.0 + number * adapter.FRAME_PERIOD
            chunk = adapter.read()
            if number == 5:
                self.assertFalse(chunk.endswith(b"$"))
                chunk += adapter.read()
            packets.append(chunk)
        self.assertTrue(all(packet.startswith(b"{C") and packet.endswith(b"}$") for packet in packets))
        self.assertEqual(adapter.write(b"{C999:999:1}$"), 13)
        self.assertEqual(list(adapter.written_payloads), [b"{C999:999:1}$"])
        self.assertEqual(adapter.read(), b"")
        adapter.close()
        with self.assertRaises(RuntimeError):
            adapter.read()

    def test_simulation_bounded_reads_and_repeatable_reopen(self):
        clock = FakeClock()
        adapter = SimulationTransport(clock=clock, sleeper=clock.sleep)
        adapter.open()
        clock.now += 0.05
        chunks = [adapter.read(2) for _ in range(5)]
        self.assertEqual(b"".join(chunks), b"{C4:6:90}$")
        for _ in range(200):
            adapter.write(b"x")
        self.assertEqual(len(adapter.written_payloads), 128)
        adapter.close()
        adapter.open()
        clock.now += 0.05
        self.assertEqual(adapter.read(), b"{C4:6:90}$")

    def test_factory_and_source(self):
        self.assertIsInstance(create_adapter(ConnectionConfig(kind="simulation")), SimulationTransport)
        with self.assertRaises(ValueError):
            create_adapter(ConnectionConfig(kind="invalid"))
        self.assertEqual(TransportWorker(ConnectionConfig()).source, "SERIAL")
        self.assertEqual(TransportWorker(ConnectionConfig(kind="simulation")).source, "SIMULATOR")

    def test_real_serial_configuration_and_byte_io(self):
        import serial

        device = MagicMock()
        device.in_waiting = 3
        device.read.side_effect = [b"a", b"bcd", b""]
        device.write.return_value = 3
        with patch("serial.Serial", return_value=device) as constructor:
            adapter = SerialTransport(ConnectionConfig(port="COM8", baudrate=115200))
            adapter.open()
            constructor.assert_called_once_with(
                port=None,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.02,
                write_timeout=0.3,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            self.assertEqual(device.port, "COM8")
            self.assertFalse(device.dtr)
            self.assertFalse(device.rts)
            device.open.assert_called_once()
            self.assertEqual(adapter.read(), b"abcd")
            self.assertEqual(adapter.read(), b"")
            self.assertEqual(adapter.write(b"xyz"), 3)
            adapter.close()
            adapter.close()
            device.close.assert_called_once()
            with self.assertRaises(RuntimeError):
                adapter.write(b"closed")

    def test_real_serial_open_failure_cleanup_and_enumeration(self):
        device = MagicMock()
        device.open.side_effect = OSError("busy")
        with patch("serial.Serial", return_value=device):
            adapter = SerialTransport(ConnectionConfig(port="COM8"))
            with self.assertRaises(OSError):
                adapter.open()
            device.close.assert_called_once()
            with self.assertRaises(RuntimeError):
                adapter.read()
        with self.assertRaises(ValueError):
            SerialTransport(ConnectionConfig()).open()
        ports = [
            SimpleNamespace(device="COM8", description="Bluetooth"),
            SimpleNamespace(device="COM3", description="USB"),
        ]
        with patch("serial.tools.list_ports.comports", return_value=ports):
            self.assertEqual(list_serial_ports(), [("COM3", "USB"), ("COM8", "Bluetooth")])

    def test_simulation_worker_receives_telemetry_without_echo(self):
        worker = TransportWorker(ConnectionConfig(kind="simulation"))
        self.addCleanup(self.stop_worker, worker)
        worker.start()
        self.wait_for_events(worker, "opened")
        events = self.wait_for_events(worker, "rx")
        self.assertTrue(next(event for event in events if event.kind == "rx").payload.startswith(b"{C"))
        self.assertTrue(worker.request_send(b"arbitrary-debug-bytes"))
        events = self.wait_for_events(worker, "tx")
        self.assertEqual(next(event for event in events if event.kind == "tx").payload, b"arbitrary-debug-bytes")
        events = self.wait_for_events(worker, "rx")
        self.assertNotIn(b"arbitrary-debug-bytes", [event.payload for event in events if event.kind == "rx"])

    def test_lifecycle_send_timestamps_and_thread_ownership(self):
        adapter = FakeAdapter()
        worker = self.make_worker(adapter)
        self.assertFalse(worker.request_send(b"before-open"))
        worker.start()
        opened = self.wait_for_events(worker, "opened")
        self.assertTrue(worker.request_send(b"debug"))
        sent = self.wait_for_events(worker, "tx")
        self.assertEqual(sent[-1].payload, b"debug")
        worker.request_stop()
        self.assertFalse(worker.request_send(b"after-stop"))
        self.assertTrue(worker.wait(1500))
        closed = worker.drain_events()
        self.assertEqual(closed[-1].kind, "closed")
        self.assertTrue(adapter.closed)
        self.assertEqual(adapter.written, [b"debug"])
        self.assertEqual(len(set(adapter.thread_ids)), 1)
        self.assertNotEqual(adapter.thread_ids[0], threading.get_ident())
        for event in opened + sent + closed:
            self.assertGreater(event.monotonic, 0)
            self.assertTrue(event.utc.endswith("Z"))

    def test_open_and_read_failures_close(self):
        for failure in ("open", "read"):
            with self.subTest(failure=failure):
                adapter = FakeAdapter(failure=failure)
                worker = self.make_worker(adapter)
                worker.start()
                self.assertTrue(worker.wait(1500))
                events = worker.drain_events()
                self.assertEqual([event.kind for event in events][-2:], ["error", "closed"])
                self.assertIn(f"fake {failure} failure", events[-2].message)
                self.assertTrue(adapter.closed)
                self.assertFalse(worker.request_send(b"x"))

    def test_write_failure_and_partial_write_close(self):
        for failure, partial in (("write", False), ("", True)):
            with self.subTest(failure=failure, partial=partial):
                adapter = FakeAdapter(failure=failure, partial_write=partial)
                worker = self.make_worker(adapter)
                worker.start()
                self.wait_for_events(worker, "opened")
                self.assertTrue(worker.request_send(b"debug"))
                events = self.wait_for_events(worker, "closed")
                self.assertEqual([event.kind for event in events][-2:], ["error", "closed"])
                self.assertNotIn("tx", [event.kind for event in events])
                self.assertTrue(worker.wait(1500))

    def test_send_limit_and_stop_cancel_pending(self):
        adapter = FakeAdapter()
        adapter.block_read = True
        worker = self.make_worker(adapter)
        worker.start()
        self.wait_for_events(worker, "opened")
        self.assertTrue(adapter.read_entered.wait(1))
        self.assertFalse(worker.request_send(b""))
        self.assertFalse(worker.request_send(b"x" * 4097))
        self.assertFalse(worker.request_send("text"))
        self.assertTrue(worker.request_send(b"x" * 4096))
        for _ in range(127):
            self.assertTrue(worker.request_send(b"x"))
        self.assertFalse(worker.request_send(b"overflow"))
        worker.request_stop()
        adapter.release_read.set()
        self.assertTrue(worker.wait(1500))
        self.assertEqual(adapter.written, [])
        self.assertFalse(worker.request_send(b"disconnected"))

    def test_event_limit_and_overflow_counter(self):
        worker = TransportWorker(ConnectionConfig())
        for _ in range(1100):
            worker._emit("rx", payload=b"x")
        worker._emit("error", message="must remain visible")
        worker._emit("closed")
        self.assertEqual(worker.take_overflow_count(), 78)
        self.assertEqual(worker.take_overflow_count(), 0)
        self.assertEqual(worker.drain_events(0), [])
        events = worker.drain_events(9999)
        self.assertEqual(len(events), 1024)
        self.assertEqual([event.kind for event in events][-2:], ["error", "closed"])
        self.assertEqual(worker.drain_events(), [])

    def test_repeated_explicit_start_stop(self):
        adapter = FakeAdapter()
        worker = self.make_worker(adapter)
        for _ in range(3):
            worker.start()
            worker.start()
            self.wait_for_events(worker, "opened")
            worker.request_stop()
            worker.request_stop()
            self.assertTrue(worker.wait(1500))
            self.assertEqual(worker.drain_events()[-1].kind, "closed")
            self.assertFalse(worker.request_send(b"old-command"))


if __name__ == "__main__":
    unittest.main()
