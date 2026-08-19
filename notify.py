"""
Telegram notifications for the panel admin.

Alerts go to the admin's own bot and chat, configured in the panel — this
never messages end users and never talks to any third party the admin did
not set up. If no token is configured the whole module is inert.

Every alert type is rate-limited per user per day, because the condition
that triggers it (over 80% of quota, 3 days from expiry) stays true for as
long as it stays true, and an unthrottled check would fire on every sweep.
"""
import time
from typing import Optional

import httpx

TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT = 10

# One alert of a given kind per user per this window.
ALERT_COOLDOWN = 24 * 3600
# Remember at most this many (uid, kind) records, so the table cannot grow
# without bound on a panel with heavy churn.
MAX_ALERT_RECORDS = 5000


class TelegramError(Exception):
    pass


def _escape(text: str) -> str:
    """Escape for Telegram's HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def send_message(token: str, chat_id: str, text: str,
                       timeout: float = SEND_TIMEOUT) -> dict:
    """Post one message. Raises TelegramError with the API's own reason."""
    if not token or not chat_id:
        raise TelegramError("not-configured")
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
    except httpx.HTTPError as e:
        raise TelegramError(f"unreachable: {e}") from e

    try:
        data = r.json()
    except ValueError:
        raise TelegramError(f"bad-response-{r.status_code}")

    if not data.get("ok"):
        # Surface Telegram's description verbatim: "chat not found" and
        # "Unauthorized" are the two the admin will actually hit, and a
        # generic failure message would leave them guessing which.
        raise TelegramError(data.get("description") or f"error-{r.status_code}")
    return data


def _fmt_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def format_quota_alert(panel: str, name: str, used: int, quota: int, percent: float) -> str:
    return (
        f"⚠️ <b>{_escape(panel)}</b>\n\n"
        f"کاربر <b>{_escape(name)}</b> به {percent:.0f}% حجم خود رسید.\n"
        f"مصرف: <code>{_fmt_bytes(used)}</code> از <code>{_fmt_bytes(quota)}</code>"
    )


def format_expiry_alert(panel: str, name: str, days_left: int) -> str:
    when = "امروز" if days_left <= 0 else f"{days_left} روز دیگر"
    return (
        f"⏳ <b>{_escape(panel)}</b>\n\n"
        f"اشتراک <b>{_escape(name)}</b> {when} منقضی می‌شود."
    )


def format_daily_report(panel: str, stats: dict) -> str:
    return (
        f"📊 <b>{_escape(panel)}</b> — گزارش روزانه\n\n"
        f"کاربران: <b>{stats.get('total', 0)}</b>"
        f" (فعال: {stats.get('active', 0)}، غیرفعال: {stats.get('inactive', 0)})\n"
        f"اتصالات فعال: <b>{stats.get('connections', 0)}</b>\n"
        f"ترافیک امروز: <code>{_fmt_bytes(stats.get('today_bytes', 0))}</code>\n"
        f"ترافیک کل: <code>{_fmt_bytes(stats.get('total_bytes', 0))}</code>\n"
        f"منقضی در ۷ روز آینده: <b>{stats.get('expiring_soon', 0)}</b>"
    )


def should_alert(sent: dict, uid: str, kind: str, now: Optional[float] = None) -> bool:
    """True when this (user, alert kind) is outside its cooldown."""
    now = now if now is not None else time.time()
    last = sent.get(f"{uid}:{kind}")
    return not isinstance(last, (int, float)) or now - last >= ALERT_COOLDOWN


def record_alert(sent: dict, uid: str, kind: str, now: Optional[float] = None):
    now = now if now is not None else time.time()
    sent[f"{uid}:{kind}"] = now
    if len(sent) > MAX_ALERT_RECORDS:
        # Drop the oldest half rather than one at a time, so this trim runs
        # rarely instead of on every single insert past the cap.
        for key in sorted(sent, key=lambda k: sent[k])[: len(sent) // 2]:
            sent.pop(key, None)


def prune_alerts(sent: dict, now: Optional[float] = None) -> bool:
    """Forget records past their cooldown. True if anything was removed."""
    now = now if now is not None else time.time()
    stale = [k for k, v in sent.items()
             if not isinstance(v, (int, float)) or now - v > ALERT_COOLDOWN * 2]
    for k in stale:
        sent.pop(k, None)
    return bool(stale)


async def verify_credentials(token: str, chat_id: str) -> dict:
    """Send a test message so the admin can confirm setup before relying on it."""
    await send_message(token, chat_id, "✅ اتصال ربات با موفقیت برقرار شد.")
    return {"ok": True}
