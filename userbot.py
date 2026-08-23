"""
Self-service Telegram bot for end users.

Without it every "how much data do I have left?" and every lost config comes
to the seller by hand. This lets a user bind their subscription once and then
answer those questions themselves.

Design constraints that shaped it:

* **Read-only.** The bot can show a config and a usage figure. It can never
  create, extend, disable or delete anything. A compromised bot token must
  not become a compromised panel.
* **Long polling, no webhook.** A webhook needs a public HTTPS endpoint and a
  registered URL; polling works on any deployment, including one behind a CDN.
* **Binding is by uid, which is already the subscription secret.** Anyone who
  can send the uid already holds the subscription, so this grants nothing new
  — but the binding is remembered so it only has to be sent once.
* **One chat, one subscription.** Rebinding replaces the previous link rather
  than accumulating, so a resold account cannot quietly keep the old owner
  subscribed to its usage.
"""
import json
import time
from typing import Dict, List, Optional

import httpx

TELEGRAM_API = "https://api.telegram.org"
POLL_TIMEOUT = 25          # seconds Telegram holds an empty long poll open
REQUEST_TIMEOUT = POLL_TIMEOUT + 15
# A chat may issue at most this many commands per window, so one user cannot
# spin the panel with a held-down button.
RATE_LIMIT = 20
RATE_WINDOW = 60
MAX_BINDINGS = 20000
# A user may have one open renewal request at a time; more would just spam
# the admin with duplicates of the same ask.
MAX_REQUESTS = 500
REQUEST_TTL = 7 * 24 * 3600


class UserBotError(Exception):
    pass


async def _call(token: str, method: str, http_timeout: float = 20, **params) -> dict:
    """`http_timeout` is ours; anything in **params goes to Telegram verbatim."""
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            r = await client.post(url, json=params)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise UserBotError(f"unreachable: {e}") from e
    if not data.get("ok"):
        raise UserBotError(data.get("description") or f"error-{r.status_code}")
    return data["result"]


async def get_updates(token: str, offset: int = 0) -> List[dict]:
    """One long poll. Returns an empty list when nothing arrived."""
    # `timeout` here is Telegram's long-poll duration: without it the call
    # returns immediately and the caller spins against the API.
    return await _call(token, "getUpdates", http_timeout=REQUEST_TIMEOUT,
                       offset=offset, timeout=POLL_TIMEOUT, limit=50,
                       allowed_updates=["message", "callback_query"]) or []


async def send(token: str, chat_id, text: str, timeout: float = 20) -> dict:
    return await _call(token, "sendMessage", http_timeout=timeout,
                       chat_id=chat_id, text=text, parse_mode="HTML",
                       disable_web_page_preview=True)


async def send_with_buttons(token: str, chat_id, text: str, keyboard: list,
                            timeout: float = 20) -> dict:
    """Send a message carrying an inline keyboard."""
    return await _call(token, "sendMessage", http_timeout=timeout,
                       chat_id=chat_id, text=text, parse_mode="HTML",
                       disable_web_page_preview=True,
                       reply_markup={"inline_keyboard": keyboard})


async def edit_message(token: str, chat_id, message_id: int, text: str,
                       timeout: float = 20) -> dict:
    """Rewrite a sent message, dropping its keyboard.

    Used after a decision so the buttons cannot be tapped a second time.
    """
    return await _call(token, "editMessageText", http_timeout=timeout,
                       chat_id=chat_id, message_id=message_id, text=text,
                       parse_mode="HTML", disable_web_page_preview=True)


async def answer_callback(token: str, callback_id: str, text: str = "",
                          timeout: float = 15) -> dict:
    """Acknowledge a button tap; without this the client spins."""
    return await _call(token, "answerCallbackQuery", http_timeout=timeout,
                       callback_query_id=callback_id, text=text[:200])


async def get_me(token: str) -> dict:
    return await _call(token, "getMe")


# ------------------------------------------------------------------ rate limit
def allow(buckets: Dict[str, list], chat_id, now: Optional[float] = None) -> bool:
    """Sliding-window limiter, kept in memory only."""
    now = now if now is not None else time.time()
    key = str(chat_id)
    hits = [t for t in buckets.get(key, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        buckets[key] = hits
        return False
    hits.append(now)
    buckets[key] = hits
    return True


# ------------------------------------------------------------------ formatting
def _bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def _escape(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _bar(used: int, total: int, width: int = 10) -> str:
    if not total:
        return ""
    filled = max(0, min(width, round(used / total * width)))
    return "▓" * filled + "░" * (width - filled)


HELP = (
    "<b>راهنما</b>\n\n"
    "/start — شروع\n"
    "/bind &lt;کد&gt; — اتصال اشتراک شما به این چت\n"
    "/redeem &lt;کد-شارژ&gt; — فعال‌سازی اشتراک با کد خرید\n"
    "/buy — خرید اشتراک\n"
    "/invite — دعوت دوستان و گرفتن هدیه\n"
    "/trial — دریافت اکانت تست رایگان\n"
    "/status — حجم و اعتبار باقی‌مانده\n"
    "/config — لینک اشتراک و کانفیگ\n"
    "/renew — درخواست تمدید از فروشنده\n"
    "/help — همین راهنما\n\n"
    "کد اشتراک را از فروشنده بگیرید."
)



# Reasons redeem_voucher can refuse, as sentences a customer can act on.
VOUCHER_ERRORS = {
    "invalid-code": "❌ این کد معتبر نیست. دوباره از روی رسید بخوانید یا از فروشنده بپرسید.",
    "already-used": "❌ این کد قبلاً استفاده شده است.",
    "plan-gone": "❌ پلن این کد دیگر موجود نیست. با فروشنده تماس بگیرید.",
    "panel-full": "❌ ظرفیت سرویس پر است. کمی بعد دوباره تلاش کنید.",
    "disabled": "فعال‌سازی با کد در این ربات فعال نیست.",
}

# Reasons claim_trial can refuse, as sentences a customer can act on.
TRIAL_ERRORS = {
    "disabled": "اکانت تست خودکار فعال نیست. با پشتیبانی تماس بگیرید.",
    "already-claimed": "شما قبلاً اکانت تست خود را گرفته‌اید. برای خرید اشتراک با پشتیبانی در تماس باشید.",
    "already-subscribed": "شما الان یک اشتراک فعال دارید. وضعیت آن را با /status ببینید.",
    "panel-full": "ظرفیت سرویس پر است. کمی بعد دوباره تلاش کنید.",
}


def render_status(ib: dict, status: dict, panel: str) -> str:
    quota = status.get("quota_bytes") or 0
    used = status.get("used") or 0
    lines = [f"<b>{_escape(panel)}</b>", "", f"👤 {_escape(ib.get('name'))}"]

    if status.get("live_enabled"):
        lines.append("🟢 وضعیت: فعال")
    else:
        reason = ("منقضی شده" if status.get("expired")
                  else "حجم تمام شده" if status.get("quota_exceeded")
                  else "سقف درخواست" if status.get("request_exceeded")
                  else "غیرفعال")
        lines.append(f"🔴 وضعیت: {reason}")

    if quota:
        pct = min(100, used / quota * 100)
        lines.append(f"\n📊 مصرف: {_bytes(used)} از {_bytes(quota)}")
        lines.append(f"<code>{_bar(used, quota)}</code> {pct:.0f}%")
        lines.append(f"باقی‌مانده: <b>{_bytes(max(0, quota - used))}</b>")
    else:
        lines.append(f"\n📊 مصرف: {_bytes(used)} (نامحدود)")

    days = status.get("days_left")
    if days is None:
        lines.append("⏳ اعتبار: نامحدود")
    else:
        lines.append(f"⏳ اعتبار: <b>{days}</b> روز باقی‌مانده")

    max_conn = ib.get("max_connections") or 0
    active = status.get("active_connections", 0)
    lines.append(f"📱 اتصال فعال: {active}" + (f" از {max_conn}" if max_conn else ""))
    return "\n".join(lines)


def render_config(sub_url: str, configs: List[dict], panel: str) -> str:
    if not sub_url:
        # No public domain configured, so there is no URL worth handing out.
        return (f"<b>{_escape(panel)}</b>\n\n"
                "⚠️ لینک اشتراک هنوز آماده نیست. با پشتیبانی تماس بگیرید.")
    lines = [f"<b>{_escape(panel)}</b>", "", "🔗 <b>لینک اشتراک</b> (پیشنهادی):",
             f"<code>{_escape(sub_url)}</code>", "",
             "این لینک را در کلاینت خود اضافه کنید تا همه لوکیشن‌ها را بگیرید "
             "و با فیلتر شدن یکی، بقیه کار کنند."]
    if configs:
        lines += ["", "یا یک کانفیگ مستقیم:", f"<code>{_escape(configs[0]['link'])}</code>"]
    return "\n".join(lines)


# ------------------------------------------------------------------ renewals
def renew_keyboard(request_id: str, day_options: list) -> list:
    """Inline keyboard the admin taps to decide a renewal.

    callback_data is capped at 64 bytes by Telegram, so it carries only a
    short request id and the choice; everything else is looked up panel-side.
    """
    row = [{"text": f"{d} روز", "callback_data": f"rn:{request_id}:{d}"}
           for d in day_options[:4]]
    return [row, [{"text": "رد", "callback_data": f"rn:{request_id}:x"}]]


def parse_callback(data: str) -> tuple:
    """('rn', request_id, days|None) — days is None for a rejection."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "rn":
        return None, None, None
    request_id = parts[1]
    if parts[2] == "x":
        return "rn", request_id, None
    try:
        return "rn", request_id, int(parts[2])
    except ValueError:
        return None, None, None


def format_renew_request(panel: str, name: str, status: dict) -> str:
    days = status.get("days_left")
    used = status.get("used") or 0
    quota = status.get("quota_bytes") or 0
    lines = [f"🔔 <b>{_escape(panel)}</b> — درخواست تمدید", "",
             f"👤 {_escape(name)}"]
    if quota:
        lines.append(f"📊 {_bytes(used)} از {_bytes(quota)}")
    else:
        lines.append(f"📊 {_bytes(used)} (نامحدود)")
    lines.append("⏳ " + ("منقضی شده" if status.get("expired")
                         else f"{days} روز باقی‌مانده" if days is not None
                         else "بدون انقضا"))
    return "\n".join(lines)


def prune_requests(requests: dict, now: Optional[float] = None) -> bool:
    """Drop stale and excess requests. True if anything was removed."""
    now = now if now is not None else time.time()
    stale = [k for k, v in requests.items()
             if not isinstance(v, dict) or now - v.get("created_at", 0) > REQUEST_TTL]
    for k in stale:
        requests.pop(k, None)
    removed = bool(stale)
    if len(requests) > MAX_REQUESTS:
        for k in sorted(requests, key=lambda k: requests[k].get("created_at", 0))[
                :len(requests) - MAX_REQUESTS]:
            requests.pop(k, None)
        removed = True
    return removed


# ------------------------------------------------------------------ customer nudges
def _renew_tail(can_renew: bool) -> str:
    if can_renew:
        return "\n\nبرای تمدید دستور /renew را بزنید."
    return "\n\nبرای تمدید با پشتیبانی در تماس باشید."


def format_customer_quota(panel: str, name: str, used: int, quota: int,
                          percent: float, can_renew: bool) -> str:
    """Addressed to the customer, not about them.

    The owner's copy of this alert is a status report. This one has to read
    like a helpful heads-up, or it comes across as nagging someone who paid.
    """
    return (
        f"⚠️ <b>{_escape(panel)}</b>\n\n"
        f"سلام! {percent:.0f}٪ از حجم اشتراک <b>{_escape(name)}</b> مصرف شده.\n"
        f"مصرف: <code>{_bytes(used)}</code> از <code>{_bytes(quota)}</code>"
        + _renew_tail(can_renew)
    )


def format_customer_expiry(panel: str, name: str, days_left: int,
                           can_renew: bool) -> str:
    when = "امروز" if days_left <= 0 else f"<b>{days_left}</b> روز دیگر"
    return (
        f"⏳ <b>{_escape(panel)}</b>\n\n"
        f"اشتراک <b>{_escape(name)}</b> شما {when} تمام می‌شود."
        + _renew_tail(can_renew)
    )


# ------------------------------------------------------------------ dispatch
def parse_command(text: str) -> tuple:
    """Split '/cmd@BotName arg' into (cmd, arg). Not a command -> ('', text)."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", text
    head, _, rest = text.partition(" ")
    cmd = head[1:].split("@")[0].lower()
    return cmd, rest.strip()


async def handle_message(message: dict, ctx) -> Optional[str]:
    """Turn one incoming message into a reply, or None to stay silent.

    `ctx` supplies the panel side: lookup(uid), bind(chat, uid),
    bound_uid(chat), links(uid), panel_name.
    """
    chat = (message.get("chat") or {}).get("id")
    if chat is None:
        return None
    cmd, arg = parse_command(message.get("text") or "")

    if cmd in ("start", "help"):
        bound = ctx.bound_uid(chat)
        # Telegram passes a deep link's payload as the argument to /start, so
        # an invited customer arrives already carrying their referrer's code.
        invited = False
        if cmd == "start" and arg.startswith("ref_"):
            invited = ctx.note_referral(chat, arg.strip())
        if cmd == "start" and not bound:
            welcome = "👋 خوش آمدید.\n\n"
            if invited:
                welcome += ("🎁 با لینک دعوت وارد شدید — بعد از اولین خرید، "
                            "هدیه‌ی شما و دعوت‌کننده‌تان اضافه می‌شود.\n\n")
            return (welcome + "برای شروع، کد اشتراک خود را بفرستید:\n"
                    "<code>/bind کد-اشتراک</code>\n\n" + HELP)
        return HELP

    if cmd == "invite":
        if not getattr(ctx, "referral_enabled", False):
            return "دعوت دوستان در این ربات فعال نیست."
        uid = ctx.bound_uid(chat)
        if not uid:
            return "اول اشتراک خود را وصل کنید:\n<code>/bind کد-اشتراک</code>"
        info = ctx.invite_info(uid)
        if not info:
            return "❌ این اشتراک دیگر وجود ندارد."
        link = info.get("link") or info.get("code")
        return (f"🎁 <b>دعوت دوستان</b>\n\n"
                f"این لینک را برای دوستانتان بفرستید:\n<code>{_escape(link)}</code>\n\n"
                f"وقتی از طریق آن اولین خریدشان را انجام دهند، "
                f"<b>{info['days']}</b> روز به اشتراک هر دوی شما اضافه می‌شود.")

    if cmd == "bind":
        uid = arg.strip()
        # Tolerate a pasted subscription URL instead of a bare code.
        if "/" in uid:
            uid = uid.rstrip("/").split("/")[-1].split("?")[0]
        if not uid:
            return "کد اشتراک را بعد از دستور بنویسید:\n<code>/bind کد-اشتراک</code>"
        found = ctx.lookup(uid)
        if not found:
            return "❌ این کد معتبر نیست. از فروشنده کد درست را بگیرید."
        ctx.bind(chat, uid)
        # lookup() returns {"inbound": ..., "status": ...}; the name is inside.
        name = (found.get("inbound") or {}).get("name", "")
        return f"✅ اشتراک <b>{_escape(name)}</b> به این چت وصل شد.\n\n" + HELP

    if cmd == "redeem":
        if not getattr(ctx, "voucher_enabled", False):
            return VOUCHER_ERRORS["disabled"]
        if not arg.strip():
            return ("کد خرید را بعد از دستور بنویسید:\n"
                    "<code>/redeem ABCD-EFGH-JKLM</code>")
        # Rate-limited by the same per-chat budget as every other command, so
        # a code cannot be brute-forced from here.
        outcome = await ctx.redeem(chat, arg)
        if outcome.get("error"):
            return VOUCHER_ERRORS.get(outcome["error"], "❌ انجام نشد.")
        return ("🎉 اشتراک شما فعال شد!\n\n"
                + render_config(outcome["sub_url"], outcome["configs"], ctx.panel_name))

    if cmd == "buy":
        if not getattr(ctx, "shop_enabled", False):
            return SHOP_ERRORS["disabled"]
        if ctx.has_open_order(chat):
            return SHOP_ERRORS["pending"]
        plans = ctx.shop_plans()
        if not plans:
            return SHOP_ERRORS["no-plans"]
        await ctx.send_shop(chat, format_shop(ctx.panel_name, plans),
                            shop_keyboard(plans))
        return None

    if cmd == "trial":
        if not getattr(ctx, "trial_enabled", False):
            return TRIAL_ERRORS["disabled"]
        outcome = await ctx.claim_trial(chat)
        if outcome.get("error"):
            return TRIAL_ERRORS.get(outcome["error"], "❌ انجام نشد.")
        return ("🎁 اکانت تست شما ساخته شد!\n\n"
                + render_config(outcome["sub_url"], outcome["configs"], ctx.panel_name)
                + "\n\nوضعیت مصرف را هر وقت خواستید با /status ببینید.")

    if cmd == "renew":
        uid = ctx.bound_uid(chat)
        if not uid:
            return "اول اشتراک خود را وصل کنید:\n<code>/bind کد-اشتراک</code>"
        found = ctx.lookup(uid)
        if not found:
            return "❌ این اشتراک دیگر وجود ندارد. با فروشنده تماس بگیرید."
        if not ctx.renew_enabled:
            return "درخواست تمدید از ربات فعال نیست. با فروشنده تماس بگیرید."
        if ctx.has_open_request(chat):
            return "⏳ درخواست قبلی شما هنوز بررسی نشده است."
        ctx.request_renew(chat, uid, found)
        return "✅ درخواست تمدید برای فروشنده ارسال شد. نتیجه همین‌جا اعلام می‌شود."

    if cmd in ("status", "config", "usage"):
        uid = ctx.bound_uid(chat)
        if not uid:
            return "اول اشتراک خود را وصل کنید:\n<code>/bind کد-اشتراک</code>"
        found = ctx.lookup(uid)
        if not found:
            # The subscription was deleted after binding.
            return "❌ این اشتراک دیگر وجود ندارد. با فروشنده تماس بگیرید."
        if cmd == "config":
            sub_url, configs = ctx.links(uid)
            return render_config(sub_url, configs, ctx.panel_name)
        return render_status(found["inbound"], found["status"], ctx.panel_name)

    # A photo with no command is only ever a payment receipt, and only when
    # the customer has a plan waiting for one.
    if not cmd and largest_photo(message):
        if not getattr(ctx, "shop_enabled", False):
            return None
        file_id = largest_photo(message)
        outcome = await ctx.submit_receipt(chat, file_id)
        if outcome.get("error"):
            return SHOP_ERRORS.get(outcome["error"], "❌ انجام نشد.")
        return ("✅ رسید شما دریافت شد و برای فروشنده ارسال شد.\n\n"
                "نتیجه‌ی بررسی همین‌جا اعلام می‌شود.")

    if cmd:
        return "دستور ناشناخته.\n\n" + HELP
    return None


def prune_bindings(bindings: dict) -> bool:
    """Keep the binding table bounded. True if anything was dropped."""
    if len(bindings) <= MAX_BINDINGS:
        return False
    for key in list(bindings)[: len(bindings) - MAX_BINDINGS]:
        bindings.pop(key, None)
    return True


# ------------------------------------------------------------------ shop
SHOP_ERRORS = {
    "disabled": "فروش از ربات فعال نیست. برای خرید با پشتیبانی در تماس باشید.",
    "no-plans": "هنوز پلنی برای فروش تعریف نشده. با پشتیبانی تماس بگیرید.",
    "plan-gone": "این پلن دیگر موجود نیست. دوباره /buy را بزنید.",
    "no-order": "سفارش بازی ندارید. برای شروع /buy را بزنید.",
    "pending": "یک سفارش در انتظار بررسی دارید. لطفاً تا اعلام نتیجه صبر کنید.",
    "too-large": "حجم عکس زیاد است. لطفاً تصویر کوچک‌تری بفرستید.",
}


async def send_photo_bytes(token: str, chat_id, blob: bytes, caption: str,
                           keyboard: Optional[list] = None) -> dict:
    """Upload raw image bytes.

    A file_id belongs to the bot that received it, so a receipt taken in by
    the customer bot cannot be re-sent by the alert bot. The bytes have to
    make the trip.
    """
    url = f"{TELEGRAM_API}/bot{token}/sendPhoto"
    files = {"photo": ("receipt.jpg", blob, "image/jpeg")}
    data = {"chat_id": str(chat_id), "caption": caption[:1000], "parse_mode": "HTML"}
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(url, data=data, files=files)
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise UserBotError(f"telegram-unreachable: {e}") from e
    if not body.get("ok"):
        raise UserBotError(body.get("description") or f"error-{r.status_code}")
    return body["result"]


async def download_file(token: str, file_id: str, max_bytes: int) -> bytes:
    """Fetch a file the bot was sent, refusing anything oversized."""
    meta = await _call(token, "getFile", file_id=file_id)
    path = meta.get("file_path")
    if not path:
        raise UserBotError("no-file-path")
    if (meta.get("file_size") or 0) > max_bytes:
        raise UserBotError("too-large")

    url = f"{TELEGRAM_API}/file/bot{token}/{path}"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise UserBotError(f"download-{resp.status_code}")
                chunks, total = [], 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    # The size in getFile is advisory; enforce it here too so a
                    # lying or chunked response cannot blow up memory.
                    if total > max_bytes:
                        raise UserBotError("too-large")
                    chunks.append(chunk)
    except httpx.HTTPError as e:
        raise UserBotError(f"telegram-unreachable: {e}") from e
    return b"".join(chunks)


def largest_photo(message: dict) -> Optional[str]:
    """Telegram sends every rendered size; the last is the biggest."""
    photos = message.get("photo")
    if not isinstance(photos, list) or not photos:
        return None
    best = max(photos, key=lambda p: (p.get("file_size") or 0))
    return best.get("file_id")


def shop_keyboard(plans: List[dict]) -> list:
    """One plan per row: names and prices are too long to sit side by side."""
    rows = []
    for plan in plans[:20]:
        label = plan["name"]
        if plan.get("price"):
            label += f" — {plan['price']:,} {plan.get('currency', 'تومان')}"
        rows.append([{"text": label[:60], "callback_data": f"buy:{plan['id']}"}])
    return rows


def order_keyboard(order_id: str) -> list:
    return [[
        {"text": "✅ تأیید و ساخت اکانت", "callback_data": f"ord:{order_id}:ok"},
        {"text": "❌ رد", "callback_data": f"ord:{order_id}:no"},
    ]]


def format_shop(panel: str, plans: List[dict]) -> str:
    lines = [f"🛒 <b>{_escape(panel)}</b>", "", "یکی از پلن‌ها را انتخاب کنید:", ""]
    for plan in plans[:20]:
        bits = []
        if plan.get("quota_gb"):
            bits.append(f"{plan['quota_gb']:g} گیگ")
        else:
            bits.append("حجم نامحدود")
        bits.append(f"{plan['days']} روزه" if plan.get("days") else "بدون انقضا")
        if plan.get("max_connections"):
            bits.append(f"{plan['max_connections']} دستگاه")
        price = f"{plan['price']:,} {plan.get('currency', 'تومان')}" if plan.get("price") else "—"
        lines.append(f"• <b>{_escape(plan['name'])}</b> — {' · '.join(bits)} — <b>{price}</b>")
    return "\n".join(lines)


def format_payment(panel: str, plan: dict, instructions: str) -> str:
    price = f"{plan['price']:,} {plan.get('currency', 'تومان')}" if plan.get("price") else "—"
    return (
        f"💳 <b>{_escape(panel)}</b>\n\n"
        f"پلن انتخابی: <b>{_escape(plan['name'])}</b>\n"
        f"مبلغ قابل پرداخت: <b>{price}</b>\n\n"
        f"{instructions}\n\n"
        "پس از واریز، <b>عکس رسید</b> را همین‌جا بفرستید. "
        "سفارش شما بعد از تأیید فروشنده فعال می‌شود."
    )


def format_order_for_admin(panel: str, plan_name: str, price, chat_id) -> str:
    amount = f"{price:,}" if isinstance(price, int) else str(price)
    return (
        f"🧾 <b>{_escape(panel)}</b> — سفارش جدید\n\n"
        f"پلن: <b>{_escape(plan_name)}</b>\n"
        f"مبلغ: <b>{amount}</b>\n"
        f"چت مشتری: <code>{_escape(chat_id)}</code>\n\n"
        "رسید بالا را بررسی کنید."
    )


def parse_order_callback(data: str) -> tuple:
    """'ord:<id>:ok' -> (id, True). Anything else -> (None, None)."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "ord":
        return None, None
    return parts[1], parts[2] == "ok"
