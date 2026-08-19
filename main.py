#!/usr/bin/env python3
"""
StanNG — a single-service VLESS-over-WebSocket panel, wizarding-academy themed.

Version 1.6.0
  * live quota/expiry enforcement (limits used to apply only at connect time)
  * SSRF-guarded relay, working UDP, idle-timeout on dead sessions
  * batched disk writes, non-blocking stats, cached edge lookup
  * standard Subscription-Userinfo header for v2rayNG/Clash/Nekobox
  * plans + bulk provisioning, per-user traffic history
  * TOTP two-factor auth, Telegram alerts, configurable branding
"""
import asyncio
import io
import os
import re
import secrets
import time
import uuid as uuid_lib
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

import httpx
import psutil
import qrcode
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import notify
import totp
from colo_map import describe_colo
from storage import (
    MAX_LOGIN_LOG, normalize_db, prune_login_attempts, store,
    hash_password_async, verify_password_async,
)
from vless_engine import relay

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "1.6.0"


def _env_str(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---- branding: env vars set the defaults, panel settings can override them ----
DEFAULT_PANEL_NAME = _env_str("STANNG_PANEL_NAME", "StanNG")
DEFAULT_TELEGRAM_CONTACT = _env_str("STANNG_TELEGRAM_CONTACT", "https://t.me/rvivl")

SESSION_COOKIE = _env_str("STANNG_SESSION_COOKIE", "stanng_session")
SESSION_MAX_AGE = _env_int("STANNG_SESSION_MAX_AGE", 60 * 60 * 24 * 7)
LOGIN_MAX_ATTEMPTS = _env_int("STANNG_LOGIN_MAX_ATTEMPTS", 6)
LOGIN_LOCK_SECONDS = _env_int("STANNG_LOGIN_LOCK_SECONDS", 5 * 60)

# How many reverse proxies sit in front of us. X-Forwarded-For is appended to by
# each hop, so the trustworthy entry is the Nth from the right — reading the
# leftmost value (as this used to) lets any client forge its own IP and either
# dodge the login lockout or lock somebody else out.
TRUSTED_PROXY_HOPS = max(0, _env_int("STANNG_PROXY_HOPS", 1))

FLUSH_INTERVAL = 5          # seconds: fold pending traffic into the in-memory db
DISK_FLUSH_INTERVAL = 15    # seconds: persist the db if anything changed
ENFORCE_INTERVAL = 10       # seconds: kick sessions that ran past their limits
COLO_CACHE_TTL = 3600       # seconds: the edge datacentre does not move often
NOTIFY_INTERVAL = 300       # seconds: scan for quota/expiry alerts
MAX_BULK_CREATE = 200       # users creatable in one bulk request

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

runtime = {
    # uid -> {conn_id: {"ip": str, "since": float, "ws": WebSocket}}
    "active": {},
    "pending_traffic": {},
    "pending_requests": {},
    "colo": {"value": None, "at": 0.0},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Prime the CPU sampler so the first interval-free reading is meaningful.
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
    tasks = [
        asyncio.create_task(_periodic_flush()),
        asyncio.create_task(_disk_flush_loop()),
        asyncio.create_task(_enforcement_loop()),
        asyncio.create_task(_keep_alive_loop()),
        asyncio.create_task(_notify_loop()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await _fold_pending_traffic()
            await store.flush(force=True)
        except Exception:
            pass


app = FastAPI(title=DEFAULT_PANEL_NAME, version=APP_VERSION, lifespan=lifespan)


class CachedStaticFiles(StaticFiles):
    """Static assets with cache headers.

    Templates append ?v=APP_VERSION to css/js, so a long max-age is safe: an
    update changes the URL. Previously every dashboard load re-fetched ~1MB of
    fonts because nothing set Cache-Control at all.
    """

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
        return resp


app.mount("/static", CachedStaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
    # 'unsafe-inline' for styles only: the templates carry a lot of style="…"
    # attributes. All scripts live in /static, so script-src stays strict.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; media-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    ),
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if request.url.path.startswith(("/api/", "/sub/", "/stats", "/health")):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ------------------------------------------------------------------ helpers
def get_serializer(db) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(db["secret_key"], salt="stanng-session")


def _forwarded_for(headers) -> Optional[str]:
    fwd = headers.get("x-forwarded-for")
    if not fwd:
        return None
    parts = [p.strip() for p in fwd.split(",") if p.strip()]
    if not parts:
        return None
    if TRUSTED_PROXY_HOPS <= 0:
        return None
    # Nth from the right = the address our nearest trusted proxy observed.
    idx = len(parts) - TRUSTED_PROXY_HOPS
    return parts[max(0, idx)]


def _client_ip(request: Request) -> str:
    return _forwarded_for(request.headers) or (request.client.host if request.client else "unknown")


def _ws_client_ip(websocket: WebSocket) -> str:
    return _forwarded_for(websocket.headers) or (
        websocket.client.host if websocket.client else "unknown"
    )


def brand(db) -> dict:
    s = db.get("settings") or {}
    return {
        "panel_name": (s.get("panel_name") or "").strip() or DEFAULT_PANEL_NAME,
        "telegram_contact": (s.get("telegram_contact") or "").strip() or DEFAULT_TELEGRAM_CONTACT,
    }


def _session_fingerprint(db) -> str:
    """Value baked into the cookie; changing it invalidates every session."""
    admin = db.get("admin") or {}
    return f"{admin.get('password_hash', '')[:12]}:{db.get('sessions_epoch', 0)}"


async def current_username(request: Request) -> Optional[str]:
    db = await store.get()
    if not db.get("admin"):
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = get_serializer(db).loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    admin = db["admin"]
    if data.get("u") != admin.get("username"):
        return None
    if data.get("v") != _session_fingerprint(db):
        return None
    return admin["username"]


async def require_auth(request: Request) -> str:
    user = await current_username(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def get_scheme(request: Request) -> str:
    """The externally visible scheme, honouring the proxy's forwarded header."""
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        return forwarded_proto.split(",")[0].strip() or "https"
    if request.url.scheme in ("http", "https"):
        return request.url.scheme
    return "https"


def set_session_cookie(response: Response, request: Request, db, username: str):
    token = get_serializer(db).dumps({"u": username, "v": _session_fingerprint(db)})
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE, httponly=True,
        # Behind Railway/Render the ASGI scheme stays http unless uvicorn is told
        # to trust the proxy, so keying Secure off request.url.scheme silently
        # shipped the session cookie without the flag on every real deployment.
        samesite="lax", secure=(get_scheme(request) == "https"),
        path="/",
    )


def gen_uid() -> str:
    return secrets.token_hex(8)


def gen_uuid() -> str:
    return str(uuid_lib.uuid4())


def _split_host_port(raw: str):
    """Split an authority into (hostname, port|None), handling IPv6 literals."""
    raw = (raw or "").split(",")[0].strip()
    raw = re.sub(r"^https?://", "", raw).strip("/").split("/")[0]
    if not raw:
        return "", None
    if raw.startswith("["):                       # [::1]:8000
        host, _, rest = raw.partition("]")
        host = host.lstrip("[")
        port = rest.lstrip(":") or None
    elif raw.count(":") == 1:                     # host:port
        host, _, port = raw.partition(":")
        port = port or None
    else:                                         # bare IPv6 or plain hostname
        host, port = raw, None
    return host, (port if (port or "").isdigit() else None)


def _authority(request: Request, db):
    """The host and port the outside world reaches us on."""
    override = ((db.get("settings") or {}).get("public_domain") or "").strip()
    if override:
        return _split_host_port(override)
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host")         or request.url.hostname or ""
    return _split_host_port(raw)


def public_host(request: Request, db) -> str:
    """Hostname only — this goes into the VLESS link, where a port would break it."""
    return _authority(request, db)[0]


def public_origin(request: Request, db) -> str:
    """scheme://host[:port] for subscription and status URLs.

    The port has to survive here or local/self-hosted installs hand out
    subscription links that point at the wrong place.
    """
    host, port = _authority(request, db)
    scheme = get_scheme(request)
    if port and port not in ("80", "443"):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def inbound_by_uid(db, uid: str):
    for ib in db["inbounds"]:
        if ib.get("uid") == uid:
            return ib
    return None


def _pending_for(uid: str) -> int:
    d = runtime["pending_traffic"].get(uid)
    return (d["up"] + d["down"]) if d else 0


def inbound_status(ib) -> dict:
    now = time.time()
    quota_bytes = int((ib.get("quota_gb") or 0) * 1024 ** 3)
    # Include traffic not yet folded into the db, otherwise a fast transfer can
    # overshoot the quota by everything that arrived since the last flush.
    used = (ib.get("used_up") or 0) + (ib.get("used_down") or 0) + _pending_for(ib["uid"])
    quota_exceeded = quota_bytes > 0 and used >= quota_bytes
    expire_at = ib.get("expire_at")
    expired = bool(expire_at) and now >= expire_at
    req_count = (ib.get("request_count") or 0) + runtime["pending_requests"].get(ib["uid"], 0)
    max_requests = ib.get("max_requests") or 0
    req_exceeded = max_requests > 0 and req_count >= max_requests
    manually_enabled = ib.get("enabled", True)
    return {
        "quota_bytes": quota_bytes,
        "used": used,
        "quota_exceeded": quota_exceeded,
        "expired": expired,
        "disabled": not manually_enabled,
        "request_exceeded": req_exceeded,
        "live_enabled": bool(manually_enabled and not quota_exceeded
                             and not expired and not req_exceeded),
        "active_connections": len(runtime["active"].get(ib["uid"], {})),
        "request_count": req_count,
        "days_left": max(0, int((expire_at - now) // 86400)) if expire_at else None,
    }


def _relay_options(db) -> dict:
    s = db.get("settings") or {}
    try:
        idle = float(s.get("idle_timeout_seconds", 600) or 0)
    except (TypeError, ValueError):
        idle = 600.0
    return {
        "allow_private": bool(s.get("allow_private_destinations", False)),
        "idle_timeout": max(0.0, idle),
    }


# ------------------------------------------------------------------ background tasks
async def _fold_pending_traffic():
    """Move buffered byte/request counts into the in-memory db (no disk write)."""
    traffic = runtime["pending_traffic"]
    requests_ = runtime["pending_requests"]
    if not traffic and not requests_:
        return
    runtime["pending_traffic"] = {}
    runtime["pending_requests"] = {}

    day = time.strftime("%Y-%m-%d", time.gmtime())

    def _apply(db):
        retention = _history_days(db)
        total_up = total_down = 0
        for uid, delta in traffic.items():
            ib = inbound_by_uid(db, uid)
            if ib:
                up, down = delta.get("up", 0), delta.get("down", 0)
                ib["used_up"] = ib.get("used_up", 0) + up
                ib["used_down"] = ib.get("used_down", 0) + down
                # Per-user history is bucketed by UTC day: fine enough to draw
                # a usage trend, coarse enough that db.json stays small even
                # with thousands of users.
                hist = ib.setdefault("history", [])
                if hist and hist[-1].get("d") == day:
                    hist[-1]["up"] += up
                    hist[-1]["down"] += down
                else:
                    hist.append({"d": day, "up": up, "down": down})
                if len(hist) > retention:
                    del hist[:-retention]
            total_up += delta.get("up", 0)
            total_down += delta.get("down", 0)
        for uid, count in requests_.items():
            ib = inbound_by_uid(db, uid)
            if ib:
                ib["request_count"] = ib.get("request_count", 0) + count
        stats = db.setdefault("stats", {})
        stats["total_up"] = stats.get("total_up", 0) + total_up
        stats["total_down"] = stats.get("total_down", 0) + total_down
        if total_up or total_down:
            hourly = stats.setdefault("hourly", [])
            bucket = int(time.time() // 3600) * 3600
            if hourly and hourly[-1].get("t") == bucket:
                hourly[-1]["up"] += total_up
                hourly[-1]["down"] += total_down
            else:
                hourly.append({"t": bucket, "up": total_up, "down": total_down})
            if len(hourly) > 72:
                del hourly[:-72]

    # persist=False: the disk flush loop batches these together. This used to
    # rewrite the whole db.json every 5 seconds, plus once per connection.
    await store.mutate(_apply, persist=False)


async def _periodic_flush():
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL)
            await _fold_pending_traffic()
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(2)


async def _disk_flush_loop():
    while True:
        try:
            await asyncio.sleep(DISK_FLUSH_INTERVAL)
            await store.flush()
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


async def _disconnect_uid(uid: str, code: int = 1008):
    """Force every live session of a user to close."""
    for conn in list(runtime["active"].get(uid, {}).values()):
        ws = conn.get("ws")
        if ws is None:
            continue
        try:
            await ws.close(code=code)
        except Exception:
            pass


async def _enforce_once() -> list:
    """One enforcement sweep. Returns the uids that were cut off."""
    if not runtime["active"]:
        return []
    db = await store.get()
    kicked = []
    for uid in list(runtime["active"].keys()):
        if not runtime["active"].get(uid):
            runtime["active"].pop(uid, None)
            continue
        ib = inbound_by_uid(db, uid)
        if ib is None or not inbound_status(ib)["live_enabled"]:
            await _disconnect_uid(uid)
            kicked.append(uid)
    return kicked


async def _enforcement_loop():
    """Cut off sessions that have run past their quota, expiry or request cap.

    The old build only checked limits during the WebSocket handshake, so one
    long-lived connection could transfer unlimited data on a 1 GB plan.
    """
    while True:
        try:
            await asyncio.sleep(ENFORCE_INTERVAL)
            await _enforce_once()
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


def _history_days(db) -> int:
    try:
        return max(1, min(90, int((db.get("settings") or {}).get("history_days", 30))))
    except (TypeError, ValueError):
        return 30


def _notify_config(db):
    s = db.get("settings") or {}
    token = (s.get("telegram_bot_token") or "").strip()
    chat = (s.get("telegram_chat_id") or "").strip()
    return (token, chat, s) if (token and chat) else (None, None, s)


async def _scan_alerts(db) -> list:
    """Decide which alerts are due. Returns [(uid, kind, message)].

    Pure decision-making: it sends nothing and mutates nothing, which keeps
    it straightforward to test without a Telegram token.
    """
    token, chat, settings = _notify_config(db)
    if not token:
        return []
    sent = db.setdefault("alerts_sent", {})
    panel = brand(db)["panel_name"]
    try:
        threshold = max(1, min(100, int(settings.get("notify_quota_percent", 80))))
    except (TypeError, ValueError):
        threshold = 80
    try:
        expiry_days = max(0, min(60, int(settings.get("notify_expiry_days", 3))))
    except (TypeError, ValueError):
        expiry_days = 3

    due = []
    for ib in db["inbounds"]:
        st = inbound_status(ib)
        uid = ib["uid"]
        if settings.get("notify_quota_enabled", True) and st["quota_bytes"] > 0:
            percent = st["used"] / st["quota_bytes"] * 100
            if percent >= threshold and notify.should_alert(sent, uid, "quota"):
                due.append((uid, "quota", notify.format_quota_alert(
                    panel, ib["name"], st["used"], st["quota_bytes"], percent)))
        if settings.get("notify_expiry_enabled", True) and st["days_left"] is not None:
            if st["days_left"] <= expiry_days and not st["expired"]                     and notify.should_alert(sent, uid, "expiry"):
                due.append((uid, "expiry", notify.format_expiry_alert(
                    panel, ib["name"], st["days_left"])))
    return due


async def _notify_loop():
    while True:
        try:
            await asyncio.sleep(NOTIFY_INTERVAL)
            db = await store.get()
            token, chat, _ = _notify_config(db)
            if not token:
                continue
            due = await _scan_alerts(db)
            delivered = []
            for uid, kind, message in due:
                try:
                    await notify.send_message(token, chat, message)
                    delivered.append((uid, kind))
                except notify.TelegramError:
                    # A bad token or chat id would otherwise make this loop
                    # retry the same failing sends every cycle forever.
                    break
            if delivered:
                def _record(db):
                    sent = db.setdefault("alerts_sent", {})
                    for uid, kind in delivered:
                        notify.record_alert(sent, uid, kind)
                    notify.prune_alerts(sent)
                await store.mutate(_record, persist=False)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(30)


async def _keep_alive_loop():
    """Ping our own public URL so free-tier hosts don't idle the service out.

    Hitting 127.0.0.1 (as before) never reached the platform's router and so
    never counted as activity; and the settings snapshot was read once before
    the sleep, meaning toggling keep_alive off took a full cycle to apply.
    """
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.sleep(600)
            db = await store.get()
            settings = db.get("settings") or {}
            if not settings.get("keep_alive", True):
                continue
            domain = (settings.get("public_domain") or "").strip()
            if domain:
                url = domain if domain.startswith("http") else f"https://{domain}"
                target = f"{url.rstrip('/')}/health"
            else:
                target = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/health"
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                await client.get(target)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(15)


# ------------------------------------------------------------------ page routes
def _page_context(db, extra: Optional[dict] = None) -> dict:
    ctx = {"app_version": APP_VERSION}
    ctx.update(brand(db))
    if extra:
        ctx.update(extra)
    return ctx


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    db = await store.get()
    if not db.get("admin"):
        return RedirectResponse("/setup")
    if await current_username(request):
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = await store.get()
    if db.get("admin"):
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "setup.html", _page_context(db))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    db = await store.get()
    if not db.get("admin"):
        return RedirectResponse("/setup")
    if await current_username(request):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "login.html", _page_context(db))


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    db = await store.get()
    if not db.get("admin"):
        return RedirectResponse("/setup")
    if not await current_username(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "dashboard.html", _page_context(db))


@app.get("/status/{uid}", response_class=HTMLResponse)
async def status_page(request: Request, uid: str):
    db = await store.get()
    if not inbound_by_uid(db, uid):
        return HTMLResponse("<h1>404</h1><p>Not found.</p>", status_code=404)
    return templates.TemplateResponse(request, "status.html", _page_context(db, {"uid": uid}))


# ------------------------------------------------------------------ auth api
@app.get("/api/setup-status")
async def setup_status():
    db = await store.get()
    return {"needs_setup": not bool(db.get("admin"))}


USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")
MIN_PASSWORD_LEN = 8


@app.post("/api/setup")
async def api_setup(request: Request):
    payload = await _json_body(request)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    db = await store.get()
    if db.get("admin"):
        raise HTTPException(400, "already-configured")
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "invalid-username")
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, "weak-password")

    hp = await hash_password_async(password)

    def _apply(db):
        db["admin"] = {
            "username": username,
            "password_hash": hp["hash"],
            "salt": hp["salt"],
            "created_at": time.time(),
        }

    db = await store.mutate(_apply)
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, db, username)
    return resp


@app.post("/api/login")
async def api_login(request: Request):
    payload = await _json_body(request)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ip = _client_ip(request)
    db = await store.get()

    attempts = (db.get("login_attempts") or {}).get(ip, {})
    locked_until = attempts.get("locked_until", 0)
    if locked_until > time.time():
        raise HTTPException(429, f"locked:{int(locked_until - time.time())}")

    admin = db.get("admin")
    if admin:
        # Always run the KDF, even for an unknown username, so response time
        # doesn't reveal whether the account exists.
        password_ok = await verify_password_async(password, admin["salt"], admin["password_hash"])
        ok = secrets.compare_digest(admin["username"], username) and password_ok
    else:
        await verify_password_async(password, "dummy-salt", "0" * 64)
        ok = False

    twofa = db.get("twofa") or {}
    needs_2fa = ok and bool(twofa.get("enabled")) and bool(twofa.get("secret"))
    method = "password"
    used_recovery = None

    if needs_2fa:
        code = str(payload.get("code") or "").strip()
        if not code:
            # Password was right but the second factor is still owed. This is
            # not a failed attempt, so it must not count toward the lockout.
            return JSONResponse({"ok": False, "twofa_required": True}, status_code=200)
        if totp.verify_code(twofa["secret"], code):
            method = "totp"
        else:
            used_recovery = totp.consume_recovery_code(code, twofa.get("recovery_hashes") or [])
            if used_recovery:
                method = "recovery"
            else:
                ok = False
                method = "totp-failed"

    def _record(db):
        la = db.setdefault("login_attempts", {})
        if ok:
            la.pop(ip, None)
            if used_recovery:
                # Recovery codes are single use.
                hashes = db["twofa"].get("recovery_hashes") or []
                db["twofa"]["recovery_hashes"] = [h for h in hashes if h != used_recovery]
        else:
            rec = la.setdefault(ip, {"count": 0, "locked_until": 0, "seen": 0})
            rec["count"] = rec.get("count", 0) + 1
            rec["seen"] = time.time()
            if rec["count"] >= LOGIN_MAX_ATTEMPTS:
                rec["locked_until"] = time.time() + LOGIN_LOCK_SECONDS
                rec["count"] = 0
        prune_login_attempts(db)
        _append_login_log(db, ip, ok, method)

    db = await store.mutate(_record)

    if not ok:
        raise HTTPException(401, "invalid-credentials")

    resp = JSONResponse({"ok": True, "recovery_remaining": len(
        (db.get("twofa") or {}).get("recovery_hashes") or [])})
    set_session_cookie(resp, request, db, username)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.post("/api/logout-all")
async def api_logout_all(user: str = Depends(require_auth)):
    """Invalidate every session cookie ever issued, on every device."""
    def _apply(db):
        db["sessions_epoch"] = db.get("sessions_epoch", 0) + 1

    await store.mutate(_apply)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    user = await current_username(request)
    db = await store.get()
    settings = dict(db.get("settings", {}))
    return {
        "logged_in": bool(user),
        "username": user,
        "settings": settings if user else {},
        "app_version": APP_VERSION,
        **brand(db),
    }


@app.post("/api/change-password")
async def api_change_password(request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    old_password = payload.get("old_password") or ""
    new_password = payload.get("new_password") or ""
    new_username = (payload.get("new_username") or "").strip()
    db = await store.get()
    admin = db["admin"]
    if not await verify_password_async(old_password, admin["salt"], admin["password_hash"]):
        raise HTTPException(401, "wrong-old-password")
    if new_username and not USERNAME_RE.match(new_username):
        raise HTTPException(400, "invalid-username")
    if new_password and len(new_password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, "weak-password")
    if not new_password and not new_username:
        raise HTTPException(400, "nothing-to-change")

    hp = await hash_password_async(new_password) if new_password else None

    def _apply(db):
        if hp:
            db["admin"]["password_hash"] = hp["hash"]
            db["admin"]["salt"] = hp["salt"]
        if new_username:
            db["admin"]["username"] = new_username

    db = await store.mutate(_apply)
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, request, db, db["admin"]["username"])
    return resp


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "invalid-json")
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid-json")
    return payload


# ------------------------------------------------------------------ settings
VALID_FINGERPRINTS = {"chrome", "ios", "firefox", "edge", "random", "safari", "android"}
VALID_ALPN = {"http/1.1", "h2,http/1.1", "h3,h2,http/1.1"}
BOOL_SETTINGS = {
    "keep_alive", "fragment_enabled", "allow_private_destinations",
    "notify_quota_enabled", "notify_expiry_enabled", "notify_daily_report",
}
TEXT_SETTINGS = {
    "public_domain": 200, "ota_repo": 140, "sni_override": 253,
    "fragment_packets": 32, "fragment_length": 32, "fragment_interval": 32,
    "panel_name": 40, "telegram_contact": 200,
    "telegram_bot_token": 100, "telegram_chat_id": 40,
}
# Numeric settings: name -> (min, max)
INT_SETTINGS = {
    "idle_timeout_seconds": (0, 86400),
    "notify_quota_percent": (1, 100),
    "notify_expiry_days": (0, 60),
    "history_days": (1, 90),
}
OTA_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")


@app.post("/api/settings")
async def api_update_settings(request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    clean = {}

    for k, maxlen in TEXT_SETTINGS.items():
        if k in payload:
            v = str(payload[k] or "").strip()[:maxlen]
            if k == "ota_repo" and v:
                v = re.sub(r"^https?://github\.com/", "", v).strip("/")
                if not OTA_REPO_RE.match(v):
                    raise HTTPException(400, "invalid-ota-repo")
            if k == "telegram_contact" and v and not v.startswith(("http://", "https://")):
                v = f"https://t.me/{v.lstrip('@')}"
            if k == "telegram_bot_token" and v and not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", v):
                raise HTTPException(400, "invalid-bot-token")
            clean[k] = v

    for k in BOOL_SETTINGS:
        if k in payload:
            clean[k] = bool(payload[k])

    if "lang" in payload and payload["lang"] in ("fa", "en"):
        clean["lang"] = payload["lang"]
    if "theme" in payload and payload["theme"] in ("dark", "light"):
        clean["theme"] = payload["theme"]
    if "default_fingerprint" in payload and payload["default_fingerprint"] in VALID_FINGERPRINTS:
        clean["default_fingerprint"] = payload["default_fingerprint"]
    if "default_alpn" in payload and payload["default_alpn"] in VALID_ALPN:
        clean["default_alpn"] = payload["default_alpn"]
    for k, (lo, hi) in INT_SETTINGS.items():
        if k in payload:
            try:
                clean[k] = max(lo, min(hi, int(payload[k])))
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid-{k}")

    def _apply(db):
        db.setdefault("settings", {}).update(clean)

    db = await store.mutate(_apply)
    return {"ok": True, "settings": db["settings"], **brand(db)}


# ------------------------------------------------------------------ login log
def _append_login_log(db, ip: str, ok: bool, method: str):
    log = db.setdefault("login_log", [])
    log.append({"ts": time.time(), "ip": ip, "ok": bool(ok), "method": method})
    if len(log) > MAX_LOGIN_LOG:
        del log[:-MAX_LOGIN_LOG]


@app.get("/api/login-log")
async def api_login_log(user: str = Depends(require_auth)):
    db = await store.get()
    return {"entries": list(reversed(db.get("login_log", [])))[:100]}


# ------------------------------------------------------------------ two-factor auth
@app.get("/api/2fa/status")
async def api_2fa_status(user: str = Depends(require_auth)):
    db = await store.get()
    twofa = db.get("twofa") or {}
    return {
        "enabled": bool(twofa.get("enabled")),
        "confirmed_at": twofa.get("confirmed_at"),
        "recovery_remaining": len(twofa.get("recovery_hashes") or []),
    }


@app.post("/api/2fa/setup")
async def api_2fa_setup(request: Request, user: str = Depends(require_auth)):
    """Stage a secret. It stays inactive until a valid code confirms it, so a
    half-finished enrolment can never lock the admin out."""
    db = await store.get()
    if (db.get("twofa") or {}).get("enabled"):
        raise HTTPException(400, "already-enabled")
    secret = totp.generate_secret()

    def _apply(db):
        db["twofa"]["secret"] = secret
        db["twofa"]["enabled"] = False
        db["twofa"]["confirmed_at"] = None

    await store.mutate(_apply)
    return {
        "secret": secret,
        "uri": totp.provisioning_uri(secret, user, brand(db)["panel_name"]),
    }


@app.get("/api/2fa/qr")
async def api_2fa_qr(user: str = Depends(require_auth)):
    db = await store.get()
    secret = (db.get("twofa") or {}).get("secret")
    if not secret:
        raise HTTPException(404, "no-pending-secret")
    uri = totp.provisioning_uri(secret, user, brand(db)["panel_name"])

    def _render() -> bytes:
        img = qrcode.make(uri, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    png = await asyncio.to_thread(_render)
    return StreamingResponse(io.BytesIO(png), media_type="image/png",
                             headers={"Cache-Control": "no-store"})


@app.post("/api/2fa/enable")
async def api_2fa_enable(request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    code = str(payload.get("code") or "").strip()
    db = await store.get()
    twofa = db.get("twofa") or {}
    secret = twofa.get("secret")
    if not secret:
        raise HTTPException(400, "no-pending-secret")
    if twofa.get("enabled"):
        raise HTTPException(400, "already-enabled")
    if not totp.verify_code(secret, code):
        raise HTTPException(400, "invalid-code")

    codes = totp.generate_recovery_codes()
    hashes = [totp.hash_recovery_code(c) for c in codes]

    def _apply(db):
        db["twofa"]["enabled"] = True
        db["twofa"]["confirmed_at"] = time.time()
        db["twofa"]["recovery_hashes"] = hashes

    await store.mutate(_apply)
    # The only time the plaintext codes are ever returned.
    return {"ok": True, "recovery_codes": codes}


@app.post("/api/2fa/disable")
async def api_2fa_disable(request: Request, user: str = Depends(require_auth)):
    """Requires the account password: a stolen session alone must not be able
    to strip the second factor."""
    payload = await _json_body(request)
    password = payload.get("password") or ""
    db = await store.get()
    admin = db["admin"]
    if not await verify_password_async(password, admin["salt"], admin["password_hash"]):
        raise HTTPException(401, "wrong-password")

    def _apply(db):
        db["twofa"] = {"enabled": False, "secret": "", "recovery_hashes": [], "confirmed_at": None}

    await store.mutate(_apply)
    return {"ok": True}


@app.post("/api/2fa/recovery-codes")
async def api_2fa_regen_recovery(request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    password = payload.get("password") or ""
    db = await store.get()
    admin = db["admin"]
    if not await verify_password_async(password, admin["salt"], admin["password_hash"]):
        raise HTTPException(401, "wrong-password")
    if not (db.get("twofa") or {}).get("enabled"):
        raise HTTPException(400, "not-enabled")

    codes = totp.generate_recovery_codes()
    hashes = [totp.hash_recovery_code(c) for c in codes]

    def _apply(db):
        db["twofa"]["recovery_hashes"] = hashes

    await store.mutate(_apply)
    return {"ok": True, "recovery_codes": codes}


# ------------------------------------------------------------------ notifications
@app.post("/api/notify/test")
async def api_notify_test(user: str = Depends(require_auth)):
    db = await store.get()
    token, chat, _ = _notify_config(db)
    if not token:
        raise HTTPException(400, "not-configured")
    try:
        await notify.verify_credentials(token, chat)
    except notify.TelegramError as e:
        raise HTTPException(502, f"telegram: {e}")
    return {"ok": True}


@app.get("/api/notify/status")
async def api_notify_status(user: str = Depends(require_auth)):
    db = await store.get()
    token, chat, settings = _notify_config(db)
    return {
        "configured": bool(token),
        "quota_enabled": bool(settings.get("notify_quota_enabled", True)),
        "expiry_enabled": bool(settings.get("notify_expiry_enabled", True)),
        "pending_alerts": len(await _scan_alerts(db)) if token else 0,
    }


# ------------------------------------------------------------------ inbounds api
MAX_INBOUNDS = _env_int("STANNG_MAX_INBOUNDS", 5000)


def _as_number(value, field: str, minimum: float, maximum: float, integer: bool):
    """Coerce and range-check one numeric field, or 400.

    PATCH used to copy the request body straight into the record, so
    {"quota_gb": "abc"} was stored happily and then crashed every subsequent
    listing with a TypeError on the quota multiplication.
    """
    if value is None or value == "":
        value = 0
    if isinstance(value, bool):
        raise HTTPException(400, f"invalid-{field}")
    try:
        num = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"invalid-{field}")
    if num != num or num in (float("inf"), float("-inf")):  # NaN / inf
        raise HTTPException(400, f"invalid-{field}")
    if not (minimum <= num <= maximum):
        raise HTTPException(400, f"invalid-{field}")
    return num


def sanitize_inbound_payload(payload: dict, partial: bool) -> dict:
    """Validate and coerce a create/update body into storable fields."""
    out = {}
    if "name" in payload or not partial:
        name = str(payload.get("name") or "").strip()[:64] or "User"
        out["name"] = name
    if "note" in payload or not partial:
        out["note"] = str(payload.get("note") or "").strip()[:200]
    if "quota_gb" in payload or not partial:
        out["quota_gb"] = _as_number(payload.get("quota_gb"), "quota_gb", 0, 1_000_000, False)
    if "expire_days" in payload or not partial:
        out["expire_days"] = _as_number(payload.get("expire_days"), "expire_days", 0, 3650, True)
    if "max_connections" in payload or not partial:
        out["max_connections"] = _as_number(payload.get("max_connections"), "max_connections", 0, 10_000, True)
    if "max_requests" in payload or not partial:
        out["max_requests"] = _as_number(payload.get("max_requests"), "max_requests", 0, 100_000_000, True)
    if "fp" in payload or not partial:
        fp = payload.get("fp") or "chrome"
        if fp not in VALID_FINGERPRINTS:
            raise HTTPException(400, "invalid-fp")
        out["fp"] = fp
    if "strict_single_ip" in payload or not partial:
        out["strict_single_ip"] = bool(payload.get("strict_single_ip"))
    if "enabled" in payload:
        out["enabled"] = bool(payload["enabled"])
    return out


def serialize_inbound(ib, include_secrets: bool = True) -> dict:
    st = inbound_status(ib)
    # "history" is up to 90 daily buckets per user; shipping it inside every
    # listing would dominate the payload. It has its own endpoint.
    out = {k: v for k, v in ib.items() if k not in ("uuid", "history")}
    if include_secrets:
        out["uuid"] = ib.get("uuid")
    conns = runtime["active"].get(ib["uid"], {})
    # Previously hardcoded to None, so the panel could never show who was online.
    out["active_ips"] = sorted({c["ip"] for c in conns.values()})
    out["status"] = st
    return out


@app.get("/api/inbounds")
async def api_list_inbounds(user: str = Depends(require_auth)):
    db = await store.get()
    return {"inbounds": [serialize_inbound(ib) for ib in db["inbounds"]]}


@app.post("/api/inbounds")
async def api_create_inbound(request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    db = await store.get()
    if len(db["inbounds"]) >= MAX_INBOUNDS:
        raise HTTPException(400, "inbound-limit-reached")

    fields = sanitize_inbound_payload(payload, partial=False)
    if not payload.get("fp"):
        fields["fp"] = (db.get("settings") or {}).get("default_fingerprint", "chrome")

    now = time.time()
    ib = {
        "uid": gen_uid(),
        "uuid": gen_uuid(),
        "enabled": True,
        "created_at": now,
        "expire_at": (now + fields["expire_days"] * 86400) if fields["expire_days"] > 0 else None,
        "request_count": 0,
        "used_up": 0,
        "used_down": 0,
    }
    ib.update(fields)

    def _apply(db):
        db["inbounds"].append(ib)

    await store.mutate(_apply)
    return {"ok": True, "inbound": serialize_inbound(ib)}


@app.patch("/api/inbounds/{uid}")
async def api_update_inbound(uid: str, request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    fields = sanitize_inbound_payload(payload, partial=True)
    result = {}

    def _apply(db):
        ib = inbound_by_uid(db, uid)
        if not ib:
            raise HTTPException(404, "not-found")
        # Only restart the validity window when the admin actually changed the
        # number. Recomputing from created_at (the old behaviour) made renewing
        # a long-standing user expire them on the spot, while recomputing on
        # every save would silently extend anyone whose row was merely edited.
        if "expire_days" in fields and fields["expire_days"] != (ib.get("expire_days") or 0):
            days = fields["expire_days"]
            ib["expire_at"] = (time.time() + days * 86400) if days > 0 else None
        ib.update(fields)
        result.update(ib)

    await store.mutate(_apply)
    if not inbound_status(result)["live_enabled"]:
        await _disconnect_uid(uid)
    return {"ok": True, "inbound": serialize_inbound(result)}


@app.post("/api/inbounds/{uid}/renew")
async def api_renew_inbound(uid: str, request: Request, user: str = Depends(require_auth)):
    """Extend a subscription by N days from whichever is later: now or its current expiry."""
    payload = await _json_body(request)
    days = _as_number(payload.get("days"), "days", 1, 3650, True)
    reset_usage = bool(payload.get("reset_usage"))
    result = {}

    def _apply(db):
        ib = inbound_by_uid(db, uid)
        if not ib:
            raise HTTPException(404, "not-found")
        base = max(time.time(), ib.get("expire_at") or 0)
        ib["expire_at"] = base + days * 86400
        ib["expire_days"] = int((ib["expire_at"] - ib.get("created_at", time.time())) // 86400)
        if reset_usage:
            ib["used_up"] = 0
            ib["used_down"] = 0
            ib["request_count"] = 0
            runtime["pending_traffic"].pop(uid, None)
            runtime["pending_requests"].pop(uid, None)
        result.update(ib)

    await store.mutate(_apply)
    return {"ok": True, "inbound": serialize_inbound(result)}


@app.delete("/api/inbounds/{uid}")
async def api_delete_inbound(uid: str, user: str = Depends(require_auth)):
    found = {"v": False}

    def _apply(db):
        before = len(db["inbounds"])
        db["inbounds"] = [ib for ib in db["inbounds"] if ib.get("uid") != uid]
        found["v"] = len(db["inbounds"]) != before

    await store.mutate(_apply)
    if not found["v"]:
        raise HTTPException(404, "not-found")
    await _disconnect_uid(uid)
    runtime["active"].pop(uid, None)
    runtime["pending_traffic"].pop(uid, None)
    runtime["pending_requests"].pop(uid, None)
    return {"ok": True}


@app.post("/api/inbounds/{uid}/reset-usage")
async def api_reset_usage(uid: str, user: str = Depends(require_auth)):
    def _apply(db):
        ib = inbound_by_uid(db, uid)
        if not ib:
            raise HTTPException(404, "not-found")
        ib["used_up"] = 0
        ib["used_down"] = 0
        ib["request_count"] = 0

    runtime["pending_traffic"].pop(uid, None)
    runtime["pending_requests"].pop(uid, None)
    db = await store.mutate(_apply)
    return {"ok": True, "inbound": serialize_inbound(inbound_by_uid(db, uid))}


@app.post("/api/inbounds/{uid}/regenerate")
async def api_regenerate_uuid(uid: str, user: str = Depends(require_auth)):
    """Anti-resale: instantly revoke old links by rotating the VLESS uuid."""
    def _apply(db):
        ib = inbound_by_uid(db, uid)
        if not ib:
            raise HTTPException(404, "not-found")
        ib["uuid"] = gen_uuid()

    db = await store.mutate(_apply)
    await _disconnect_uid(uid)
    runtime["active"].pop(uid, None)
    return {"ok": True, "inbound": serialize_inbound(inbound_by_uid(db, uid))}


# ------------------------------------------------------------------ plans
MAX_PLANS = 50


def sanitize_plan(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()[:48]
    if not name:
        raise HTTPException(400, "invalid-name")
    return {
        "name": name,
        "days": _as_number(payload.get("days"), "days", 0, 3650, True),
        "quota_gb": _as_number(payload.get("quota_gb"), "quota_gb", 0, 1_000_000, False),
        "max_connections": _as_number(payload.get("max_connections"), "max_connections", 0, 10_000, True),
        "max_requests": _as_number(payload.get("max_requests"), "max_requests", 0, 100_000_000, True),
    }


@app.get("/api/plans")
async def api_list_plans(user: str = Depends(require_auth)):
    db = await store.get()
    return {"plans": db.get("plans", [])}


@app.post("/api/plans")
async def api_create_plan(request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    fields = sanitize_plan(payload)
    db = await store.get()
    if len(db.get("plans", [])) >= MAX_PLANS:
        raise HTTPException(400, "plan-limit-reached")
    plan = {"id": gen_uid()}
    plan.update(fields)

    def _apply(db):
        db.setdefault("plans", []).append(plan)

    await store.mutate(_apply)
    return {"ok": True, "plan": plan}


@app.patch("/api/plans/{plan_id}")
async def api_update_plan(plan_id: str, request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    fields = sanitize_plan(payload)
    result = {}

    def _apply(db):
        for plan in db.setdefault("plans", []):
            if plan.get("id") == plan_id:
                plan.update(fields)
                result.update(plan)
                return
        raise HTTPException(404, "not-found")

    await store.mutate(_apply)
    return {"ok": True, "plan": result}


@app.delete("/api/plans/{plan_id}")
async def api_delete_plan(plan_id: str, user: str = Depends(require_auth)):
    found = {"v": False}

    def _apply(db):
        plans = db.setdefault("plans", [])
        before = len(plans)
        db["plans"] = [pl for pl in plans if pl.get("id") != plan_id]
        found["v"] = len(db["plans"]) != before

    await store.mutate(_apply)
    if not found["v"]:
        raise HTTPException(404, "not-found")
    return {"ok": True}


# ------------------------------------------------------------------ bulk operations
@app.post("/api/inbounds/bulk")
async def api_bulk_create(request: Request, user: str = Depends(require_auth)):
    """Create N users from a plan (or explicit fields) in one write.

    Provisioning a batch one HTTP call at a time meant one full db.json
    rewrite per user; this is a single mutation for the whole batch.
    """
    payload = await _json_body(request)
    count = _as_number(payload.get("count"), "count", 1, MAX_BULK_CREATE, True)
    prefix = str(payload.get("prefix") or "user").strip()[:40] or "user"
    start = _as_number(payload.get("start_index") or 1, "start_index", 1, 1_000_000, True)

    db = await store.get()
    plan_id = payload.get("plan_id")
    if plan_id:
        plan = next((pl for pl in db.get("plans", []) if pl.get("id") == plan_id), None)
        if not plan:
            raise HTTPException(404, "plan-not-found")
        base = {
            "quota_gb": plan.get("quota_gb", 0),
            "expire_days": plan.get("days", 0),
            "max_connections": plan.get("max_connections", 0),
            "max_requests": plan.get("max_requests", 0),
        }
    else:
        base = {
            "quota_gb": _as_number(payload.get("quota_gb"), "quota_gb", 0, 1_000_000, False),
            "expire_days": _as_number(payload.get("expire_days"), "expire_days", 0, 3650, True),
            "max_connections": _as_number(payload.get("max_connections"), "max_connections", 0, 10_000, True),
            "max_requests": _as_number(payload.get("max_requests"), "max_requests", 0, 100_000_000, True),
        }

    if len(db["inbounds"]) + count > MAX_INBOUNDS:
        raise HTTPException(400, "inbound-limit-reached")

    settings = db.get("settings") or {}
    fp = payload.get("fp") or settings.get("default_fingerprint", "chrome")
    if fp not in VALID_FINGERPRINTS:
        raise HTTPException(400, "invalid-fp")
    strict = bool(payload.get("strict_single_ip"))
    note = str(payload.get("note") or "").strip()[:200]

    now = time.time()
    created = []
    width = len(str(start + count - 1))
    for i in range(count):
        ib = {
            "uid": gen_uid(),
            "uuid": gen_uuid(),
            "name": f"{prefix}-{str(start + i).zfill(width)}"[:64],
            "enabled": True,
            "created_at": now,
            "expire_at": (now + base["expire_days"] * 86400) if base["expire_days"] > 0 else None,
            "request_count": 0,
            "used_up": 0,
            "used_down": 0,
            "history": [],
            "fp": fp,
            "strict_single_ip": strict,
            "note": note,
        }
        ib.update(base)
        created.append(ib)

    def _apply(db):
        db["inbounds"].extend(created)

    await store.mutate(_apply)
    return {"ok": True, "created": len(created),
            "inbounds": [serialize_inbound(ib) for ib in created]}


BULK_ACTIONS = {"delete", "enable", "disable", "reset-usage", "renew"}


@app.post("/api/inbounds/bulk-action")
async def api_bulk_action(request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    action = payload.get("action")
    if action not in BULK_ACTIONS:
        raise HTTPException(400, "invalid-action")
    uids = payload.get("uids")
    if not isinstance(uids, list) or not uids:
        raise HTTPException(400, "no-uids")
    uids = [str(u) for u in uids][:MAX_INBOUNDS]
    days = _as_number(payload.get("days") or 30, "days", 1, 3650, True) if action == "renew" else 0
    affected = {"n": 0}

    def _apply(db):
        target = set(uids)
        if action == "delete":
            before = len(db["inbounds"])
            db["inbounds"] = [ib for ib in db["inbounds"] if ib.get("uid") not in target]
            affected["n"] = before - len(db["inbounds"])
            return
        for ib in db["inbounds"]:
            if ib.get("uid") not in target:
                continue
            if action == "enable":
                ib["enabled"] = True
            elif action == "disable":
                ib["enabled"] = False
            elif action == "reset-usage":
                ib["used_up"] = 0
                ib["used_down"] = 0
                ib["request_count"] = 0
            elif action == "renew":
                base = max(time.time(), ib.get("expire_at") or 0)
                ib["expire_at"] = base + days * 86400
            affected["n"] += 1

    await store.mutate(_apply)

    # Anything that can revoke access must also drop live sessions, or the
    # user keeps proxying until their connection happens to end.
    if action in ("delete", "disable"):
        for uid in uids:
            await _disconnect_uid(uid)
            if action == "delete":
                runtime["active"].pop(uid, None)
                runtime["pending_traffic"].pop(uid, None)
                runtime["pending_requests"].pop(uid, None)
    elif action == "reset-usage":
        for uid in uids:
            runtime["pending_traffic"].pop(uid, None)
            runtime["pending_requests"].pop(uid, None)

    if not affected["n"]:
        raise HTTPException(404, "not-found")
    return {"ok": True, "action": action, "affected": affected["n"]}


# ------------------------------------------------------------------ per-user history
def _history_series(db, ib) -> list:
    """Dense daily series - gaps filled with zeros so the chart has no holes."""
    days = _history_days(db)
    buckets = {h.get("d"): h for h in ib.get("history", []) if isinstance(h, dict)}
    now = time.time()
    out = []
    for i in range(days - 1, -1, -1):
        key = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
        rec = buckets.get(key) or {}
        out.append({"d": key, "up": rec.get("up", 0), "down": rec.get("down", 0)})
    return out


@app.get("/api/inbounds/{uid}/history")
async def api_inbound_history(uid: str, user: str = Depends(require_auth)):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    return {"uid": uid, "name": ib["name"], "history": _history_series(db, ib)}


# ------------------------------------------------------------------ link building
def build_links(request: Request, db, ib) -> dict:
    """Build the VLESS-over-WS TLS link for an inbound.

    path and alpn keep '/' and ',' unescaped: percent-encoding them is what
    made v2rayNG reject these links before 1.4.1.
    """
    settings = db.get("settings") or {}
    host = public_host(request, db)
    fp = ib.get("fp") or settings.get("default_fingerprint", "chrome")
    alpn = settings.get("default_alpn", "http/1.1")
    sni = (settings.get("sni_override") or "").strip() or host
    path = f"/ws/{ib['uid']}"

    params = [
        "encryption=none", "security=tls", "type=ws",
        f"host={quote(host)}",
        f"path={quote(path, safe='/')}",
        f"sni={quote(sni)}",
        f"fp={quote(fp)}",
        f"alpn={quote(alpn, safe=',/')}",
    ]
    # Fragment settings were stored and shown in the UI but never reached the
    # link, so turning fragmentation on in the panel did nothing at all.
    if settings.get("fragment_enabled"):
        packets = (settings.get("fragment_packets") or "tlshello").strip()
        length = (settings.get("fragment_length") or "10-30").strip()
        interval = (settings.get("fragment_interval") or "10-20").strip()
        params.append(f"fragment={quote(f'{packets},{length},{interval}')}")

    remark = f"{brand(db)['panel_name']}-{ib['name']}"
    link_tls = f"vless://{ib['uuid']}@{host}:443?{'&'.join(params)}#{quote(remark)}"
    return {"tls": link_tls, "remark": remark}


def subscription_userinfo(ib) -> str:
    """The header every modern client reads to show quota and expiry natively.

    Replaces the old trick of injecting fake 127.0.0.1 configs into the
    subscription purely to render text in the client's server list.
    """
    st = inbound_status(ib)
    pending = runtime["pending_traffic"].get(ib["uid"]) or {}
    upload = (ib.get("used_up") or 0) + pending.get("up", 0)
    download = (ib.get("used_down") or 0) + pending.get("down", 0)
    total = st["quota_bytes"]  # 0 means unlimited, which clients understand
    expire = int(ib["expire_at"]) if ib.get("expire_at") else 0
    return f"upload={upload}; download={download}; total={total}; expire={expire}"


@app.get("/api/inbounds/{uid}/links")
async def api_inbound_links(uid: str, request: Request, user: str = Depends(require_auth)):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    origin = public_origin(request, db)
    return {
        "links": build_links(request, db, ib),
        "sub_url": f"{origin}/sub/{uid}",
        "sub_json_url": f"{origin}/sub/{uid}/json",
        "status_url": f"{origin}/status/{uid}",
    }


@app.get("/api/inbounds/{uid}/qr")
async def api_inbound_qr(uid: str, request: Request, user: str = Depends(require_auth)):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    target = build_links(request, db, ib)["tls"]

    def _render() -> bytes:
        img = qrcode.make(target, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    png = await asyncio.to_thread(_render)  # PIL encoding is CPU-bound
    return StreamingResponse(io.BytesIO(png), media_type="image/png",
                             headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ subscriptions
@app.get("/sub/{uid}")
async def sub_plain(uid: str, request: Request):
    """Plain-text VLESS links, as v2rayNG and friends expect."""
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    links = build_links(request, db, ib)
    headers = {
        "Subscription-Userinfo": subscription_userinfo(ib),
        "Profile-Update-Interval": "12",
        "Profile-Title": links["remark"],
        "Content-Disposition": f'inline; filename="{uid}.txt"',
        "Cache-Control": "no-store",
    }
    return PlainTextResponse(links["tls"], headers=headers)


@app.get("/sub/{uid}/json")
async def sub_json(uid: str, request: Request):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    st = inbound_status(ib)
    links = build_links(request, db, ib)
    return JSONResponse({
        "name": ib["name"],
        "uid": uid,
        "enabled": st["live_enabled"],
        "quota_gb": ib.get("quota_gb"),
        "used_gb": round(st["used"] / (1024 ** 3), 3),
        "days_left": st["days_left"],
        "max_connections": ib.get("max_connections"),
        "active_connections": st["active_connections"],
        "links": {"tls": links["tls"]},
    }, headers={"Subscription-Userinfo": subscription_userinfo(ib), "Cache-Control": "no-store"})


@app.get("/api/inbounds/{uid}/sub")
async def api_inbound_sub_alias(uid: str, request: Request, user: str = Depends(require_auth)):
    return await sub_json(uid, request)


# ------------------------------------------------------------------ public status api
@app.get("/api/status/{uid}")
async def api_public_status(uid: str):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib:
        raise HTTPException(404, "not-found")
    st = inbound_status(ib)
    return {
        "name": ib["name"],
        "enabled": st["live_enabled"],
        "reason": ("expired" if st["expired"] else
                   "quota" if st["quota_exceeded"] else
                   "requests" if st["request_exceeded"] else
                   "disabled" if st["disabled"] else None),
        "quota_gb": ib.get("quota_gb"),
        "used_gb": round(st["used"] / (1024 ** 3), 4),
        "used_bytes": st["used"],
        "quota_bytes": st["quota_bytes"],
        "days_left": st["days_left"],
        "expire_at": ib.get("expire_at"),
        "max_connections": ib.get("max_connections"),
        "active_connections": st["active_connections"],
        "max_requests": ib.get("max_requests"),
        "request_count": st["request_count"],
    }


# ------------------------------------------------------------------ backup / restore
@app.get("/api/backup")
async def api_backup(user: str = Depends(require_auth)):
    """Download the full db as JSON. Contains the admin hash and session key."""
    db = await store.get()
    payload = store.snapshot_json()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = re.sub(r"[^A-Za-z0-9_.-]", "", brand(db)["panel_name"]) or "stanng"
    return Response(
        payload, media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{name}-backup-{stamp}.json"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/restore")
async def api_restore(request: Request, user: str = Depends(require_auth)):
    payload = await _json_body(request)
    incoming = payload.get("db") if isinstance(payload.get("db"), dict) else payload
    if not isinstance(incoming.get("inbounds"), list) or not isinstance(incoming.get("admin"), dict):
        raise HTTPException(400, "invalid-backup")
    if not incoming.get("secret_key"):
        raise HTTPException(400, "invalid-backup")

    for uid in list(runtime["active"].keys()):
        await _disconnect_uid(uid)
    runtime["active"].clear()
    runtime["pending_traffic"].clear()
    runtime["pending_requests"].clear()

    normalize_db(incoming)
    await store.replace(incoming)
    resp = JSONResponse({"ok": True, "inbounds": len(incoming["inbounds"])})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ------------------------------------------------------------------ system / stats
@app.get("/health")
async def health():
    return {"status": "ok", "ts": time.time()}


async def _current_colo() -> str:
    """Cloudflare edge code, cached — this used to be an outbound HTTP request
    on every 8-second dashboard poll."""
    cache = runtime["colo"]
    if cache["value"] is not None and time.time() - cache["at"] < COLO_CACHE_TTL:
        return cache["value"]
    colo = "?"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
            for line in r.text.splitlines():
                if line.startswith("colo="):
                    colo = line.split("=", 1)[1].strip()
                    break
    except Exception:
        colo = cache["value"] or "?"
    cache["value"] = colo
    cache["at"] = time.time()
    return colo


@app.get("/stats")
async def stats(user: str = Depends(require_auth)):
    db = await store.get()
    # interval=None samples since the previous call instead of sleeping 200ms
    # inside the event loop, which stalled every proxied connection per poll.
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    started = (db.get("stats") or {}).get("started_at", time.time())

    pending_up = sum(d.get("up", 0) for d in runtime["pending_traffic"].values())
    pending_down = sum(d.get("down", 0) for d in runtime["pending_traffic"].values())

    return {
        "cpu_percent": cpu,
        "mem_percent": mem.percent,
        "mem_used_mb": round(mem.used / 1024 / 1024, 1),
        "mem_total_mb": round(mem.total / 1024 / 1024, 1),
        "uptime_seconds": time.time() - started,
        "total_up": db["stats"].get("total_up", 0) + pending_up,
        "total_down": db["stats"].get("total_down", 0) + pending_down,
        "hourly": db["stats"].get("hourly", []),
        "inbounds_count": len(db["inbounds"]),
        "active_connections": sum(len(v) for v in runtime["active"].values()),
        "location": describe_colo(await _current_colo()),
    }


def _ver_tuple(v):
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) if parts else (0,)


async def _resolve_latest_release(repo: str, current: str, client: httpx.AsyncClient):
    """Returns (latest_version, html_url, download_zip_url) via the GitHub API."""
    latest, url, zip_url = current, f"https://github.com/{repo}/releases", None
    r = await client.get(f"https://api.github.com/repos/{repo}/releases/latest")
    if r.status_code == 200:
        data = r.json()
        tag = (data.get("tag_name") or "").lstrip("v")
        if tag:
            latest = tag
            url = data.get("html_url", url)
            zip_url = data.get("zipball_url")
    else:
        r2 = await client.get(f"https://api.github.com/repos/{repo}/tags")
        if r2.status_code == 200 and r2.json():
            tag_info = r2.json()[0]
            latest = (tag_info.get("name") or current).lstrip("v")
            url = f"https://github.com/{repo}/releases/tag/{tag_info.get('name')}"
            zip_url = tag_info.get("zipball_url")
    return latest, url, zip_url


def _ota_repo(db) -> str:
    repo = ((db.get("settings") or {}).get("ota_repo") or "").strip()
    if not repo or not OTA_REPO_RE.match(repo):
        raise HTTPException(400, "no-repo-configured")
    return repo


@app.get("/api/ota/check")
async def api_ota_check(user: str = Depends(require_auth)):
    db = await store.get()
    repo = _ota_repo(db)
    current = APP_VERSION
    try:
        async with httpx.AsyncClient(timeout=8, headers={"Accept": "application/vnd.github+json"}) as client:
            latest, url, _zip = await _resolve_latest_release(repo, current, client)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "github-unreachable")

    return {
        "current": current, "latest": latest,
        "update_available": _ver_tuple(latest) > _ver_tuple(current),
        "url": url,
    }


# ------------------------------------------------------------------ OTA self-update
UPDATE_LOCK = asyncio.Lock()
NEVER_TOUCH = {"data", ".git", ".env"}
MAX_UPDATE_BYTES = 64 * 1024 * 1024


def _safe_extract_zip(zip_path: str, dest_dir: str):
    import zipfile
    dest_root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError("empty archive")
        total = sum(i.file_size for i in zf.infolist())
        if total > MAX_UPDATE_BYTES:
            raise RuntimeError("archive too large")
        root_prefix = names[0].split("/")[0] + "/"
        for member in names:
            if not member.startswith(root_prefix):
                continue
            rel = member[len(root_prefix):]
            if not rel:
                continue
            target = os.path.realpath(os.path.join(dest_root, rel))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise RuntimeError(f"unsafe path in archive: {member}")
            if member.endswith("/"):
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(1 << 16)
                        if not chunk:
                            break
                        dst.write(chunk)


def _apply_staged_update(staged_dir: str, live_dir: str) -> list:
    import shutil
    touched = []
    for entry in os.listdir(staged_dir):
        if entry in NEVER_TOUCH:
            continue
        src = os.path.join(staged_dir, entry)
        dst = os.path.join(live_dir, entry)
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        touched.append(entry)
    return touched


@app.post("/api/ota/update")
async def api_ota_update(request: Request, user: str = Depends(require_auth)):
    if UPDATE_LOCK.locked():
        raise HTTPException(409, "update-already-in-progress")

    async with UPDATE_LOCK:
        db = await store.get()
        repo = _ota_repo(db)

        import shutil as _shutil
        import tempfile

        current = APP_VERSION
        tmp_root = None
        try:
            async with httpx.AsyncClient(
                timeout=60, headers={"Accept": "application/vnd.github+json"}, follow_redirects=True
            ) as client:
                latest, _html_url, zip_url = await _resolve_latest_release(repo, current, client)
                if _ver_tuple(latest) <= _ver_tuple(current):
                    return {"ok": False, "reason": "already-up-to-date",
                            "current": current, "latest": latest}
                if not zip_url:
                    raise HTTPException(502, "no-downloadable-archive-found")

                tmp_root = tempfile.mkdtemp(prefix="stanng_ota_")
                zip_path = os.path.join(tmp_root, "release.zip")
                staged_dir = os.path.join(tmp_root, "staged")
                os.makedirs(staged_dir, exist_ok=True)

                written = 0
                async with client.stream("GET", zip_url) as resp:
                    if resp.status_code != 200:
                        raise HTTPException(502, f"download-failed-{resp.status_code}")
                    with open(zip_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            written += len(chunk)
                            if written > MAX_UPDATE_BYTES:
                                raise HTTPException(502, "archive-too-large")
                            f.write(chunk)

            await asyncio.to_thread(_safe_extract_zip, zip_path, staged_dir)

            if not os.path.exists(os.path.join(staged_dir, "main.py")):
                raise HTTPException(502, "downloaded-archive-missing-main.py")

            staged_data = os.path.join(staged_dir, "data")
            if os.path.isdir(staged_data):
                _shutil.rmtree(staged_data, ignore_errors=True)

            # Persist everything before the files under us are swapped out.
            await store.flush(force=True)
            touched = await asyncio.to_thread(_apply_staged_update, staged_dir, BASE_DIR)

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"update-failed: {e}")
        finally:
            if tmp_root:
                _shutil.rmtree(tmp_root, ignore_errors=True)

        async def _delayed_restart():
            await asyncio.sleep(1.5)
            os._exit(87)

        asyncio.create_task(_delayed_restart())
        return {
            "ok": True, "previous_version": current, "new_version": latest,
            "files_updated": touched, "restarting": True,
        }


# ------------------------------------------------------------------ VLESS websocket endpoint
def _negotiated_subprotocol(websocket: WebSocket) -> Optional[str]:
    """Echo back a single protocol token.

    Reflecting the raw header meant a client offering "a, b" got the whole
    string back as one protocol name, which is not a valid negotiation.
    """
    raw = websocket.headers.get("sec-websocket-protocol")
    if not raw:
        return None
    first = raw.split(",")[0].strip()
    return first or None


@app.websocket("/ws/{uid}")
async def ws_endpoint(websocket: WebSocket, uid: str):
    db = await store.get()
    ib = inbound_by_uid(db, uid)
    if not ib or not inbound_status(ib)["live_enabled"]:
        await websocket.close(code=1008)
        return

    ip = _ws_client_ip(websocket)
    active_for_uid = runtime["active"].setdefault(uid, {})
    max_conn = ib.get("max_connections") or 0

    if ib.get("strict_single_ip"):
        existing_ips = {v["ip"] for v in active_for_uid.values()}
        if existing_ips and ip not in existing_ips:
            await websocket.close(code=1008)
            return
    if max_conn > 0 and len(active_for_uid) >= max_conn:
        await websocket.close(code=1008)
        return

    await websocket.accept(subprotocol=_negotiated_subprotocol(websocket))

    conn_id = secrets.token_hex(6)
    active_for_uid[conn_id] = {"ip": ip, "since": time.time(), "ws": websocket}
    # Buffered, not written through: this used to rewrite the whole db.json
    # once per connection.
    runtime["pending_requests"][uid] = runtime["pending_requests"].get(uid, 0) + 1

    def on_traffic(du, dd):
        bucket = runtime["pending_traffic"].setdefault(uid, {"up": 0, "down": 0})
        bucket["up"] += du
        bucket["down"] += dd

    opts = _relay_options(db)
    try:
        await relay(websocket, ib["uuid"], on_traffic,
                    idle_timeout=opts["idle_timeout"], allow_private=opts["allow_private"])
    except Exception:
        pass
    finally:
        active_for_uid.pop(conn_id, None)
        if not active_for_uid:
            runtime["active"].pop(uid, None)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8000),
        log_level=os.environ.get("LOG_LEVEL", "info"),
        # Let uvicorn parse the platform's forwarded headers; we still pick the
        # trustworthy X-Forwarded-For entry ourselves via TRUSTED_PROXY_HOPS.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
