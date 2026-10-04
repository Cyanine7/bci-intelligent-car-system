"""回环桥测试仅使用假 service，不创建 HostController 或连接实物。"""

import json
import socket
import threading
import time

import pytest

from car_host.automation_bridge import AutomationBridge, MAX_CLIENTS, MAX_MESSAGE_BYTES


class FakeService:
    def __init__(self):
        self.calls = []
        self.lost = []
        self.thread_ids = []

    def dispatch(self, method, params, client_id):
        self.calls.append((method, params, client_id))
        self.thread_ids.append(threading.get_ident())
        if params.get("huge"):
            return {"text": "x" * MAX_MESSAGE_BYTES}
        return {"ok": True, "method": method}

    def client_lost(self, client_id):
        self.lost.append(client_id)


@pytest.fixture
def endpoint(qapp, tmp_path):
    service = FakeService()
    bridge = AutomationBridge(service, tmp_path)
    assert bridge.start()
    saved = json.loads(bridge.endpoint_path.read_text(encoding="utf-8"))
    yield bridge, service, saved
    bridge.close()
    qapp.processEvents()


def spin(qapp, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.002)
    assert predicate()


def link(saved):
    client = socket.create_connection((saved["host"], saved["port"]), timeout=1)
    client.setblocking(False)
    return client


def send(client, saved, request_id="one", method="get_status", params=None, **changes):
    value = dict(id=request_id, token=saved["token"], method=method, params=params or {})
    value.update(changes)
    client.sendall((json.dumps(value) + "\n").encode())


def receive(qapp, client):
    buffer = bytearray()
    def ready():
        try:
            buffer.extend(client.recv(MAX_MESSAGE_BYTES))
        except BlockingIOError:
            pass
        return b"\n" in buffer
    spin(qapp, ready)
    return json.loads(buffer.split(b"\n")[0])


def test_endpoint_loopback_single_instance_cleanup(endpoint, qapp, tmp_path):
    bridge, service, saved = endpoint
    assert saved["host"] == "127.0.0.1" and saved["port"] > 0
    assert len(saved["token"]) >= 32
    second = AutomationBridge(FakeService(), tmp_path)
    assert not second.start()
    second.close()
    assert bridge.endpoint_path.exists()
    bridge.close()
    assert not bridge.endpoint_path.exists()
    assert second.start()
    second.close()


def test_stable_identity_gui_thread_and_disconnect(endpoint, qapp):
    bridge, service, saved = endpoint
    client = link(saved)
    try:
        send(client, saved)
        assert receive(qapp, client)["result"]["ok"]
        send(client, saved, "two", "heartbeat")
        receive(qapp, client)
        assert service.calls[0][2] == service.calls[1][2]
        assert service.thread_ids == [threading.get_ident()] * 2
    finally:
        client.close()
    spin(qapp, lambda: len(service.lost) == 1)
    assert service.lost == [service.calls[0][2]]


@pytest.mark.parametrize("bad", [b"not-json\n", b"[]\n", b"x" * (MAX_MESSAGE_BYTES + 1)],
                         ids=["invalid_json", "not_object", "oversized"])
def test_malformed_or_oversized_not_dispatched(endpoint, qapp, bad):
    bridge, service, saved = endpoint
    client = link(saved)
    spin(qapp, lambda: len(bridge._clients) == 1)
    client.sendall(bad)
    spin(qapp, lambda: not bridge._clients)
    assert not service.calls
    client.close()


def test_bad_token_and_raw_commands_refused(endpoint, qapp):
    bridge, service, saved = endpoint
    client = link(saved)
    spin(qapp, lambda: len(bridge._clients) == 1)
    send(client, saved, token="wrong")
    spin(qapp, lambda: not bridge._clients)
    assert not service.calls
    client.close()
    client = link(saved)
    send(client, saved, method="set_pwm")
    assert receive(qapp, client)["result"]["error"] == "invalid_request"
    assert not service.calls
    client.close()


def test_duplicate_request_not_reexecuted_and_large_reply(endpoint, qapp):
    bridge, service, saved = endpoint
    client = link(saved)
    send(client, saved)
    receive(qapp, client)
    send(client, saved)
    assert receive(qapp, client)["result"]["error"] == "duplicate_request"
    assert len(service.calls) == 1
    send(client, saved, "large", params={"huge": True})
    assert receive(qapp, client)["result"]["error"] == "response_too_large"
    client.close()


def test_heartbeat_expiry_notifies_owner_once(endpoint, qapp):
    bridge, service, saved = endpoint
    client = link(saved)
    send(client, saved, method="heartbeat")
    receive(qapp, client)
    state = next(iter(bridge._clients.values()))
    state.last_seen -= 3.1
    bridge._expire_clients()
    assert service.lost == [state.client_id]
    bridge._expire_clients()
    assert len(service.lost) == 1
    client.close()


def test_connection_count_bounded(endpoint, qapp):
    bridge, service, saved = endpoint
    clients = []
    try:
        for _ in range(MAX_CLIENTS + 2):
            clients.append(link(saved))
            qapp.processEvents()
        spin(qapp, lambda: len(bridge._clients) == MAX_CLIENTS)
        assert len(bridge._clients) == MAX_CLIENTS
    finally:
        for client in clients:
            client.close()
