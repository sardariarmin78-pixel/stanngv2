"""
Messaging every customer at once.

The property that matters most here is who a broadcast can reach. It goes only
to people who bound their own subscription to the bot, and a reseller reaches
only its own customers -- getting either wrong means messaging strangers.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import userbot

ADMIN = {"username": "bcadmin", "password": "correct horse battery"}
SELLER = {"username": "bc_seller", "password": "seller-pass-1234"}
BOT_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789"


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


@pytest.fixture(autouse=True)
def clean(client):
    if client.get("/api/setup-status").json().get("needs_setup"):
        client.post("/api/setup", json=ADMIN)
    client.post("/api/login", json=ADMIN)
    db = main.store.get_sync()
    db["bot_bindings"] = {}
    db["broadcast_log"] = []
    for ib in client.get("/api/inbounds").json()["inbounds"]:
        client.delete(f"/api/inbounds/{ib['uid']}")
    for r in client.get("/api/resellers").json()["resellers"]:
        client.delete(f"/api/resellers/{r['id']}?delete_users=1")
    # A real-shaped token: the panel validates the format, and a silently
    # rejected settings POST would leave every send test failing obscurely.
    r = client.post("/api/settings", json={"userbot_enabled": True,
                                           "userbot_token": BOT_TOKEN,
                                           "broadcast_route_changes": False})
    assert r.status_code == 200, r.text
    yield


@pytest.fixture
def outbox(monkeypatch):
    """Capture what would go to Telegram instead of sending it."""
    sent = []

    async def fake_send(token, chat, text, **kw):
        if chat == "blocked":
            raise userbot.UserBotError("blocked by user")
        sent.append((str(chat), text))

    monkeypatch.setattr(userbot, "send", fake_send)
    # The real pause paces Telegram's rate limit; patching asyncio.sleep itself
    # would stall the event loop the test client runs on.
    monkeypatch.setattr(main, "BROADCAST_PAUSE", 0)
    return sent


def make_user(client, name):
    return client.post("/api/inbounds", json={"name": name, "quota_gb": 5}).json()["inbound"]


def bind(uid, chat):
    main.store.get_sync().setdefault("bot_bindings", {})[str(chat)] = uid


def login_as(creds):
    c = TestClient(main.app)
    assert c.post("/api/login", json=creds).status_code == 200
    return c


# ------------------------------------------------------------------ audience
def test_only_customers_who_opted_in_are_reachable(client):
    """Binding is the opt-in. Someone who never messaged the bot has no chat
    to message and must never be counted as reachable."""
    a = make_user(client, "Bound")
    make_user(client, "NeverUsedTheBot")
    bind(a["uid"], 111)

    body = client.get("/api/broadcast").json()
    assert body["audience"] == 1
    assert body["customers"] == 2


def test_a_binding_for_a_deleted_customer_is_dropped(client):
    ib = make_user(client, "Gone")
    bind(ib["uid"], 111)
    client.delete(f"/api/inbounds/{ib['uid']}")
    assert client.get("/api/broadcast").json()["audience"] == 0


def test_one_customer_bound_twice_is_messaged_once(client):
    """Rebinding from a second device leaves two chats for one subscription."""
    ib = make_user(client, "TwoDevices")
    bind(ib["uid"], 111)
    bind(ib["uid"], 222)
    assert client.get("/api/broadcast").json()["audience"] == 1


def test_the_audience_is_empty_without_a_bot(client):
    client.post("/api/settings", json={"userbot_enabled": False})
    assert client.get("/api/broadcast").json()["bot_ready"] is False


# ------------------------------------------------------------------ sending
def test_a_broadcast_reaches_everyone_bound(client, outbox):
    for i, name in enumerate(("A", "B", "C")):
        bind(make_user(client, name)["uid"], 100 + i)

    r = client.post("/api/broadcast", json={"text": "سرور جدید اضافه شد"})
    assert r.status_code == 200
    assert r.json()["sent"] == 3
    assert len(outbox) == 3
    assert all("سرور جدید اضافه شد" in text for _chat, text in outbox)


def test_the_panel_name_heads_the_message(client, outbox):
    """An unheralded message from a bot bound weeks ago reads as spam."""
    bind(make_user(client, "A")["uid"], 111)
    client.post("/api/broadcast", json={"text": "سلام"})
    assert "TestPanel" in outbox[0][1]


def test_html_in_the_body_is_escaped(client, outbox):
    """The seller's text is data, not markup -- a stray < would otherwise
    break Telegram's parser and the message would not arrive at all."""
    bind(make_user(client, "A")["uid"], 111)
    client.post("/api/broadcast", json={"text": "<b>تخفیف</b> ۵۰٪"})
    assert "&lt;b&gt;" in outbox[0][1]


def test_one_blocked_customer_does_not_stop_the_rest(client, outbox):
    """Someone who blocked the bot must not cost everyone else their notice."""
    bind(make_user(client, "A")["uid"], "blocked")
    bind(make_user(client, "B")["uid"], 222)
    bind(make_user(client, "C")["uid"], 333)

    body = client.post("/api/broadcast", json={"text": "hi"}).json()
    assert body["sent"] == 2
    assert body["failed"] == 1
    assert len(outbox) == 2


def test_an_empty_message_is_refused(client, outbox):
    bind(make_user(client, "A")["uid"], 111)
    for text in ("", "   ", "\n"):
        r = client.post("/api/broadcast", json={"text": text})
        assert r.status_code == 400
        assert r.json()["detail"] == "empty-message"
    assert outbox == []


def test_an_enormous_message_is_refused(client, outbox):
    bind(make_user(client, "A")["uid"], 111)
    r = client.post("/api/broadcast", json={"text": "x" * 4000})
    assert r.status_code == 400
    assert r.json()["detail"] == "message-too-long"


def test_broadcasting_to_nobody_is_refused(client, outbox):
    r = client.post("/api/broadcast", json={"text": "hi"})
    assert r.status_code == 400
    assert r.json()["detail"] == "no-audience"


def test_sending_needs_a_configured_bot(client, outbox):
    bind(make_user(client, "A")["uid"], 111)
    client.post("/api/settings", json={"userbot_token": ""})
    r = client.post("/api/broadcast", json={"text": "hi"})
    assert r.status_code == 400
    assert r.json()["detail"] == "not-configured"


# ------------------------------------------------------------------ log
def test_the_send_is_logged(client, outbox):
    bind(make_user(client, "A")["uid"], 111)
    client.post("/api/broadcast", json={"text": "اطلاعیه مهم"})

    last = client.get("/api/broadcast").json()["last"]
    assert last[0]["sent"] == 1
    assert "اطلاعیه" in last[0]["preview"]


def test_the_log_does_not_grow_without_bound(client, outbox):
    bind(make_user(client, "A")["uid"], 111)
    db = main.store.get_sync()
    db["broadcast_log"] = [{"at": 0, "sent": 0, "failed": 0, "preview": str(i)}
                           for i in range(60)]
    client.post("/api/broadcast", json={"text": "hi"})
    assert len(main.store.get_sync()["broadcast_log"]) <= 50


# ------------------------------------------------------------------ resellers
def test_a_reseller_reaches_only_its_own_customers(client, outbox):
    """The boundary that matters: a seller must not be able to message the
    owner's customers, or another seller's."""
    client.post("/api/resellers", json=SELLER)
    seller = login_as(SELLER)

    mine = make_user(client, "OwnersCustomer")
    theirs = seller.post("/api/inbounds",
                         json={"name": "SellersCustomer"}).json()["inbound"]
    bind(mine["uid"], 111)
    bind(theirs["uid"], 222)

    assert seller.get("/api/broadcast").json()["audience"] == 1
    body = seller.post("/api/broadcast", json={"text": "hi"}).json()
    assert body["sent"] == 1
    assert [chat for chat, _ in outbox] == ["222"]


def test_the_owner_reaches_everyone(client, outbox):
    client.post("/api/resellers", json=SELLER)
    seller = login_as(SELLER)
    bind(make_user(client, "Mine")["uid"], 111)
    bind(seller.post("/api/inbounds", json={"name": "Theirs"}).json()["inbound"]["uid"], 222)

    assert client.post("/api/broadcast", json={"text": "hi"}).json()["sent"] == 2


def test_broadcast_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.post("/api/broadcast", json={"text": "hi"}).status_code == 401
        assert c.get("/api/broadcast").status_code == 401


# ------------------------------------------------------------------ automatic
def test_a_dead_location_tells_customers_to_refresh(client, outbox):
    client.post("/api/settings", json={"broadcast_route_changes": True})
    bind(make_user(client, "Ali")["uid"], 111)

    client.portal.call(main._announce_route_change, ["Frankfurt"])
    assert len(outbox) == 1
    body = outbox[0][1]
    assert "به‌روزرسانی" in body
    assert "Ali" in body


def test_the_automatic_notice_is_off_unless_turned_on(client, outbox):
    """Customers should not start receiving mail because a probe blipped."""
    assert main.store.get_sync()["settings"].get("broadcast_route_changes") is False
    bind(make_user(client, "Ali")["uid"], 111)
    client.portal.call(main._announce_route_change, ["Frankfurt"])
    assert outbox == []


def test_an_expired_customer_is_not_told_to_refresh(client, outbox):
    """Telling someone whose subscription ran out to refresh their link is
    just confusing -- their problem is not the route."""
    client.post("/api/settings", json={"broadcast_route_changes": True})
    live = make_user(client, "Live")
    dead = make_user(client, "Expired")
    bind(live["uid"], 111)
    bind(dead["uid"], 222)
    main.inbound_by_uid(main.store.get_sync(), dead["uid"])["expire_at"] = 1

    client.portal.call(main._announce_route_change, ["Frankfurt"])
    assert [chat for chat, _ in outbox] == ["111"]


def test_the_automatic_notice_is_logged(client, outbox):
    client.post("/api/settings", json={"broadcast_route_changes": True})
    bind(make_user(client, "Ali")["uid"], 111)
    client.portal.call(main._announce_route_change, ["Frankfurt"])

    last = client.get("/api/broadcast").json()["last"]
    assert last[0]["auto"] is True
    assert "Frankfurt" in last[0]["preview"]


def test_nothing_is_sent_when_no_one_is_bound(client, outbox):
    client.post("/api/settings", json={"broadcast_route_changes": True})
    make_user(client, "NeverBound")
    client.portal.call(main._announce_route_change, ["Frankfurt"])
    assert outbox == []


# ------------------------------------------------------------------ copy
def test_the_route_notice_does_not_claim_things_are_broken():
    """Most customers carry several routes and will not have noticed a thing;
    alarming them invites a support message that was not needed."""
    text = userbot.format_route_changed("Peyk", "Ali", True)
    assert "به‌روزرسانی" in text
    for alarming in ("قطع شد", "کار نمی‌کند", "خراب"):
        assert alarming not in text


def test_customer_names_are_escaped_in_the_notice():
    text = userbot.format_route_changed("Peyk", "<script>", True)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_migration_creates_the_log():
    import storage
    db = {"schema_version": 19,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64, "inbounds": []}
    storage.normalize_db(db)
    assert db["broadcast_log"] == []
