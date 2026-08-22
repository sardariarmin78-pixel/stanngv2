"""
Renewal requests from the bot, approved by the admin with a button.

This is the one path where a message from an untrusted chat can end in a
subscription being extended, so most of these tests are about the boundary:
the request only ever *asks*, and the approval is only honoured when it comes
from the admin's own chat.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import userbot

ADMIN = {"username": "rnadmin", "password": "correct horse battery"}
ADMIN_CHAT = "987654321"
USER_CHAT = 555000111
BOT_TOKEN = "123456789:AAEhBOweik6ad9r_ZeuN65HDdvBcQnKxyz0"


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


@pytest.fixture(autouse=True)
def session(client):
    if client.get("/api/setup-status").json().get("needs_setup"):
        client.post("/api/setup", json=ADMIN)
    client.post("/api/login", json=ADMIN)
    client.post("/api/settings", json={
        "telegram_bot_token": BOT_TOKEN, "telegram_chat_id": ADMIN_CHAT,
        "userbot_token": BOT_TOKEN, "userbot_enabled": True,
        "userbot_renew_enabled": True,
    })

    def _clear(db):
        db["renew_requests"] = {}
        db["bot_bindings"] = {}

    client.portal.call(main.store.mutate, _clear)
    yield


class FakeBot:
    """Records what would have been sent to Telegram."""

    def __init__(self):
        self.sent = []
        self.buttons = []
        self.edits = []
        self.acks = []
        self.next_message_id = 4200

    async def send(self, token, chat, text, timeout=20):
        self.sent.append({"chat": chat, "text": text})
        return {"message_id": 1}

    async def send_with_buttons(self, token, chat, text, keyboard, timeout=20):
        self.next_message_id += 1
        self.buttons.append({"chat": chat, "text": text, "keyboard": keyboard,
                             "message_id": self.next_message_id})
        return {"message_id": self.next_message_id}

    async def edit_message(self, token, chat, message_id, text, timeout=20):
        self.edits.append({"chat": chat, "message_id": message_id, "text": text})
        return {}

    async def answer_callback(self, token, callback_id, text="", timeout=15):
        self.acks.append(text)
        return {}


@pytest.fixture
def bot(monkeypatch):
    fake = FakeBot()
    monkeypatch.setattr(userbot, "send", fake.send)
    monkeypatch.setattr(userbot, "send_with_buttons", fake.send_with_buttons)
    monkeypatch.setattr(userbot, "edit_message", fake.edit_message)
    monkeypatch.setattr(userbot, "answer_callback", fake.answer_callback)
    return fake


def make_request(client, bot, name="Ali", expire_days=1):
    """Create a user and a pending renewal request for it."""
    ib = client.post("/api/inbounds",
                     json={"name": name, "quota_gb": 5,
                           "expire_days": expire_days}).json()["inbound"]

    def _apply(db):
        db.setdefault("renew_requests", {})["req12345"] = {
            "uid": ib["uid"], "chat": USER_CHAT, "name": name,
            "created_at": 1000.0, "status": "pending", "admin_message_id": 4321,
        }

    client.portal.call(main.store.mutate, _apply)
    return ib


def callback(data, chat=ADMIN_CHAT, message_id=4321):
    return {"id": "cb1", "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}}}


# ------------------------------------------------------------------ approval
def test_approval_extends_and_resets(client, bot):
    ib = make_request(client, bot)
    before = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    before["used_down"] = 3 * 1024 ** 3

    client.portal.call(main._handle_renew_callback, callback("rn:req12345:60"))

    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert main.inbound_status(row)["days_left"] >= 59
    assert row["used_down"] == 0           # usage restarts with the renewal
    assert row["enabled"] is True
    assert main.store.get_sync()["renew_requests"]["req12345"]["status"] == "approved"


def test_approval_tells_both_sides(client, bot):
    make_request(client, bot, name="Sara")
    client.portal.call(main._handle_renew_callback, callback("rn:req12345:30"))

    # the admin's message loses its buttons and states the outcome
    assert bot.edits and "Sara" in bot.edits[0]["text"]
    assert "30" in bot.edits[0]["text"]
    # and the user hears about it in their own chat
    assert bot.sent and bot.sent[0]["chat"] == USER_CHAT
    assert "30" in bot.sent[0]["text"]


def test_rejection_changes_nothing(client, bot):
    ib = make_request(client, bot)
    before = dict(main.inbound_by_uid(main.store.get_sync(), ib["uid"]))

    client.portal.call(main._handle_renew_callback, callback("rn:req12345:x"))

    after = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert after["expire_at"] == before["expire_at"]
    assert main.store.get_sync()["renew_requests"]["req12345"]["status"] == "rejected"
    assert bot.sent and "تأیید نشد" in bot.sent[0]["text"]


# ------------------------------------------------------------------ the boundary
def test_only_the_admin_chat_can_approve(client, bot):
    """The keyboard only goes to the admin, but a tap arriving from anywhere
    else must not extend anything — otherwise a user approves their own."""
    ib = make_request(client, bot)
    before = dict(main.inbound_by_uid(main.store.get_sync(), ib["uid"]))

    client.portal.call(main._handle_renew_callback,
                       callback("rn:req12345:365", chat=USER_CHAT))

    after = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert after["expire_at"] == before["expire_at"]
    assert main.store.get_sync()["renew_requests"]["req12345"]["status"] == "pending"
    assert not bot.edits


def test_a_request_is_decided_once(client, bot):
    ib = make_request(client, bot)
    client.portal.call(main._handle_renew_callback, callback("rn:req12345:30"))
    first = main.inbound_by_uid(main.store.get_sync(), ib["uid"])["expire_at"]

    # a second tap on the same (stale) keyboard must not stack another 30 days
    client.portal.call(main._handle_renew_callback, callback("rn:req12345:30"))
    assert main.inbound_by_uid(main.store.get_sync(), ib["uid"])["expire_at"] == first
    assert any("قبلاً" in a for a in bot.acks)


def test_unknown_request_is_ignored(client, bot):
    client.portal.call(main._handle_renew_callback, callback("rn:nosuchid:30"))
    assert not bot.edits


@pytest.mark.parametrize("data", ["", "garbage", "xx:1:2", "rn:req12345:abc",
                                  "rn:req12345", "rn::30"])
def test_malformed_callbacks_are_ignored(client, bot, data):
    ib = make_request(client, bot)
    before = dict(main.inbound_by_uid(main.store.get_sync(), ib["uid"]))
    client.portal.call(main._handle_renew_callback, callback(data))
    after = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert after["expire_at"] == before["expire_at"]


def test_deleted_subscription_is_handled(client, bot):
    ib = make_request(client, bot)
    client.delete(f"/api/inbounds/{ib['uid']}")
    client.portal.call(main._handle_renew_callback, callback("rn:req12345:30"))
    assert main.store.get_sync()["renew_requests"]["req12345"]["status"] == "rejected"
    assert any("وجود ندارد" in a for a in bot.acks)


# ------------------------------------------------------------------ options
def test_renew_options_are_validated(client):
    assert client.post("/api/settings",
                       json={"userbot_renew_options": "notalist"}).status_code == 400
    assert client.post("/api/settings",
                       json={"userbot_renew_options": []}).status_code == 400
    assert client.post("/api/settings",
                       json={"userbot_renew_options": [0, -5]}).status_code == 400
    r = client.post("/api/settings", json={"userbot_renew_options": [7, 30, 90, 180, 365]})
    # capped at four, since that is what fits on one keyboard row
    assert r.json()["settings"]["userbot_renew_options"] == [7, 30, 90, 180]


def test_keyboard_follows_the_configured_options(client, bot):
    client.post("/api/settings", json={"userbot_renew_options": [15, 45]})
    db = main.store.get_sync()
    keyboard = userbot.renew_keyboard("abc", main._renew_options(db))
    labels = [b["text"] for b in keyboard[0]]
    assert labels == ["15 روز", "45 روز"]
    assert keyboard[-1][0]["callback_data"].endswith(":x")


def test_malformed_options_fall_back(client):
    db = {"settings": {"userbot_renew_options": ["a", None, 99999]}}
    assert main._renew_options(db) == [30, 60, 90]


# ------------------------------------------------------------------ panel view
def test_requests_are_listed_for_the_admin(client, bot):
    make_request(client, bot, name="Listed")
    body = client.get("/api/userbot/requests").json()
    assert body["pending"] == 1
    assert body["requests"][0]["name"] == "Listed"
    # the internal message id is not part of the panel's view
    assert "admin_message_id" not in body["requests"][0]


def test_requests_endpoint_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.get("/api/userbot/requests").status_code == 401


def test_requests_are_bounded():
    import time
    reqs = {f"r{i}": {"created_at": time.time()} for i in range(userbot.MAX_REQUESTS + 50)}
    assert userbot.prune_requests(reqs) is True
    assert len(reqs) == userbot.MAX_REQUESTS


def test_stale_requests_expire():
    import time
    reqs = {"old": {"created_at": time.time() - userbot.REQUEST_TTL - 1},
            "fresh": {"created_at": time.time()}}
    assert userbot.prune_requests(reqs) is True
    assert list(reqs) == ["fresh"]
