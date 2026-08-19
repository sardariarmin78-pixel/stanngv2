"""
StanNG - Minimal VLESS-over-WebSocket forwarding engine.

Implements just enough of the VLESS protocol (proxy protocol v0, no encryption
layer beyond outer TLS which is terminated by the platform / Cloudflare edge)
to relay a client's WebSocket stream to the requested remote destination.

Supports CMD_TCP (stream) and CMD_UDP (length-prefixed datagrams, as used by
xray/v2ray for DNS and QUIC). Mux is rejected — clients fall back transparently.

This is intentionally dependency-free (uses asyncio streams only) so the whole
panel stays a single Python process / single service, per project scope.
"""
import asyncio
import ipaddress
import socket
import struct
import time
from typing import Callable, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

VLESS_VERSION = 0

CMD_TCP = 1
CMD_UDP = 2
CMD_MUX = 3

ATYPE_IPV4 = 1
ATYPE_DOMAIN = 2
ATYPE_IPV6 = 3

# Read chunk for the remote->client direction.
READ_CHUNK = 64 * 1024
# Largest single UDP datagram we will forward.
MAX_UDP_PACKET = 65535
# Flush traffic accounting after this many bytes, or this many seconds,
# whichever comes first. Keeps the shared counters fresh without waking a
# per-connection timer task every second.
TRAFFIC_FLUSH_BYTES = 512 * 1024
TRAFFIC_FLUSH_SECONDS = 2.0

# Networks that a proxied client must never be able to reach through us.
# Beyond the ranges ipaddress already flags, these cover carrier-grade NAT and
# the cloud instance metadata services (AWS/GCP/Azure/DO all use 169.254.169.254,
# which is link-local and therefore also caught below — listed for clarity).
_EXTRA_BLOCKED = (
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT / shared address space
    ipaddress.ip_network("192.0.0.0/24"),    # IETF protocol assignments
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("64:ff9b::/96"),    # NAT64 — can be used to reach v4 privates
)


class VlessError(Exception):
    """Raised for a malformed or unsupported VLESS request."""


class VlessHeader:
    __slots__ = ("uuid", "cmd", "port", "atype", "addr", "header_len")

    def __init__(self, uuid: str, cmd: int, port: int, atype: int, addr: str, header_len: int):
        self.uuid = uuid
        self.cmd = cmd
        self.port = port
        self.atype = atype
        self.addr = addr
        self.header_len = header_len


def _fmt_uuid(b: bytes) -> str:
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def parse_vless_header(buf: bytes) -> Optional[VlessHeader]:
    """Parse the initial VLESS request header from the first WS message.

    Returns None when the buffer is not a well-formed VLESS v0 request. Every
    field read is bounds-checked first: the buffer is fully attacker-controlled.
    """
    if len(buf) < 24:
        return None
    pos = 0
    if buf[pos] != VLESS_VERSION:
        return None
    pos += 1

    uuid_str = _fmt_uuid(buf[pos:pos + 16])
    pos += 16

    addon_len = buf[pos]
    pos += 1
    pos += addon_len  # protobuf addons (flow control); unused by this engine
    if pos >= len(buf):
        return None

    cmd = buf[pos]
    pos += 1
    # Mux (3) would need a whole sub-protocol; refusing it makes clients fall
    # back to plain connections rather than silently corrupting the stream.
    if cmd not in (CMD_TCP, CMD_UDP):
        return None

    if pos + 2 > len(buf):
        return None
    port = struct.unpack(">H", buf[pos:pos + 2])[0]
    pos += 2
    if port == 0:
        return None

    if pos >= len(buf):
        return None
    atype = buf[pos]
    pos += 1

    if atype == ATYPE_IPV4:
        if pos + 4 > len(buf):
            return None
        addr = socket.inet_ntoa(buf[pos:pos + 4])
        pos += 4
    elif atype == ATYPE_DOMAIN:
        if pos >= len(buf):
            return None
        dlen = buf[pos]
        pos += 1
        if dlen == 0 or pos + dlen > len(buf):
            return None
        try:
            addr = buf[pos:pos + dlen].decode("idna")
        except (UnicodeError, UnicodeDecodeError):
            addr = buf[pos:pos + dlen].decode("utf-8", errors="ignore")
        if not addr:
            return None
        pos += dlen
    elif atype == ATYPE_IPV6:
        if pos + 16 > len(buf):
            return None
        addr = socket.inet_ntop(socket.AF_INET6, buf[pos:pos + 16])
        pos += 16
    else:
        return None

    return VlessHeader(uuid_str, cmd, port, atype, addr, pos)


# ------------------------------------------------------------------ SSRF guard
def _is_public_ip(ip) -> bool:
    """True only for addresses that are safe to proxy a stranger's traffic to."""
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:127.0.0.1 must not slip past the IPv4 checks.
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED if ip.version == net.version)


async def resolve_target(host: str, port: int, allow_private: bool = False) -> list:
    """Resolve `host` and return the addrinfo entries that are safe to dial.

    Resolution happens once here and the caller dials the returned IP literal,
    so a hostile DNS server cannot answer with a public IP for the check and a
    private one for the connect (DNS rebinding).
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise VlessError(f"dns-failed: {host}") from e

    if allow_private:
        return infos

    safe = []
    for family, socktype, proto, canonname, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_public_ip(ip):
            safe.append((family, socktype, proto, canonname, sockaddr))
    if not safe:
        raise VlessError(f"blocked-destination: {host}:{port}")
    return safe


class TrafficCounter:
    """Byte counter for one session, with threshold-based reporting.

    The old engine woke a timer task once a second for every live connection
    just to sample these numbers. Reporting from the data path instead costs a
    couple of integer compares per chunk and scales with traffic, not with the
    number of idle connections.
    """

    def __init__(self, on_traffic: Optional[Callable[[int, int], None]] = None):
        self.up = 0
        self.down = 0
        self._on_traffic = on_traffic
        self._sent_up = 0
        self._sent_down = 0
        self._last_flush = time.monotonic()
        self.last_activity = time.monotonic()

    def add_up(self, n: int):
        self.up += n
        self.last_activity = time.monotonic()
        self._maybe_flush()

    def add_down(self, n: int):
        self.down += n
        self.last_activity = time.monotonic()
        self._maybe_flush()

    def _maybe_flush(self):
        du = self.up - self._sent_up
        dd = self.down - self._sent_down
        if du + dd >= TRAFFIC_FLUSH_BYTES or (
            (du or dd) and time.monotonic() - self._last_flush >= TRAFFIC_FLUSH_SECONDS
        ):
            self.flush()

    def flush(self):
        du = self.up - self._sent_up
        dd = self.down - self._sent_down
        if not (du or dd):
            return
        self._sent_up = self.up
        self._sent_down = self.down
        self._last_flush = time.monotonic()
        if self._on_traffic:
            try:
                self._on_traffic(du, dd)
            except Exception:
                pass


async def _ws_recv_bytes(ws: WebSocket) -> Optional[bytes]:
    """Receive one WS frame as bytes; None signals a closed connection."""
    msg = await ws.receive()
    if msg.get("type") == "websocket.disconnect":
        return None
    data = msg.get("bytes")
    if data is None:
        text = msg.get("text")
        if text is None:
            return b""
        data = text.encode("utf-8")
    return data


# ------------------------------------------------------------------ TCP relay
async def _pump_ws_to_tcp(ws: WebSocket, writer: asyncio.StreamWriter,
                          counter: TrafficCounter, first_payload: bytes):
    try:
        if first_payload:
            writer.write(first_payload)
            counter.add_up(len(first_payload))
            await writer.drain()
        while True:
            data = await _ws_recv_bytes(ws)
            if data is None:
                break
            if not data:
                continue
            writer.write(data)
            counter.add_up(len(data))
            await writer.drain()
    except (asyncio.CancelledError, ConnectionError, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _pump_tcp_to_ws(ws: WebSocket, reader: asyncio.StreamReader, counter: TrafficCounter):
    try:
        while True:
            data = await reader.read(READ_CHUNK)
            if not data:
                break
            if ws.application_state != WebSocketState.CONNECTED:
                break
            await ws.send_bytes(data)
            counter.add_down(len(data))
    except (asyncio.CancelledError, ConnectionError, RuntimeError):
        pass
    except Exception:
        pass


async def _relay_tcp(ws: WebSocket, header: VlessHeader, payload: bytes,
                     counter: TrafficCounter, connect_timeout: float,
                     idle_timeout: float, allow_private: bool):
    infos = await resolve_target(header.addr, header.port, allow_private)

    reader = writer = None
    last_err = None
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host=sockaddr[0], port=sockaddr[1], family=family),
                timeout=connect_timeout,
            )
            break
        except Exception as e:  # try the next resolved address
            last_err = e
            reader = writer = None
    if writer is None:
        raise VlessError(f"connect-failed: {header.addr}:{header.port} ({last_err})")

    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass

    # The VLESS response header must go out as soon as the upstream connect
    # succeeds. The previous version piggybacked it onto the first byte of
    # response data, so any protocol where the server stays silent until the
    # client speaks (and the client waits for our ack first) deadlocked.
    await ws.send_bytes(bytes([VLESS_VERSION, 0]))

    up = asyncio.create_task(_pump_ws_to_tcp(ws, writer, counter, payload))
    down = asyncio.create_task(_pump_tcp_to_ws(ws, reader, counter))
    try:
        await _await_with_idle_timeout({up, down}, counter, idle_timeout)
    finally:
        for t in (up, down):
            if not t.done():
                t.cancel()
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        except Exception:
            pass


# ------------------------------------------------------------------ UDP relay
class _UdpProto(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    def datagram_received(self, data, addr):
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            pass  # drop, as UDP is allowed to

    def error_received(self, exc):
        pass


def _iter_udp_packets(buf: bytearray):
    """Yield complete length-prefixed datagrams, leaving any partial tail."""
    while len(buf) >= 2:
        length = struct.unpack(">H", buf[:2])[0]
        if len(buf) < 2 + length:
            break
        packet = bytes(buf[2:2 + length])
        del buf[:2 + length]
        yield packet


async def _relay_udp(ws: WebSocket, header: VlessHeader, payload: bytes,
                     counter: TrafficCounter, idle_timeout: float, allow_private: bool):
    """VLESS UDP association: 2-byte big-endian length prefix per datagram.

    The old engine handed UDP requests to asyncio.open_connection, i.e. it
    opened a TCP socket to a UDP port and fed it length-prefixed frames — so
    plain DNS over the tunnel never worked.
    """
    loop = asyncio.get_running_loop()
    infos = await resolve_target(header.addr, header.port, allow_private)
    family, _st, _p, _c, sockaddr = infos[0]

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    transport, _proto = await loop.create_datagram_endpoint(
        lambda: _UdpProto(queue), remote_addr=(sockaddr[0], sockaddr[1]), family=family
    )

    await ws.send_bytes(bytes([VLESS_VERSION, 0]))

    async def ws_to_udp():
        buf = bytearray(payload)
        try:
            for packet in _iter_udp_packets(buf):
                transport.sendto(packet)
                counter.add_up(len(packet))
            while True:
                data = await _ws_recv_bytes(ws)
                if data is None:
                    break
                buf.extend(data)
                if len(buf) > MAX_UDP_PACKET * 4:
                    break  # client is not framing correctly; stop rather than grow
                for packet in _iter_udp_packets(buf):
                    transport.sendto(packet)
                    counter.add_up(len(packet))
        except Exception:
            pass

    async def udp_to_ws():
        try:
            while True:
                data = await queue.get()
                if ws.application_state != WebSocketState.CONNECTED:
                    break
                await ws.send_bytes(struct.pack(">H", len(data)) + data)
                counter.add_down(len(data))
        except Exception:
            pass

    up = asyncio.create_task(ws_to_udp())
    down = asyncio.create_task(udp_to_ws())
    try:
        await _await_with_idle_timeout({up, down}, counter, idle_timeout)
    finally:
        for t in (up, down):
            if not t.done():
                t.cancel()
        try:
            transport.close()
        except Exception:
            pass


# ------------------------------------------------------------------ shared
async def _await_with_idle_timeout(tasks: set, counter: TrafficCounter, idle_timeout: float):
    """Wait for either pump to finish, tearing the session down if it goes idle.

    Without this, a half-dead connection (client gone, no TCP RST — common on
    mobile) held its slot against the user's max_connections limit forever.
    """
    if idle_timeout <= 0:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return
    while True:
        done, pending = await asyncio.wait(
            tasks, timeout=idle_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if done:
            return
        if time.monotonic() - counter.last_activity >= idle_timeout:
            return


async def relay(ws: WebSocket, uuid_str: str, on_traffic,
                connect_timeout: float = 8.0, idle_timeout: float = 600.0,
                allow_private: bool = False) -> TrafficCounter:
    """Relay an accepted VLESS-over-WebSocket session.

    Expects the first frame to carry the VLESS request header. `on_traffic(up,
    down)` receives byte deltas as they accumulate.
    """
    counter = TrafficCounter(on_traffic)
    try:
        raw = await _ws_recv_bytes(ws)
        if not raw:
            return counter

        header = parse_vless_header(raw)
        if header is None or header.uuid != uuid_str:
            await _close(ws, 1008)
            return counter

        payload = raw[header.header_len:]

        if header.cmd == CMD_UDP:
            await _relay_udp(ws, header, payload, counter, idle_timeout, allow_private)
        else:
            await _relay_tcp(ws, header, payload, counter,
                             connect_timeout, idle_timeout, allow_private)
    except VlessError:
        await _close(ws, 1011)
    except asyncio.CancelledError:
        raise
    except Exception:
        await _close(ws, 1011)
    finally:
        counter.flush()
        await _close(ws, 1000)
    return counter


async def _close(ws: WebSocket, code: int):
    try:
        if ws.application_state == WebSocketState.CONNECTED:
            await ws.close(code=code)
    except Exception:
        pass
