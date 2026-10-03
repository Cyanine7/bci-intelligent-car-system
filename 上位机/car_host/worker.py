"""Exclusive background transport ownership with bounded event delivery."""

from __future__ import annotations

from datetime import datetime, timezone
from queue import Empty, Full, Queue
import threading
import time
from typing import Callable, Protocol
from uuid import uuid4

from PySide6.QtCore import QThread

from .transport import ConnectionConfig, IoEvent, TransportAdapter, create_adapter, source_for_kind


class WorkerInterface(Protocol):
    """The application consumes events, never a concrete thread or socket."""
    connection_id: str
    source: str

    def start(self) -> None: ...
    def request_send(self, payload: bytes, request_id: str = "") -> bool: ...
    def request_stop(self) -> None: ...
    def drain_events(self, max_count: int = 256) -> list[IoEvent]: ...
    def take_overflow_count(self) -> int: ...
    def isFinished(self) -> bool: ...


def create_worker(config: ConnectionConfig) -> WorkerInterface:
    if config.kind == "bluetooth_spp":
        # Optional platform component must not prevent serial/simulation startup.
        from .bluetooth_worker import BluetoothWorker
        return BluetoothWorker(config)
    source_for_kind(config.kind)
    return TransportWorker(config)


class TransportWorker(QThread):
    SEND_QUEUE_CAPACITY = 128
    EVENT_QUEUE_CAPACITY = 1024
    MAX_PAYLOAD_BYTES = 4096

    def __init__(
        self,
        config: ConnectionConfig,
        adapter_factory: Callable[[ConnectionConfig], TransportAdapter] = create_adapter,
    ):
        super().__init__()
        self.config = config
        self.connection_id = uuid4().hex
        self._adapter_factory = adapter_factory
        self._send_queue: Queue[tuple[bytes, str]] = Queue(maxsize=self.SEND_QUEUE_CAPACITY)
        self._events: Queue[IoEvent] = Queue(maxsize=self.EVENT_QUEUE_CAPACITY)
        self._stopping = threading.Event()
        self._accepting = threading.Event()
        self._state_lock = threading.Lock()
        self._overflow_lock = threading.Lock()
        self._overflow_count = 0
        self._active = False

    @property
    def source(self) -> str:
        return source_for_kind(self.config.kind)

    def start(self, priority=QThread.Priority.InheritPriority) -> None:
        """Permit a completed worker to be explicitly started again."""
        with self._state_lock:
            if self._active or self.isRunning():
                return
            self._clear_send_queue()
            self._accepting.clear()
            self._stopping.clear()
            self._active = True
        try:
            super().start(priority)
        except Exception:
            with self._state_lock:
                self._active = False
            raise

    def request_send(self, payload: bytes, request_id: str = "") -> bool:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            return False
        payload = bytes(payload)
        if not payload or len(payload) > self.MAX_PAYLOAD_BYTES:
            return False
        with self._state_lock:
            if not self._accepting.is_set() or self._stopping.is_set():
                return False
            try:
                self._send_queue.put_nowait((payload, request_id or uuid4().hex))
            except Full:
                return False
        return True

    def request_stop(self) -> None:
        # No serial calls here: stop never waits for a serial read/write on UI.
        with self._state_lock:
            self._stopping.set()
            self._accepting.clear()
            self._clear_send_queue()

    def drain_events(self, max_count: int = 256) -> list[IoEvent]:
        events = []
        for _ in range(max(0, min(max_count, self.EVENT_QUEUE_CAPACITY))):
            try:
                events.append(self._events.get_nowait())
            except Empty:
                break
        return events

    def take_overflow_count(self) -> int:
        with self._overflow_lock:
            count, self._overflow_count = self._overflow_count, 0
        return count

    def run(self) -> None:
        adapter = None
        phase = "连接"
        active_request_id = ""
        try:
            if self._stopping.is_set():
                return
            adapter = self._adapter_factory(self.config)
            adapter.open()
            with self._state_lock:
                if self._stopping.is_set():
                    return
                self._accepting.set()
            self._emit("opened", message="连接已打开")
            while not self._stopping.is_set():
                # Keep receive processing responsive even during send bursts.
                for _ in range(32):
                    if self._stopping.is_set():
                        break
                    try:
                        payload, request_id = self._send_queue.get_nowait()
                    except Empty:
                        break
                    if self._stopping.is_set():
                        break
                    phase = "发送"
                    active_request_id = request_id
                    written = adapter.write(payload)
                    if written != len(payload):
                        raise OSError(f"发送不完整：{written}/{len(payload)} 字节")
                    self._emit("tx", payload=payload, request_id=request_id)
                    active_request_id = ""
                if self._stopping.is_set():
                    break
                phase = "接收"
                payload = adapter.read(self.MAX_PAYLOAD_BYTES)
                if payload:
                    self._emit("rx", payload=bytes(payload))
                else:
                    # Protect against a nonblocking adapter/fake busy loop.
                    self._stopping.wait(0.001)
        except Exception as exc:
            self._accepting.clear()
            self._stopping.set()
            self._emit("error", message=f"{phase}失败：{type(exc).__name__}: {exc}", request_id=active_request_id)
        finally:
            self._accepting.clear()
            with self._state_lock:
                self._clear_send_queue()
            if adapter is not None:
                try:
                    adapter.close()
                except Exception as exc:
                    self._emit("error", message=f"关闭失败：{type(exc).__name__}: {exc}")
            self._emit("closed", message="连接已关闭")
            with self._state_lock:
                self._active = False

    def _emit(self, kind: str, payload: bytes = b"", message: str = "", request_id: str = "") -> None:
        event = IoEvent(
            kind=kind,
            payload=payload,
            message=message,
            monotonic=time.monotonic(),
            utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            connection_id=self.connection_id,
            request_id=request_id,
        )
        try:
            self._events.put_nowait(event)
            return
        except Full:
            pass
        try:
            self._events.get_nowait()
        except Empty:
            pass
        else:
            with self._overflow_lock:
                self._overflow_count += 1
        self._events.put_nowait(event)

    def _clear_send_queue(self) -> None:
        while True:
            try:
                self._send_queue.get_nowait()
            except Empty:
                return
