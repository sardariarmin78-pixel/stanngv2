"""
Subscription rendering for the client families people actually use.

A plain list of vless:// URLs is the lowest common denominator: the client
shows N servers and the user picks one by hand, with no idea which route is
alive. Clash and sing-box both express a latency-tested group, so with
several entry points configured the client silently moves to whichever one
is working — which is the whole point of having several.

YAML is emitted directly rather than via PyYAML: the document shape is fixed
and known, and the project's "single service, no extra dependencies" rule is
worth more than a serialiser for one nested dict.
"""
import base64
import json
from typing import List, Optional

FORMAT_PLAIN = "plain"
FORMAT_BASE64 = "base64"
FORMAT_CLASH = "clash"
FORMAT_SINGBOX = "singbox"

FORMATS = (FORMAT_PLAIN, FORMAT_BASE64, FORMAT_CLASH, FORMAT_SINGBOX)

CONTENT_TYPES = {
    FORMAT_PLAIN: "text/plain; charset=utf-8",
    FORMAT_BASE64: "text/plain; charset=utf-8",
    FORMAT_CLASH: "text/yaml; charset=utf-8",
    FORMAT_SINGBOX: "application/json; charset=utf-8",
}

# Matched against a lowercased User-Agent. Order matters: the first hit wins,
# so more specific tokens must come before generic ones.
# Only clients that genuinely need a different document are switched. v2rayNG
# and friends are deliberately left on plain text: 1.4.1 moved them off base64
# to fix a parsing problem, and sniffing them back onto it would silently undo
# that. base64 stays available through an explicit ?format=base64.
_UA_HINTS = (
    (FORMAT_SINGBOX, ("sing-box", "singbox", "sfi/", "sfa/", "sfm/", "sft/",
                      "karing", "hiddify")),
    (FORMAT_CLASH, ("clash", "stash", "mihomo", "flclash")),
)

LATENCY_TEST_URL = "http://www.gstatic.com/generate_204"
LATENCY_INTERVAL = 300


def detect_format(explicit: Optional[str], user_agent: str = "") -> str:
    """Pick an output format.

    An explicit ?format= always wins. Otherwise the User-Agent decides, and
    anything unrecognised falls back to plain text — the format every client
    can read, and what this panel has always returned.
    """
    if explicit:
        explicit = explicit.strip().lower()
        aliases = {
            "v2ray": FORMAT_PLAIN, "text": FORMAT_PLAIN, "txt": FORMAT_PLAIN,
            "b64": FORMAT_BASE64,
            "clashmeta": FORMAT_CLASH, "clash-meta": FORMAT_CLASH, "meta": FORMAT_CLASH,
            "sing-box": FORMAT_SINGBOX, "sing_box": FORMAT_SINGBOX, "box": FORMAT_SINGBOX,
        }
        explicit = aliases.get(explicit, explicit)
        if explicit in FORMATS:
            return explicit

    ua = (user_agent or "").lower()
    for fmt, tokens in _UA_HINTS:
        if any(tok in ua for tok in tokens):
            return fmt
    return FORMAT_PLAIN


# ------------------------------------------------------------------ YAML
def _yaml_str(value) -> str:
    """Quote a scalar so any name survives: Persian text, colons, emoji, quotes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _unique_names(configs: List[dict]) -> List[str]:
    """Proxy names must be unique or Clash silently drops the duplicates."""
    seen = {}
    names = []
    for c in configs:
        base = (c.get("remark") or c.get("name") or c.get("address") or "proxy").strip()
        base = base.replace("\n", " ").strip() or "proxy"
        if base in seen:
            seen[base] += 1
            base = f"{base} #{seen[base]}"
        else:
            seen[base] = 1
        names.append(base)
    return names


def render_plain(configs: List[dict]) -> str:
    return "\n".join(c["link"] for c in configs)


def render_base64(configs: List[dict]) -> str:
    """Classic v2rayN/Shadowrocket form: the plain list, base64'd."""
    return base64.b64encode(render_plain(configs).encode("utf-8")).decode("ascii")


def render_clash(configs: List[dict], profile: str = "subscription") -> str:
    """Clash Meta YAML with a url-test group across every entry point."""
    names = _unique_names(configs)
    lines = [
        "# Generated subscription — do not edit by hand.",
        f"# profile: {profile}",
        "",
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        "log-level: info",
        "",
        "proxies:",
    ]
    for cfg, name in zip(configs, names):
        lines += [
            f"  - name: {_yaml_str(name)}",
            "    type: vless",
            f"    server: {_yaml_str(cfg['address'])}",
            f"    port: {int(cfg.get('port') or 443)}",
            f"    uuid: {_yaml_str(cfg['uuid'])}",
            "    udp: true",
            "    tls: true",
            "    skip-cert-verify: false",
            f"    servername: {_yaml_str(cfg.get('sni') or cfg['address'])}",
            "    network: ws",
        ]
        fp = (cfg.get("fp") or "").strip()
        if fp and fp != "random":
            lines.append(f"    client-fingerprint: {_yaml_str(fp)}")
        alpn = (cfg.get("alpn") or "").strip()
        if alpn:
            items = ", ".join(_yaml_str(a.strip()) for a in alpn.split(",") if a.strip())
            lines.append(f"    alpn: [{items}]")
        lines += [
            "    ws-opts:",
            f"      path: {_yaml_str(cfg['path'])}",
            "      headers:",
            f"        Host: {_yaml_str(cfg.get('host') or cfg['address'])}",
        ]

    quoted = [_yaml_str(n) for n in names]
    lines += [
        "",
        "proxy-groups:",
        f"  - name: {_yaml_str('Auto')}",
        "    type: url-test",
        f"    url: {_yaml_str(LATENCY_TEST_URL)}",
        f"    interval: {LATENCY_INTERVAL}",
        "    tolerance: 50",
        "    proxies:",
    ]
    lines += [f"      - {n}" for n in quoted]
    lines += [
        f"  - name: {_yaml_str('Select')}",
        "    type: select",
        "    proxies:",
        f"      - {_yaml_str('Auto')}",
    ]
    lines += [f"      - {n}" for n in quoted]
    lines += [
        "",
        "rules:",
        "  - GEOIP,private,DIRECT,no-resolve",
        # Rule targets are parsed by Clash as bare comma-separated fields, so
        # the group name must NOT be YAML-quoted here or it is looked up with
        # the quotes included. Safe because these names are fixed ASCII.
        "  - MATCH,Select",
        "",
    ]
    return "\n".join(lines)


def render_singbox(configs: List[dict], profile: str = "subscription") -> str:
    """sing-box JSON with a urltest outbound across every entry point."""
    names = _unique_names(configs)
    outbounds = []
    for cfg, name in zip(configs, names):
        ob = {
            "type": "vless",
            "tag": name,
            "server": cfg["address"],
            "server_port": int(cfg.get("port") or 443),
            "uuid": cfg["uuid"],
            "packet_encoding": "xudp",
            "tls": {
                "enabled": True,
                "server_name": cfg.get("sni") or cfg["address"],
                "insecure": False,
                "utls": {
                    "enabled": True,
                    "fingerprint": (cfg.get("fp") or "chrome") if
                    (cfg.get("fp") or "chrome") != "random" else "chrome",
                },
            },
            "transport": {
                "type": "ws",
                "path": cfg["path"],
                "headers": {"Host": cfg.get("host") or cfg["address"]},
            },
        }
        alpn = (cfg.get("alpn") or "").strip()
        if alpn:
            ob["tls"]["alpn"] = [a.strip() for a in alpn.split(",") if a.strip()]
        outbounds.append(ob)

    outbounds.append({
        "type": "urltest",
        "tag": "Auto",
        "outbounds": list(names),
        "url": LATENCY_TEST_URL,
        "interval": f"{LATENCY_INTERVAL}s",
        "tolerance": 50,
    })
    outbounds.append({
        "type": "selector",
        "tag": "Select",
        "outbounds": ["Auto"] + list(names),
        "default": "Auto",
    })
    outbounds.append({"type": "direct", "tag": "direct"})

    doc = {
        "log": {"level": "warn", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "remote", "address": "https://1.1.1.1/dns-query", "detour": "Select"},
                {"tag": "local", "address": "local", "detour": "direct"},
            ],
            "rules": [{"outbound": "any", "server": "local"}],
            "strategy": "prefer_ipv4",
        },
        "inbounds": [{
            "type": "mixed",
            "tag": "mixed-in",
            "listen": "127.0.0.1",
            "listen_port": 2080,
        }],
        "outbounds": outbounds,
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": "direct"},
            ],
            "final": "Select",
            "auto_detect_interface": True,
        },
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)


def render(fmt: str, configs: List[dict], profile: str = "subscription") -> str:
    if fmt == FORMAT_CLASH:
        return render_clash(configs, profile)
    if fmt == FORMAT_SINGBOX:
        return render_singbox(configs, profile)
    if fmt == FORMAT_BASE64:
        return render_base64(configs)
    return render_plain(configs)
