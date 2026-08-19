"""
Self-service Telegram bot for end users.

The bot is the one component an untrusted party talks to directly, so the
tests care most about two things: that it can only ever read, and that a
malformed or hostile message cannot make it do something else.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import userbot

ADMIN = {"username": "botadmin", "password": "correct horse battery"}


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        c.post("/api/settings", json={"public_domain": "panel.example.com"})
        yield c


class Ctx:
    """Stands in for the panel side of the bot."""

    def __init__(self, users=None, origin="https://panel.example.com"):
        self.users = users or {}
        self.bindings = {}
        self.binds = {}
        self.origin = origin
        self.panel_name = "Peyk"

    def lookup(self, uid):
        return self.users.get(uid)

    def bound_uid(self, chat):
        return self.bindings.get(str(chat))

    def bind(self, chat, uid):
        self.binds[str(chat)] = uid
        self.bindings[str(chat)] = uid

    def links(self, uid):
        if not self.origin:
            return "", []
        return f"{self.origin}/sub/{uid}", [{"link": f"vless://x@h:443#{uid}"}]


def user(name="Ali", **status):
    st = {"live_enabled": True, "quota_bytes": 10 * 1024 ** 3, "used": 2 * 1024 ** 3,
          "days_left": 20, "active_connections": 1, "expired": False,
          "quota_exceeded": False, "request_exceeded": False}
    st.update(status)
    return {"inbound": {"name": name, "max_connections": 2}, "status": st}


async def reply(text, ctx, chat=555):
    return await userbot.handle_message({"chat": {"id": chat}, "text": text}, ctx)


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("text,cmd,arg", [
    ("/status", "status", ""),
    ("/bind abc123", "bind", "abc123"),
    ("/bind@PeykBot abc123", "bind", "abc123"),
    ("/START", "start", ""),
    ("  /help  ", "help", ""),
    ("hello", "", "hello"),
    ("", "", ""),
])
def test_parse_command(text, cmd, arg):
    assert userbot.parse_command(text) == (cmd, arg)


# ------------------------------------------------------------------ binding
@pytest.mark.anyio
async def test_bind_then_status(anyio_backend):
    ctx = Ctx({"abc123": user("Ali")})
    out = await reply("/bind abc123", ctx)
    assert "Ali" in out
    assert ctx.binds["555"] == "abc123"

    out = await reply("/status", ctx)
    assert "Ali" in out and "20" in out


@pytest.mark.anyio
async def test_bind_accepts_a_pasted_subscription_url(anyio_backend):
    """People paste the whole link rather than the code."""
    ctx = Ctx({"abc123": user()})
    out = await reply("/bind https://panel.example.com/sub/abc123", ctx)
    assert ctx.binds["555"] == "abc123"
    assert "وصل شد" in out

    ctx2 = Ctx({"abc123": user()})
    await reply("/bind https://panel.example.com/sub/abc123?format=clash", ctx2)
    assert ctx2.binds["555"] == "abc123"


@pytest.mark.anyio
async def test_bind_rejects_an_unknown_code(anyio_backend):
    ctx = Ctx({"abc123": user()})
    out = await reply("/bind nope", ctx)
    assert "معتبر نیست" in out
    assert ctx.binds == {}


@pytest.mark.anyio
async def test_commands_require_binding_first(anyio_backend):
    ctx = Ctx({"abc123": user()})
    for cmd in ("/status", "/config"):
        assert "bind" in (await reply(cmd, ctx)).lower()


@pytest.mark.anyio
async def test_rebinding_replaces_the_previous_link(anyio_backend):
    """A resold account must not leave the old owner watching its usage."""
    ctx = Ctx({"a": user("First"), "b": user("Second")})
    await reply("/bind a", ctx)
    await reply("/bind b", ctx)
    assert ctx.bindings["555"] == "b"
    assert (await reply("/status", ctx)).count("First") == 0


@pytest.mark.anyio
async def test_deleted_subscription_is_handled(anyio_backend):
    ctx = Ctx({"abc123": user()})
    await reply("/bind abc123", ctx)
    ctx.users.clear()                      # admin deleted the user
    out = await reply("/status", ctx)
    assert "وجود ندارد" in out


# ------------------------------------------------------------------ read-only
@pytest.mark.anyio
async def test_bot_exposes_no_mutating_commands(anyio_backend):
    """A leaked bot token must not become a leaked panel."""
    ctx = Ctx({"abc123": user()})
    await reply("/bind abc123", ctx)
    for attempt in ("/delete", "/renew 30", "/reset", "/disable", "/adduser x",
                    "/setquota 999", "/admin"):
        out = await reply(attempt, ctx)
        assert "ناشناخته" in out, f"{attempt} was not rejected"
    # nothing was recorded beyond the original binding
    assert ctx.binds == {"555": "abc123"}


@pytest.mark.anyio
async def test_non_command_text_is_ignored(anyio_backend):
    ctx = Ctx({"abc123": user()})
    assert await reply("just chatting", ctx) is None


@pytest.mark.anyio
async def test_message_without_a_chat_is_ignored(anyio_backend):
    ctx = Ctx()
    assert await userbot.handle_message({"text": "/status"}, ctx) is None


# ------------------------------------------------------------------ rendering
def test_status_escapes_user_supplied_names():
    """The name is admin-supplied and goes into HTML parse_mode."""
    text = userbot.render_status({"name": "<script>x</script>", "max_connections": 0},
                                 user()["status"], "Peyk")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_status_shows_the_reason_it_is_off():
    for flag, expected in (("expired", "منقضی"), ("quota_exceeded", "حجم"),
                           ("request_exceeded", "سقف")):
        st = user(**{"live_enabled": False, flag: True})["status"]
        assert expected in userbot.render_status({"name": "A"}, st, "Peyk")


def test_status_handles_unlimited():
    st = user(quota_bytes=0, days_left=None)["status"]
    text = userbot.render_status({"name": "A"}, st, "Peyk")
    assert "نامحدود" in text


def test_progress_bar_is_bounded():
    assert len(userbot._bar(0, 100)) == 10
    assert len(userbot._bar(100, 100)) == 10
    # over-quota must not overflow the bar
    assert len(userbot._bar(500, 100)) == 10
    assert userbot._bar(10, 0) == ""


@pytest.mark.anyio
async def test_config_without_a_public_domain_says_so(anyio_backend):
    """Better an honest message than a link pointing at localhost."""
    ctx = Ctx({"abc123": user()}, origin="")
    await reply("/bind abc123", ctx)
    out = await reply("/config", ctx)
    assert "آماده نیست" in out
    assert "http" not in out


@pytest.mark.anyio
async def test_config_returns_the_subscription_link(anyio_backend):
    ctx = Ctx({"abc123": user()})
    await reply("/bind abc123", ctx)
    out = await reply("/config", ctx)
    assert "https://panel.example.com/sub/abc123" in out


# ------------------------------------------------------------------ rate limit
def test_rate_limit_blocks_a_flood():
    buckets = {}
    allowed = sum(userbot.allow(buckets, 1, now=1000) for _ in range(userbot.RATE_LIMIT + 10))
    assert allowed == userbot.RATE_LIMIT


def test_rate_limit_window_slides():
    buckets = {}
    for _ in range(userbot.RATE_LIMIT):
        userbot.allow(buckets, 1, now=1000)
    assert userbot.allow(buckets, 1, now=1000) is False
    assert userbot.allow(buckets, 1, now=1000 + userbot.RATE_WINDOW + 1) is True


def test_rate_limit_is_per_chat():
    buckets = {}
    for _ in range(userbot.RATE_LIMIT):
        userbot.allow(buckets, "a", now=1000)
    assert userbot.allow(buckets, "a", now=1000) is False
    assert userbot.allow(buckets, "b", now=1000) is True


def test_bindings_are_bounded():
    bindings = {str(i): "u" for i in range(userbot.MAX_BINDINGS + 100)}
    assert userbot.prune_bindings(bindings) is True
    assert len(bindings) == userbot.MAX_BINDINGS
    assert userbot.prune_bindings({"1": "u"}) is False


# ------------------------------------------------------------------ long polling
def test_getupdates_asks_telegram_to_hold_the_connection():
    """Without Telegram's own `timeout` the call returns instantly and the
    caller spins against the API until it is rate limited."""
    import inspect
    src = inspect.getsource(userbot.get_updates)
    assert "timeout=POLL_TIMEOUT" in src
    assert "http_timeout=REQUEST_TIMEOUT" in src
    assert userbot.REQUEST_TIMEOUT > userbot.POLL_TIMEOUT


# ------------------------------------------------------------------ panel api
def test_userbot_status_endpoint(client):
    body = client.get("/api/userbot").json()
    assert body["configured"] is False
    assert body["enabled"] is False
    assert body["public_domain_set"] is True
    assert client.post("/api/userbot/test").status_code == 400


def test_userbot_token_is_validated(client):
    assert client.post("/api/settings", json={"userbot_token": "nonsense"}).status_code == 400
    ok = client.post("/api/settings",
                     json={"userbot_token": "123456789:AAEhBOweik6ad9r_ZeuN65HDdvBcQnKxyz0"})
    assert ok.status_code == 200


def test_admin_can_unbind_a_subscription(client):
    ib = client.post("/api/inbounds", json={"name": "Bound"}).json()["inbound"]

    def _apply(db):
        db.setdefault("bot_bindings", {})["999"] = ib["uid"]
        db["bot_bindings"]["1000"] = ib["uid"]
        db["bot_bindings"]["1001"] = "someone-else"

    client.portal.call(main.store.mutate, _apply)

    r = client.post(f"/api/userbot/unbind/{ib['uid']}")
    assert r.json()["removed"] == 2
    remaining = main.store.get_sync()["bot_bindings"]
    assert "999" not in remaining and "1000" not in remaining
    assert remaining.get("1001") == "someone-else"


def test_userbot_endpoints_require_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.get("/api/userbot").status_code == 401
        assert c.post("/api/userbot/test").status_code == 401
        assert c.post("/api/userbot/unbind/x").status_code == 401
