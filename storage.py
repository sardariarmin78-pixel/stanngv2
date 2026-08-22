"""
Peyk - Persistent JSON storage layer.
Single-file, dependency-free storage engine (no external DB required).
Async-safe via an in-process lock + atomic file writes.

Writes are debounced: hot-path mutations (traffic accounting, request counters)
mark the store dirty and a background flush persists them, while anything the
admin does is written through immediately. The file itself is serialised on the
event loop but written from a worker thread, so a large db.json never stalls
the proxy.
"""
import asyncio
import glob
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, Optional

from cluster import FileLock

def resolve_data_dir(env=None) -> str:
    """PEYK_DATA_DIR, falling back to the pre-2.0 STANNG_DATA_DIR.

    The fallback is the difference between an update and an outage: a
    deployment with only STANNG_DATA_DIR set would otherwise come up on an
    empty database and look like every user had vanished.
    """
    env = os.environ if env is None else env
    return (env.get("PEYK_DATA_DIR")
            or env.get("STANNG_DATA_DIR")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))


DATA_DIR = resolve_data_dir()
DB_PATH = os.path.join(DATA_DIR, "db.json")
LOCK_PATH = os.path.join(DATA_DIR, "db.lock")
RUNTIME_DIR = os.path.join(DATA_DIR, "rt")

SCHEMA_VERSION = 11  # v11: rotatable subscription tokens, health checks
PBKDF2_ITERATIONS = 260_000

# Drop lockout records this old — the table used to grow forever, one entry per
# distinct client IP that ever failed a login, and every entry was rewritten to
# disk on each save.
LOGIN_ATTEMPT_TTL = 24 * 3600

# Hard ceilings for the collections that append over time. db.json is read
# and rewritten whole, so unbounded growth costs memory and write latency.
MAX_LOGIN_LOG = 200
MAX_HISTORY_DAYS = 90

_lock = asyncio.Lock()

DEFAULT_DB: Dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "admin": None,          # {"username": str, "password_hash": str, "salt": str, "created_at": ts}
    "secret_key": None,     # generated on first run, used to sign session cookies
    "sessions_epoch": 0,    # bumped to invalidate every issued session cookie
    "twofa": {              # TOTP enrolment; "enabled" gates the login check
        "enabled": False,
        "secret": "",
        "recovery_hashes": [],
        "confirmed_at": None,
    },
    "settings": {
        "lang": "fa",
        "theme": "dark",
        "public_domain": "",         # optional override; else derived from request Host header
        "keep_alive": True,
        "ota_repo": "",
        # NOTE: app_version is intentionally NOT stored here. It must always
        # reflect the code actually running on disk (main.py's APP_VERSION),
        # never a stale value frozen into db.json from an earlier install —
        # that mismatch used to make the dashboard show the wrong "Current"
        # version after every update.
        # ---- branding (env vars provide the defaults; panel can override) ----
        "panel_name": "",
        "telegram_contact": "",
        # ---- advanced config defaults (applied to newly generated VLESS links) ----
        "default_fingerprint": "chrome",     # chrome | ios | firefox | edge | random
        "default_alpn": "http/1.1",          # http/1.1 | h2,http/1.1 | h3,h2,http/1.1
        "sni_override": "",                  # optional domain-fronting SNI; blank = use host
        "fragment_enabled": True,
        "fragment_packets": "tlshello",
        "fragment_length": "10-30",
        "fragment_interval": "10-20",
        # ---- relay safety ----
        # Off by default: a proxied client reaching 127.0.0.1 or the cloud
        # metadata endpoint (169.254.169.254) can pivot into the host itself.
        "allow_private_destinations": False,
        "idle_timeout_seconds": 600,
        # ---- notifications (admin's own bot; blank token disables everything) ----
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "notify_quota_enabled": True,
        "notify_quota_percent": 80,
        "notify_expiry_enabled": True,
        "notify_expiry_days": 3,
        "notify_daily_report": False,
        # ---- per-user traffic history ----
        "history_days": 30,
        # ---- off-box backup (Railway wipes the disk on every redeploy) ----
        "auto_backup_enabled": False,
        "auto_backup_hours": 6,
        # ---- self-service bot for end users ----
        "userbot_enabled": False,
        "userbot_token": "",
        # Users may ask for a renewal from the bot; the admin approves with a
        # button. Off by default because it messages the admin.
        "userbot_renew_enabled": False,
        "userbot_renew_options": [30, 60, 90],
        # ---- endpoint health monitoring ----
        "health_check_enabled": False,
        "health_interval_minutes": 15,
        "health_fail_threshold": 3,   # consecutive failures before alerting
        "health_auto_disable": False, # drop a dead endpoint out of subscriptions
        # ---- retention: expired accounts otherwise pile up forever ----
        "cleanup_enabled": False,
        "cleanup_disable_days": 3,   # days past expiry before disabling
        "cleanup_delete_days": 30,   # days past expiry before deleting (0 = never)
        # ---- one-click trial accounts ----
        "trial_enabled": True,
        "trial_gb": 1.0,
        "trial_days": 1,
        "trial_prefix": "trial",
        # ---- DPI fragmentation profile applied to generated links ----
        "fragment_profile": "balanced",
    },
    "inbounds": [],       # list of inbound/user dicts
    "plans": [],          # reusable presets: {id, name, days, quota_gb, max_connections, max_requests}
    # Entry points a client can reach this backend through. Several hostnames
    # or clean CDN IPs in front of one deployment already behave as separate
    # routes, which is what matters against per-IP blocking. `node_url` is
    # reserved for real remote nodes later; empty means "this deployment".
    "endpoints": [],      # {id, name, address, port, sni, host, fp, alpn, enabled, sort, node_url, health}
    "login_log": [],      # bounded audit trail: {ts, ip, ok, method, reason}
    "alerts_sent": {},    # "<uid>:<kind>" -> ts, for notification cooldowns
    "last_backup": None,  # {"ts": float, "ok": bool, "detail": str}
    "bot_bindings": {},   # telegram chat id -> inbound uid
    "bot_offset": 0,      # last consumed getUpdates id
    "last_cleanup": None, # {"ts": float, "disabled": int, "deleted": int}
    "renew_requests": {}, # request id -> {uid, chat, name, created_at, status}
    "stats": {
        "started_at": time.time(),
        "total_up": 0,
        "total_down": 0,
        "hourly": []       # [{"t": ts, "up": n, "down": n}]
    },
    "login_attempts": {}  # ip -> {"count": n, "locked_until": ts, "seen": ts}
}


def _atomic_write(path: str, data: str):
    """Write then rename, so a crash mid-write cannot truncate the live db."""
    tmp_path = f"{path}.tmp-{secrets.token_hex(4)}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Don't leave orphaned .tmp-* files behind on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # Persist the rename itself; without this the new file can survive a power
    # cut while the directory entry still points at the old one.
    try:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, AttributeError):
        pass  # not supported on Windows; os.replace is atomic there regardless


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _cleanup_temp_files():
    for stale in glob.glob(f"{DB_PATH}.tmp-*"):
        try:
            os.unlink(stale)
        except OSError:
            pass


def prune_login_attempts(db: Dict[str, Any]) -> bool:
    """Drop expired lockout records. Returns True if anything was removed."""
    la = db.get("login_attempts")
    if not isinstance(la, dict) or not la:
        return False
    now = time.time()
    stale = [
        ip for ip, rec in la.items()
        if not isinstance(rec, dict)
        or (rec.get("locked_until", 0) < now and now - rec.get("seen", 0) > LOGIN_ATTEMPT_TTL)
    ]
    for ip in stale:
        la.pop(ip, None)
    return bool(stale)


def _fresh_db() -> Dict[str, Any]:
    db = json.loads(json.dumps(DEFAULT_DB))
    db["secret_key"] = secrets.token_hex(32)
    db["stats"]["started_at"] = time.time()
    return db


def normalize_db(db: Dict[str, Any]) -> bool:
    """Fill in missing keys, migrate old shapes, drop stale records.

    Shared by load_db and by backup restore, so a hand-edited or older backup
    can never install a db that is missing the keys the app assumes exist.
    """
    changed = False
    for k, v in DEFAULT_DB.items():
        if k not in db:
            db[k] = json.loads(json.dumps(v))
            changed = True

    if isinstance(db.get("settings"), dict):
        # Migration: before profiles existed the typed fragment values were
        # always used. Defaulting an upgraded install to "balanced" would
        # silently discard whatever the admin had tuned, so it starts on
        # "custom" instead — which keeps using exactly those values.
        if "fragment_profile" not in db["settings"] and any(
                k in db["settings"] for k in
                ("fragment_packets", "fragment_length", "fragment_interval")):
            db["settings"]["fragment_profile"] = "custom"
            changed = True

        for k, v in DEFAULT_DB["settings"].items():
            if k not in db["settings"]:
                db["settings"][k] = v
                changed = True
        # Migration: drop any stale app_version previously persisted by an
        # older build — it must never override the code's real APP_VERSION.
        if "app_version" in db["settings"]:
            del db["settings"]["app_version"]
            changed = True
        # Historical placeholder that 404s against the GitHub API on every
        # update check; treat it as "not configured". The literal is the value
        # older installs actually stored, so it stays spelled that way.
        if db["settings"].get("ota_repo") == "your-username/StanNG":
            db["settings"]["ota_repo"] = ""
            changed = True
    else:
        db["settings"] = json.loads(json.dumps(DEFAULT_DB["settings"]))
        changed = True

    if not db.get("secret_key"):
        db["secret_key"] = secrets.token_hex(32)
        changed = True
    if not isinstance(db.get("inbounds"), list):
        db["inbounds"] = []
        changed = True
    if not isinstance(db.get("stats"), dict):
        db["stats"] = json.loads(json.dumps(DEFAULT_DB["stats"]))
        changed = True
    if not isinstance(db.get("login_attempts"), dict):
        db["login_attempts"] = {}
        changed = True
    if not isinstance(db.get("plans"), list):
        db["plans"] = []
        changed = True
    if not isinstance(db.get("endpoints"), list):
        db["endpoints"] = []
        changed = True
    else:
        kept_eps = []
        for ep in db["endpoints"]:
            if not isinstance(ep, dict) or not ep.get("id") or not ep.get("address"):
                changed = True
                continue
            if not isinstance(ep.get("health"), dict):
                ep["health"] = {"ok": None, "ts": None, "latency_ms": None}
                changed = True
            kept_eps.append(ep)
        if len(kept_eps) != len(db["endpoints"]):
            db["endpoints"] = kept_eps
    if not isinstance(db.get("alerts_sent"), dict):
        db["alerts_sent"] = {}
        changed = True
    if not isinstance(db.get("bot_bindings"), dict):
        db["bot_bindings"] = {}
        changed = True
    if not isinstance(db.get("renew_requests"), dict):
        db["renew_requests"] = {}
        changed = True
    if not isinstance(db.get("bot_offset"), int):
        db["bot_offset"] = 0
        changed = True
    if not isinstance(db.get("twofa"), dict):
        db["twofa"] = json.loads(json.dumps(DEFAULT_DB["twofa"]))
        changed = True
    else:
        for k, v in DEFAULT_DB["twofa"].items():
            if k not in db["twofa"]:
                db["twofa"][k] = v
                changed = True
    # The login log is append-only from the app's side; trim on load so a
    # long-running panel cannot grow db.json without bound.
    if not isinstance(db.get("login_log"), list):
        db["login_log"] = []
        changed = True
    elif len(db["login_log"]) > MAX_LOGIN_LOG:
        del db["login_log"][:-MAX_LOGIN_LOG]
        changed = True

    # Repair inbound records so one bad row cannot break every listing.
    kept = []
    for ib in db["inbounds"]:
        if not isinstance(ib, dict) or not ib.get("uid") or not ib.get("uuid"):
            changed = True
            continue
        for field, default in (("used_up", 0), ("used_down", 0), ("request_count", 0),
                               ("quota_gb", 0), ("max_connections", 0), ("max_requests", 0),
                               ("expire_days", 0)):
            if not isinstance(ib.get(field), (int, float)) or isinstance(ib.get(field), bool):
                ib[field] = default
                changed = True
        if not isinstance(ib.get("name"), str):
            ib["name"] = "User"
            changed = True
        if not isinstance(ib.get("expire_at"), (int, float)) or isinstance(ib.get("expire_at"), bool):
            if ib.get("expire_at") is not None:
                ib["expire_at"] = None
                changed = True
        # The subscription URL used to be keyed by uid, which is also the
        # WebSocket path. Existing installs therefore start with the token
        # equal to the uid so no link breaks; rotating it later kills the old
        # URL without touching the client's working config.
        if not ib.get("sub_token"):
            ib["sub_token"] = ib["uid"]
            changed = True
        # Daily traffic buckets: [{"d": "YYYY-MM-DD", "up": n, "down": n}]
        if not isinstance(ib.get("history"), list):
            ib["history"] = []
            changed = True
        elif len(ib["history"]) > MAX_HISTORY_DAYS:
            del ib["history"][:-MAX_HISTORY_DAYS]
            changed = True
        kept.append(ib)
    if len(kept) != len(db["inbounds"]):
        db["inbounds"] = kept

    # ------------------- v3 migration: remove "addresses" (clean-IP) ----------
    if "addresses" in db:
        del db["addresses"]
        changed = True
    if prune_login_attempts(db):
        changed = True
    if db.get("schema_version", 1) != SCHEMA_VERSION:
        db["schema_version"] = SCHEMA_VERSION
        changed = True
    return changed


def load_db() -> Dict[str, Any]:
    _ensure_dir()
    _cleanup_temp_files()
    if not os.path.exists(DB_PATH):
        db = _fresh_db()
        _atomic_write(DB_PATH, json.dumps(db, ensure_ascii=False, indent=2))
        return db
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, dict):
            raise ValueError("db.json is not an object")
    except (json.JSONDecodeError, OSError, ValueError):
        # Never silently discard a corrupt db — keep it aside so the admin can
        # recover users manually instead of finding an empty panel.
        try:
            os.replace(DB_PATH, f"{DB_PATH}.corrupt-{int(time.time())}")
        except OSError:
            pass
        db = _fresh_db()

    if normalize_db(db):
        _atomic_write(DB_PATH, json.dumps(db, ensure_ascii=False, indent=2))
    return db


class Store:
    """Async-safe accessor around the JSON db.

    In single-process mode this is exactly what it always was: the db lives
    in memory and is written back atomically.

    In multi-worker mode (`enable_multiprocess`) every mutation takes a
    cross-process file lock and re-reads from disk first, so concurrent
    workers cannot clobber each other. Readers notice a sibling's write via
    the file's mtime and reload in place — in place specifically, so any
    reference already handed out by get() stays valid.
    """

    def __init__(self):
        self.db = load_db()
        self._dirty = False
        self._last_write = 0.0
        self._multiprocess = False
        self._mtime = self._current_mtime()

    # ---------------------------------------------------------------- mode
    def enable_multiprocess(self):
        self._multiprocess = True

    @staticmethod
    def _current_mtime() -> float:
        try:
            return os.path.getmtime(DB_PATH)
        except OSError:
            return 0.0

    def _reload_if_stale(self):
        """Pull in another worker's write. No-op in single-process mode."""
        if not self._multiprocess:
            return False
        mtime = self._current_mtime()
        if mtime == self._mtime:
            return False
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                fresh = json.load(f)
            if not isinstance(fresh, dict):
                return False
        except (OSError, json.JSONDecodeError):
            return False
        normalize_db(fresh)
        # Mutate in place: callers hold the same dict object.
        self.db.clear()
        self.db.update(fresh)
        self._mtime = mtime
        return True

    # ---------------------------------------------------------------- access
    async def get(self) -> Dict[str, Any]:
        self._reload_if_stale()
        return self.db

    async def mutate(self, fn, persist: bool = True):
        """Apply fn(db) under the lock.

        persist=False marks the store dirty instead of writing, for hot-path
        updates that a periodic flush can batch. If fn raises, nothing is
        written and the exception propagates to the caller.
        """
        async with _lock:
            if not self._multiprocess:
                fn(self.db)
                if persist:
                    await self._write_locked()
                else:
                    self._dirty = True
                return self.db

            # Multi-worker: the whole read-modify-write must be exclusive, or
            # two workers editing different users would each write a copy that
            # omits the other's change.
            def _guarded():
                with FileLock(LOCK_PATH):
                    self._reload_if_stale()
                    fn(self.db)
                    if persist:
                        data = json.dumps(self.db, ensure_ascii=False, indent=2)
                        _atomic_write(DB_PATH, data)
                        return True
                return False

            wrote = await asyncio.to_thread(_guarded)
            if wrote:
                self._mtime = self._current_mtime()
                self._dirty = False
                self._last_write = time.time()
            else:
                self._dirty = True
            return self.db

    async def flush(self, force: bool = False) -> bool:
        """Persist pending changes. Returns True if a write happened."""
        async with _lock:
            if not (self._dirty or force):
                return False
            if self._multiprocess:
                def _guarded():
                    with FileLock(LOCK_PATH):
                        _atomic_write(DB_PATH, json.dumps(self.db, ensure_ascii=False, indent=2))
                await asyncio.to_thread(_guarded)
                self._mtime = self._current_mtime()
                self._dirty = False
                self._last_write = time.time()
            else:
                await self._write_locked()
            return True

    async def _write_locked(self):
        # Serialise on the loop (fast, and a consistent snapshot because no
        # await happens mid-dump), then push the bytes to disk off-loop.
        data = json.dumps(self.db, ensure_ascii=False, indent=2)
        self._dirty = False
        self._last_write = time.time()
        await asyncio.to_thread(_atomic_write, DB_PATH, data)
        self._mtime = self._current_mtime()

    def snapshot_json(self) -> str:
        return json.dumps(self.db, ensure_ascii=False, indent=2)

    async def replace(self, new_db: Dict[str, Any]):
        """Swap in a restored backup and persist it immediately."""
        async with _lock:
            normalize_db(new_db)
            self.db.clear()
            self.db.update(new_db)
            await self._write_locked()
            return self.db

    def get_sync(self) -> Dict[str, Any]:
        return self.db


store = Store()


# ---------- password hashing (stdlib only, no extra deps) ----------

def hash_password(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return {"hash": dk.hex(), "salt": salt}


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    if not salt or not expected_hash:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return secrets.compare_digest(dk.hex(), expected_hash)


# 260k PBKDF2 rounds take ~100-200ms. Run them in a worker thread: on the event
# loop, a handful of login attempts would stall every proxied connection.

async def hash_password_async(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    return await asyncio.to_thread(hash_password, password, salt)


async def verify_password_async(password: str, salt: str, expected_hash: str) -> bool:
    return await asyncio.to_thread(verify_password, password, salt, expected_hash)
