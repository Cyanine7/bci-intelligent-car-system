"""持久 asyncio 回环连接，保持独立心跳，不重发失败请求。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4


MAX_MESSAGE_BYTES = 64 * 1024
MAX_PENDING = 32
REQUEST_TIMEOUT = 5.0


class BridgeUnavailable(Exception):
    pass


class BridgeClient:
    def __init__(self, project_directory: Path):
        self.project_directory = Path(project_directory).resolve()
        self.endpoint_path = self.project_directory / ".runtime" / "automation.json"
        self._reader = self._writer = None
        self._read_task = self._heartbeat_task = None
        self._token = ""
        self._pending = {}
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._closed = False

    def _endpoint(self) -> dict:
        try:
            if self.endpoint_path.stat().st_size > MAX_MESSAGE_BYTES:
                raise ValueError()
            value = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("host") != "127.0.0.1"
                or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535
                or not isinstance(value.get("token"), str) or not 32 <= len(value["token"]) <= 128
                or Path(value.get("project_directory", "")).resolve() != self.project_directory):
                raise ValueError()
            return value
        except (OSError, ValueError, TypeError, RecursionError):
            raise BridgeUnavailable("本项目自动化工作台未运行，或端点文件无效") from None

    async def _ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._closed:
                raise BridgeUnavailable("MCP 客户端已关闭")
            if self._writer is not None and not self._writer.is_closing():
                return
            endpoint = self._endpoint()
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", endpoint["port"], limit=MAX_MESSAGE_BYTES), 2.0)
            except (OSError, asyncio.TimeoutError):
                raise BridgeUnavailable("无法附加到本项目已有自动化工作台") from None
            self._writer.transport.set_write_buffer_limits(high=MAX_MESSAGE_BYTES, low=MAX_MESSAGE_BYTES // 2)
            self._token = endpoint["token"]
            self._read_task = asyncio.create_task(self._receive(self._reader, self._writer))
            self._heartbeat_task = asyncio.create_task(self._heartbeat(self._writer))

    async def request(self, method: str, params: dict | None = None) -> dict:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await self._ensure_connected()
                return await self._send(method, params or {})
        except asyncio.TimeoutError:
            raise BridgeUnavailable("本地请求超时；结果未知，不重发") from None

    async def _send(self, method: str, params: dict) -> dict:
        if len(self._pending) >= MAX_PENDING:
            raise BridgeUnavailable("本地请求上限已满，请等待结果")
        request_id = uuid4().hex
        try:
            payload = (json.dumps(dict(id=request_id, token=self._token, method=method, params=params),
                                  ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise BridgeUnavailable("请求无法编码为有限 JSON") from None
        if len(payload) > MAX_MESSAGE_BYTES:
            raise BridgeUnavailable("本地请求超过 64 KiB")
        writer = self._writer
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with self._write_lock:
                if writer is None or writer is not self._writer or writer.is_closing():
                    raise BridgeUnavailable("本地连接已关闭；结果未知，不重发")
                writer.write(payload)
                await asyncio.wait_for(writer.drain(), 1.0)
            return await asyncio.wait_for(future, REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            await self._disconnect(writer)
            raise BridgeUnavailable("本地响应超时；结果未知，不重发") from None
        except (OSError, ConnectionError):
            await self._disconnect(writer)
            raise BridgeUnavailable("本地连接中断；结果未知，不重发") from None
        except asyncio.CancelledError:
            await self._disconnect(writer)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _receive(self, reader, writer) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                    raise BridgeUnavailable("本地连接已关闭或响应越界")
                reply = json.loads(raw.decode("utf-8"))
                if (not isinstance(reply, dict) or not isinstance(reply.get("result"), dict)
                        or not isinstance(reply.get("id"), str)):
                    raise BridgeUnavailable("本地响应格式无效")
                future = self._pending.get(reply.get("id"))
                if future is not None and not future.done():
                    future.set_result(reply["result"])
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, BridgeUnavailable):
            await self._disconnect(writer)
        except asyncio.CancelledError:
            pass

    async def _heartbeat(self, writer) -> None:
        try:
            while writer is self._writer:
                await self._send("heartbeat", {})
                await asyncio.sleep(1.0)
        except BridgeUnavailable:
            await self._disconnect(writer)
        except asyncio.CancelledError:
            pass

    async def _disconnect(self, expected_writer=None) -> None:
        if expected_writer is not None and self._writer is not expected_writer:
            return
        current = asyncio.current_task()
        tasks = (self._read_task, self._heartbeat_task)
        self._read_task = self._heartbeat_task = None
        for task in tasks:
            if task is not None and task is not current and not task.done():
                task.cancel()
        writer, self._writer = self._writer, None
        self._reader = None
        self._token = ""
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(BridgeUnavailable("本地连接中断；结果未知，不重发"))
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except (OSError, asyncio.TimeoutError):
                pass

    async def close(self) -> None:
        self._closed = True
        await self._disconnect()
