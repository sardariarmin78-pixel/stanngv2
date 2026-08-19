"""
End-to-end relay tests: real bytes through a real WebSocket.

These drive the actual /ws/{uid} endpoint against a throwaway TCP/UDP server on
loopback, so they exercise the VLESS handshake, the bidirectional pumps, traffic
accounting and the connection limits rather than just the parser.

Loopback is normally blocked by the SSRF guard, so these tests opt in via the
allow_private_destinations setting — which is itself the thing under test.
"""
import socket
import struct
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main  # noqa: E402

ADMIN = {"username": "relayadmin", "password": "correct horse battery"}


# ------------------------------------------------------------------ helpers
class EchoServer:
    """Trivial TCP server that echoes back whatever it receives, uppercased."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            while True:
                try:
                    data = conn.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    conn.sendall(data.upper())
                except OSError:
                    return

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def vless_request(uuid_str: str, host: str, port: int, cmd: int = 1) -> bytes:
    """Build a VLESS v0 request header for an IPv4 destination."""
    body = bytes([0])
    body += bytes.fromhex(uuid_str.replace("-", ""))
    body += bytes([0])          # no addons
    body += bytes([cmd])
    body += struct.pack(">H", port)
    body += bytes([1])          # atype = IPv4
    body += socket.inet_aton(host)
    return body


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        # Loopback targets are refused by default; that is the point of the guard.
        c.post("/api/settings", json={"allow_private_destinations": True})
        yield c


@pytest.fixture
def echo():
    s = EchoServer()
    yield s
    s.close()


def make_user(client, **kw):
    payload = {"name": "relay-user"}
    payload.update(kw)
    r = client.post("/api/inbounds", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["inbound"]


# ------------------------------------------------------------------ tests
def test_relay_round_trip(client, echo):
    ib = make_user(client)
    with client.websocket_connect(f"/ws/{ib['uid']}") as ws:
        ws.send_bytes(vless_request(ib["uuid"], "127.0.0.1", echo.port) + b"hello")
        # The VLESS response header must arrive as soon as the upstream connect
        # succeeds — not glued onto the first byte of response data.
        assert ws.receive_bytes() == bytes([0, 0])
        assert ws.receive_bytes() == b"HELLO"

        ws.send_bytes(b"again")
        assert ws.receive_bytes() == b"AGAIN"


def test_traffic_is_accounted(client, echo):
    ib = make_user(client)
    uid = ib["uid"]
    main.runtime["pending_traffic"].pop(uid, None)

    with client.websocket_connect(f"/ws/{uid}") as ws:
        ws.send_bytes(vless_request(ib["uuid"], "127.0.0.1", echo.port) + b"abcdef")
        assert ws.receive_bytes() == bytes([0, 0])
        assert ws.receive_bytes() == b"ABCDEF"

    deadline = time.time() + 5
    while time.time() < deadline:
        pending = main.runtime["pending_traffic"].get(uid)
        rec = main.inbound_by_uid(main.store.get_sync(), uid)
        total = (rec.get("used_up", 0) + rec.get("used_down", 0)
                 + (pending["up"] + pending["down"] if pending else 0))
        if total >= 12:  # 6 bytes up + 6 down
            break
        time.sleep(0.1)
    assert total >= 12, f"traffic not accounted: {total}"


def test_wrong_uuid_is_rejected(client, echo):
    ib = make_user(client)
    wrong = "11111111-2222-3333-4444-555555555555"
    with client.websocket_connect(f"/ws/{ib['uid']}") as ws:
        ws.send_bytes(vless_request(wrong, "127.0.0.1", echo.port) + b"hello")
        with pytest.raises(Exception):
            # closed with 1008 rather than proxying anything
            while True:
                ws.receive_bytes()


def test_disabled_user_cannot_connect(client, echo):
    ib = make_user(client)
    client.patch(f"/api/inbounds/{ib['uid']}", json={"enabled": False})
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/{ib['uid']}"):
            pass


def test_expired_user_cannot_connect(client, echo):
    ib = make_user(client, expire_days=1)
    rec = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    rec["expire_at"] = 1000.0  # far in the past
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/{ib['uid']}"):
            pass


def test_over_quota_user_cannot_connect(client, echo):
    ib = make_user(client, quota_gb=1)
    rec = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    rec["used_down"] = 2 * 1024 ** 3
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/{ib['uid']}"):
            pass


def test_unknown_uid_is_rejected(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/deadbeefdeadbeef"):
            pass


def test_max_connections_enforced(client, echo):
    ib = make_user(client, max_connections=1)
    uid = ib["uid"]
    with client.websocket_connect(f"/ws/{uid}") as ws1:
        ws1.send_bytes(vless_request(ib["uuid"], "127.0.0.1", echo.port) + b"a")
        assert ws1.receive_bytes() == bytes([0, 0])
        assert ws1.receive_bytes() == b"A"
        assert len(main.runtime["active"].get(uid, {})) == 1

        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/{uid}"):
                pass


def test_active_ips_are_reported(client, echo):
    ib = make_user(client)
    uid = ib["uid"]
    with client.websocket_connect(f"/ws/{uid}") as ws:
        ws.send_bytes(vless_request(ib["uuid"], "127.0.0.1", echo.port) + b"a")
        assert ws.receive_bytes() == bytes([0, 0])
        assert ws.receive_bytes() == b"A"
        listed = client.get("/api/inbounds").json()["inbounds"]
        row = next(i for i in listed if i["uid"] == uid)
        # serialize_inbound used to hardcode this to None.
        assert row["active_ips"], "expected at least one connected IP"
        assert row["status"]["active_connections"] == 1


def test_ssrf_guard_blocks_loopback_when_not_opted_in(client, echo):
    """Flip the guard back on and confirm loopback is refused."""
    client.post("/api/settings", json={"allow_private_destinations": False})
    try:
        ib = make_user(client)
        with client.websocket_connect(f"/ws/{ib['uid']}") as ws:
            ws.send_bytes(vless_request(ib["uuid"], "127.0.0.1", echo.port) + b"hello")
            with pytest.raises(Exception):
                while True:
                    data = ws.receive_bytes()
                    assert data != b"HELLO", "SSRF guard let loopback traffic through"
    finally:
        client.post("/api/settings", json={"allow_private_destinations": True})


def test_udp_association(client):
    """UDP used to be handed to open_connection, i.e. a TCP socket on a UDP
    port, so plain DNS through the tunnel never worked."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def responder():
        try:
            data, addr = srv.recvfrom(4096)
            srv.sendto(data.upper(), addr)
        except OSError:
            pass

    threading.Thread(target=responder, daemon=True).start()

    ib = make_user(client)
    try:
        with client.websocket_connect(f"/ws/{ib['uid']}") as ws:
            payload = struct.pack(">H", 5) + b"hello"
            ws.send_bytes(vless_request(ib["uuid"], "127.0.0.1", port, cmd=2) + payload)
            assert ws.receive_bytes() == bytes([0, 0])
            reply = ws.receive_bytes()
            assert reply == struct.pack(">H", 5) + b"HELLO"
    finally:
        srv.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ------------------------------------------------------------------ live enforcement
def test_quota_exceeded_mid_session_disconnects(client, echo):
    """The headline fix: limits are now enforced *during* a session.

    Previously live_enabled was only consulted at handshake time, so a single
    long-lived connection could move unlimited data on a capped plan.
    """
    ib = make_user(client, quota_gb=1)
    uid = ib["uid"]

    with client.websocket_connect(f"/ws/{uid}") as ws:
        ws.send_bytes(vless_request(ib["uuid"], "127.0.0.1", echo.port) + b"hi")
        assert ws.receive_bytes() == bytes([0, 0])
        assert ws.receive_bytes() == b"HI"
        assert len(main.runtime["active"].get(uid, {})) == 1

        # Blow past the quota while the tunnel is still open.
        rec = main.inbound_by_uid(main.store.get_sync(), uid)
        rec["used_down"] = 5 * 1024 ** 3

        # Run one enforcement sweep on the app's own event loop.
        kicked = client.portal.call(main._enforce_once)
        assert uid in kicked

        with pytest.raises(Exception):
            while True:
                ws.receive_bytes()

    deadline = time.time() + 5
    while time.time() < deadline and main.runtime["active"].get(uid):
        time.sleep(0.1)
    assert not main.runtime["active"].get(uid), "session was not torn down"


def test_healthy_session_is_not_disconnected(client, echo):
    ib = make_user(client, quota_gb=100)
    uid = ib["uid"]
    with client.websocket_connect(f"/ws/{uid}") as ws:
        ws.send_bytes(vless_request(ib["uuid"], "127.0.0.1", echo.port) + b"hi")
        assert ws.receive_bytes() == bytes([0, 0])
        assert ws.receive_bytes() == b"HI"

        assert client.portal.call(main._enforce_once) == []
        ws.send_bytes(b"still here")
        assert ws.receive_bytes() == b"STILL HERE"
