"""
Reading user lists exported from other panels.

Pure parsing: nothing here touches the database or the network, so every
format quirk below is covered by a test rather than by trying it on a live
migration.

Three shapes are understood.

  Marzban   {"users": [{"username", "data_limit", "expire", ...}]}
  3x-ui     {"clients": [{"email", "totalGB", "expiryTime", ...}]}, or a whole
            inbound row whose "settings" is a JSON *string* holding clients
  Peyk      our own backup, so a panel can be split or merged

What is deliberately NOT carried over is the credential. A subscription is
identified by host and path as much as by uuid, and neither of those survives
a move between panels, so an imported customer needs a fresh link either way.
Generating new ones keeps that honest instead of implying old configs still
work.
"""
import json
import re
import time
from typing import Optional

GB = 1024 ** 3

# Names are shown in the panel and used in config labels; keep them tame.
_NAME_CLEAN = re.compile(r"[\x00-\x1f\x7f]")
MAX_NAME = 64
MAX_NOTE = 200

# Anything past this in one file is a mistake, not a migration.
MAX_IMPORT = 5000


class ImportError_(ValueError):
    """Bad input, with a code the UI can translate."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _clean_name(raw, fallback: str) -> str:
    name = _NAME_CLEAN.sub("", str(raw or "")).strip()
    # 3x-ui identifies clients by email; the local part reads better as a name.
    if "@" in name:
        name = name.split("@")[0].strip() or name
    return (name or fallback)[:MAX_NAME]


def _as_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _seconds(ts) -> Optional[float]:
    """Normalise an expiry to a unix timestamp in seconds.

    Marzban stores seconds, 3x-ui milliseconds, and both use 0 for "never".
    A 3x-ui negative value means "N ms of runtime once first used", which has
    no equivalent here -- it becomes no expiry rather than a wrong date.
    """
    n = _as_int(ts, 0)
    if n <= 0:
        return None
    # Anything past the year 2200 in seconds is really milliseconds.
    if n > 7_258_118_400:
        n = n // 1000
    return float(n)


def _row(name, uuid_hint, quota_bytes, expire_at, used_bytes,
         enabled, note, max_connections=0) -> dict:
    quota_bytes = max(0, _as_int(quota_bytes))
    used_bytes = max(0, _as_int(used_bytes))
    return {
        "name": name,
        "quota_gb": round(quota_bytes / GB, 4) if quota_bytes else 0,
        "expire_at": expire_at,
        "used_down": used_bytes,     # folded into one side; the total is what matters
        "used_up": 0,
        "enabled": bool(enabled),
        "note": _NAME_CLEAN.sub("", str(note or "")).strip()[:MAX_NOTE],
        "max_connections": max(0, _as_int(max_connections)),
        "source_uuid": str(uuid_hint or "") or None,
    }


# ------------------------------------------------------------------ marzban
def _is_marzban(doc) -> bool:
    rows = doc.get("users") if isinstance(doc, dict) else doc
    if not isinstance(rows, list) or not rows:
        return False
    first = rows[0]
    return isinstance(first, dict) and (
        "username" in first or "data_limit" in first or "proxies" in first)


def _parse_marzban(doc) -> list:
    rows = doc.get("users") if isinstance(doc, dict) else doc
    out = []
    for i, u in enumerate(rows):
        if not isinstance(u, dict):
            continue
        proxies = u.get("proxies") or {}
        uuid_hint = ""
        for proto in ("vless", "vmess", "trojan"):
            entry = proxies.get(proto)
            if isinstance(entry, dict) and entry.get("id"):
                uuid_hint = entry["id"]
                break
        # "active" and "on_hold" are usable; limited/expired/disabled are not.
        status = str(u.get("status") or "active").lower()
        out.append(_row(
            name=_clean_name(u.get("username"), f"imported-{i + 1}"),
            uuid_hint=uuid_hint,
            quota_bytes=u.get("data_limit"),
            expire_at=_seconds(u.get("expire")),
            used_bytes=u.get("used_traffic"),
            enabled=status in ("active", "on_hold"),
            note=u.get("note"),
        ))
    return out


# ------------------------------------------------------------------ 3x-ui
def _clients_of(doc):
    """3x-ui hands out clients in several nestings depending on how it was
    exported; settings arrives as a JSON string more often than not."""
    if isinstance(doc, list):
        return doc
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("clients"), list):
        return doc["clients"]

    settings = doc.get("settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except (TypeError, ValueError):
            settings = None
    if isinstance(settings, dict) and isinstance(settings.get("clients"), list):
        return settings["clients"]

    # A whole export: {"obj": [ {inbound}, ... ]} or a bare list of inbounds.
    for key in ("obj", "inbounds"):
        rows = doc.get(key)
        if isinstance(rows, list):
            merged = []
            for row in rows:
                found = _clients_of(row)
                if found:
                    merged.extend(found)
            if merged:
                return merged
    return None


def _is_xui(doc) -> bool:
    clients = _clients_of(doc)
    if not clients:
        return False
    first = clients[0]
    return isinstance(first, dict) and (
        "email" in first or "totalGB" in first or "expiryTime" in first)


def _parse_xui(doc) -> list:
    out = []
    for i, c in enumerate(_clients_of(doc) or []):
        if not isinstance(c, dict):
            continue
        out.append(_row(
            name=_clean_name(c.get("email") or c.get("remark"), f"imported-{i + 1}"),
            uuid_hint=c.get("id") or c.get("password"),
            quota_bytes=c.get("totalGB"),      # named GB, stored in bytes
            expire_at=_seconds(c.get("expiryTime")),
            used_bytes=(_as_int(c.get("up")) + _as_int(c.get("down"))),
            enabled=c.get("enable", True) is not False,
            note=c.get("comment") or c.get("tgId"),
            max_connections=c.get("limitIp"),
        ))
    return out


# ------------------------------------------------------------------ peyk
def _is_peyk(doc) -> bool:
    return isinstance(doc, dict) and isinstance(doc.get("inbounds"), list) and (
        "schema_version" in doc or "settings" in doc
        or all(isinstance(r, dict) and "uid" in r for r in doc["inbounds"][:1]))


def _parse_peyk(doc) -> list:
    out = []
    for i, ib in enumerate(doc.get("inbounds") or []):
        if not isinstance(ib, dict):
            continue
        quota_gb = ib.get("quota_gb") or 0
        out.append(_row(
            name=_clean_name(ib.get("name"), f"imported-{i + 1}"),
            uuid_hint=ib.get("uuid"),
            quota_bytes=int(float(quota_gb) * GB) if quota_gb else 0,
            expire_at=ib.get("expire_at"),
            used_bytes=_as_int(ib.get("used_up")) + _as_int(ib.get("used_down")),
            enabled=ib.get("enabled", True),
            note=ib.get("note"),
            max_connections=ib.get("max_connections"),
        ))
    return out


# ------------------------------------------------------------------ entry
DETECTORS = (
    ("peyk", _is_peyk, _parse_peyk),
    ("marzban", _is_marzban, _parse_marzban),
    ("3x-ui", _is_xui, _parse_xui),
)


def detect_and_parse(doc) -> tuple:
    """(source_name, rows). Raises ImportError_ when nothing matches."""
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except (TypeError, ValueError):
            raise ImportError_("invalid-json")
    if not isinstance(doc, (dict, list)):
        raise ImportError_("invalid-json")

    for name, matches, parse in DETECTORS:
        if matches(doc):
            rows = parse(doc)
            if rows:
                if len(rows) > MAX_IMPORT:
                    raise ImportError_("too-many-users")
                return name, rows
    raise ImportError_("unknown-format")


def plan_import(rows: list, existing_names, now: Optional[float] = None) -> dict:
    """Decide the fate of each parsed row without applying anything.

    Expired and used-up accounts are still imported, just disabled: a seller
    migrating wants their customer list intact, including the lapsed ones they
    intend to chase.
    """
    now = now or time.time()
    taken = set(existing_names)
    fresh, skipped = [], []
    for row in rows:
        if row["name"] in taken:
            skipped.append({"name": row["name"], "reason": "duplicate-name"})
            continue
        taken.add(row["name"])

        row = dict(row)
        expired = bool(row["expire_at"]) and row["expire_at"] <= now
        exhausted = bool(row["quota_gb"]) and \
            (row["used_down"] + row["used_up"]) >= row["quota_gb"] * GB
        if expired or exhausted:
            row["enabled"] = False
            row["lapsed"] = "expired" if expired else "quota"
        fresh.append(row)

    return {
        "total": len(rows),
        "importable": len(fresh),
        "skipped": skipped,
        "rows": fresh,
        "lapsed": sum(1 for r in fresh if r.get("lapsed")),
    }
