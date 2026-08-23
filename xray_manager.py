"""
Xray-core data plane.

The pure-Python relay is capped at one core and only ever spoke VLESS over
WebSocket. Xray is a Go core that speaks the modern transports — notably
XHTTP, which survives CDN inspection far better than WebSocket — and does the
byte shuffling far faster.

The panel keeps its Python relay as a fallback: when the xray binary is not
present (a plain `python main.py` checkout, or the test suite) everything
still works exactly as before. Xray takes over only when it is actually
installed.

Two things are done deliberately differently from the upstream version this
was adapted from:

1. **Users are added and removed live.** Upstream regenerates the config and
   restarts xray on every user create, edit, delete and UUID rotation — which
   drops every other user's connection each time an account is touched. Here
   the gRPC HandlerService is used instead, and a full reload is only the
   fallback when that fails.

2. **Private destinations are blocked.** Upstream ships routing rules that
   only tag the stats API, so a proxied client can reach 127.0.0.1 and the
   cloud metadata endpoint through the tunnel — the same class of hole the
   Python relay was fixed for. The rules below blackhole those ranges.
"""
import asyncio
import json
import os
import subprocess
from typing import Dict, List, Optional

XRAY_BIN = os.environ.get("PEYK_XRAY_BIN", "/usr/local/bin/xray")
XRAY_CONFIG = os.environ.get("PEYK_XRAY_CONFIG", "/usr/local/bin/config.json")
API_ADDR = "127.0.0.1:10085"

# Inbound tags and the loopback ports nginx forwards to.
TRANSPORTS = {
    "vless-ws": {"tag": "inbound-vless-ws", "port": 10001, "path": "/vl-ws",
                 "protocol": "vless", "network": "ws"},
    "vmess-ws": {"tag": "inbound-vmess-ws", "port": 10002, "path": "/vm-ws",
                 "protocol": "vmess", "network": "ws"},
    "vless-xhttp": {"tag": "inbound-vless-xhttp", "port": 10004, "path": "/vl-xhttp",
                    "protocol": "vless", "network": "xhttp"},
}
DEFAULT_TRANSPORTS = ["vless-ws", "vless-xhttp"]

# Ranges a proxied client must never reach through us. Written as literal
# CIDRs rather than geoip:private so the rules work even if geoip.dat is
# missing from the image.
PRIVATE_RANGES = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::1/128", "fc00::/7", "fe80::/10",
]

_process: Optional[subprocess.Popen] = None
# The uids xray is currently serving. There is no API to ask it, so this
# mirrors what has been pushed in; the reconciler diffs the panel against it.
_live_uids: set = set()
_previous_stats: Dict[str, dict] = {}
_counters_valid = False


# ------------------------------------------------------------------ availability
def available() -> bool:
    """True when a real xray binary is installed and executable."""
    return bool(XRAY_BIN) and os.path.isfile(XRAY_BIN) and os.access(XRAY_BIN, os.X_OK)


def enabled_transports(settings: dict) -> List[str]:
    chosen = (settings or {}).get("xray_transports")
    if not isinstance(chosen, list) or not chosen:
        return list(DEFAULT_TRANSPORTS)
    return [t for t in chosen if t in TRANSPORTS] or list(DEFAULT_TRANSPORTS)


# ------------------------------------------------------------------ config
def _client(protocol: str, ib: dict) -> dict:
    # email is the panel's uid, which is what makes per-user stats resolvable.
    client = {"id": ib["uuid"], "email": ib["uid"], "level": 0}
    if protocol == "vless":
        client["flow"] = ""
    return client


def _inbound(kind: str, inbounds: List[dict]) -> dict:
    spec = TRANSPORTS[kind]
    clients = [_client(spec["protocol"], ib) for ib in inbounds
               if ib.get("enabled", True) and ib.get("uuid") and ib.get("uid")]
    settings = {"clients": clients}
    if spec["protocol"] == "vless":
        settings["decryption"] = "none"

    stream = {"network": spec["network"]}
    if spec["network"] == "ws":
        stream["wsSettings"] = {"path": spec["path"]}
    else:
        stream["xhttpSettings"] = {"path": spec["path"]}

    return {
        "listen": "127.0.0.1",
        "port": spec["port"],
        "protocol": spec["protocol"],
        "settings": settings,
        "streamSettings": stream,
        "tag": spec["tag"],
    }


def build_config(inbounds: List[dict], settings: Optional[dict] = None) -> dict:
    settings = settings or {}
    kinds = enabled_transports(settings)

    return {
        "log": {"loglevel": "warning"},
        # DNS-over-HTTPS first: plain DNS is trivially tampered with on the
        # networks this is meant to be used from.
        "dns": {
            "servers": [
                "https+local://1.1.1.1/dns-query",
                "https+local://8.8.8.8/dns-query",
                "1.1.1.1",
                "8.8.8.8",
                "localhost",
            ],
            "queryStrategy": "UseIPv4",
        },
        "api": {"tag": "api", "services": ["StatsService", "HandlerService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
                "tag": "api",
            },
        ] + [_inbound(k, inbounds) for k in kinds],
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "blocked"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                # Without this a proxied client reaches the container's own
                # services and the cloud metadata endpoint.
                {"type": "field", "ip": PRIVATE_RANGES, "outboundTag": "blocked"},
            ],
        },
    }


def write_config(inbounds: List[dict], settings: Optional[dict] = None) -> str:
    config = build_config(inbounds, settings)
    os.makedirs(os.path.dirname(XRAY_CONFIG) or ".", exist_ok=True)
    tmp = f"{XRAY_CONFIG}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, XRAY_CONFIG)
    return XRAY_CONFIG


# ------------------------------------------------------------------ process
def start(inbounds: List[dict], settings: Optional[dict] = None) -> bool:
    """Write the config and launch xray. False when no binary is installed."""
    global _process, _previous_stats, _counters_valid, _live_uids
    if not available():
        return False
    write_config(inbounds, settings)
    _live_uids = {ib["uid"] for ib in inbounds
                  if ib.get("enabled", True) and ib.get("uuid") and ib.get("uid")}
    stop()
    _previous_stats = {}
    # Fresh process means counters restart at zero; the first stats poll must
    # therefore establish a baseline rather than be read as a delta.
    _counters_valid = False
    _process = subprocess.Popen(
        [XRAY_BIN, "run", "-c", XRAY_CONFIG],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return True


def stop():
    global _process, _live_uids
    if _process and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None
    _live_uids = set()


def running() -> bool:
    return _process is not None and _process.poll() is None


def reload(inbounds: List[dict], settings: Optional[dict] = None) -> bool:
    """Full restart. Drops every live connection, so use it only as a fallback."""
    return start(inbounds, settings)


# ------------------------------------------------------------------ live users
async def _api(*args, timeout: float = 10) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        XRAY_BIN, "api", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", "timeout"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def add_user(ib: dict, settings: Optional[dict] = None) -> bool:
    """Add one user to every enabled inbound without restarting xray.

    This is the whole point of using the API: a restart would disconnect every
    other user just because one account was created.
    """
    if not running():
        return False
    ok = True
    for kind in enabled_transports(settings or {}):
        spec = TRANSPORTS[kind]
        payload = {
            "tag": spec["tag"],
            "users": [{
                "email": ib["uid"],
                "level": 0,
                spec["protocol"]: ({"id": ib["uuid"], "flow": ""}
                                   if spec["protocol"] == "vless"
                                   else {"id": ib["uuid"], "security": "auto"}),
            }],
        }
        code, _out, _err = await _api("adu", f"--server={API_ADDR}", json.dumps(payload))
        ok = ok and code == 0
    if ok:
        _live_uids.add(ib["uid"])
    return ok


async def remove_user(uid: str, settings: Optional[dict] = None) -> bool:
    if not running():
        return False
    ok = True
    for kind in enabled_transports(settings or {}):
        spec = TRANSPORTS[kind]
        payload = {"tag": spec["tag"], "email": uid}
        code, _out, _err = await _api("rmu", f"--server={API_ADDR}", json.dumps(payload))
        ok = ok and code == 0
    # Discarded even on a partial failure: the reconciler will put it back if
    # xray really still has it, and leaving a stale entry here would make a
    # genuinely absent user look present forever.
    _live_uids.discard(uid)
    return ok


async def sync_user(ib: dict, settings: Optional[dict] = None) -> bool:
    """Apply an edit: remove then re-add, so a rotated UUID takes effect."""
    await remove_user(ib["uid"], settings)
    return await add_user(ib, settings)


# ------------------------------------------------------------------ stats
def _parse_stats(raw: str) -> Dict[str, dict]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        import re
        matches = re.findall(r'name:\s*"([^"]+)"\s*value:\s*(\d+)', raw or "")
        if not matches:
            return {}
        data = {"stat": [{"name": n, "value": int(v)} for n, v in matches]}

    totals: Dict[str, dict] = {}
    for item in data.get("stat") or []:
        name, value = item.get("name"), item.get("value")
        if not name or value is None:
            continue
        parts = name.split(">>>")
        # user>>><email>>>>traffic>>>uplink
        if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
            bucket = totals.setdefault(parts[1], {"up": 0, "down": 0})
            try:
                amount = int(value)
            except (TypeError, ValueError):
                continue
            if parts[3] == "uplink":
                bucket["up"] += amount
            elif parts[3] == "downlink":
                bucket["down"] += amount
    return totals


async def stats_deltas() -> Dict[str, dict]:
    """Bytes transferred since the previous call, per uid.

    Xray's counters are cumulative for the life of the process. The first poll
    after a start only establishes a baseline: treating it as a delta would
    book the entire history again every time the panel restarts.
    """
    global _previous_stats, _counters_valid
    if not running():
        return {}

    code, out, _err = await _api("statsquery", f"--server={API_ADDR}")
    if code != 0 or not (out or "").strip():
        return {}

    current = _parse_stats(out)
    if not _counters_valid:
        _previous_stats = current
        _counters_valid = True
        return {}

    deltas: Dict[str, dict] = {}
    for uid, totals in current.items():
        prev = _previous_stats.get(uid, {"up": 0, "down": 0})
        up = totals["up"] - prev["up"]
        down = totals["down"] - prev["down"]
        # A negative delta means xray restarted underneath us and its counters
        # went back to zero; the current value is then the delta.
        if up < 0:
            up = totals["up"]
        if down < 0:
            down = totals["down"]
        if up or down:
            deltas[uid] = {"up": up, "down": down}

    _previous_stats = current
    return deltas


def live_uids() -> set:
    """Snapshot of who xray is serving right now."""
    return set(_live_uids)


def version() -> str:
    if not available():
        return ""
    try:
        out = subprocess.run([XRAY_BIN, "version"], capture_output=True,
                             text=True, timeout=5).stdout
        return (out or "").splitlines()[0].strip()
    except Exception:
        return ""


def describe() -> dict:
    return {
        "available": available(),
        "running": running(),
        "version": version(),
        "binary": XRAY_BIN if available() else None,
        "transports": list(TRANSPORTS),
    }
