"""
Selling from inside the bot.

The flow is: pick a plan, get payment details, send a receipt photo, and wait
for the seller to tap approve. The parts that get the most attention here are
the ones where money or access is at stake -- who is allowed to press the
approve button, and whether one receipt can turn into two accounts.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import userbot


def reply_text(reply):
    """The words of a reply, without the keyboard wrapped around it.

    handle_message returns {text, keyboard} so no branch can forget the menu;
    these assertions are about the words, so they unwrap here.
    """
    if reply is None:
        return None
    return reply["text"] if isinstance(reply, dict) else reply




ADMIN = {"username": "shopadmin", "password": "correct horse battery"}
USER_TOKEN = "123456789:AAFakeUserBotTokenForTests_012345678"
ALERT_TOKEN = "987654321:AAFakeAlertBotTokenForTests_012345"
ADMIN_CHAT = "5000"
CUSTOMER_CHAT = "777"


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """Every bot call in this file would otherwise hit api.telegram.org.

    That made the suite both slow and dependent on the network, and none of
    these tests are about whether Telegram answers -- they are about what the
    panel decides before it calls out.
    """
    calls = []

    async def fake_call(token, method, **params):
        calls.append((method, params))
        return {"message_id": 4242}

    async def fake_photo(token, chat_id, blob, caption, keyboard=None):
        calls.append(("sendPhoto", {"chat_id": chat_id, "bytes": len(blob)}))
        return {"message_id": 4242}

    async def fake_download(token, file_id, max_bytes):
        calls.append(("getFile", {"file_id": file_id}))
        return b"\xff\xd8\xff fake jpeg"

    monkeypatch.setattr(userbot, "_call", fake_call)
    monkeypatch.setattr(userbot, "send_photo_bytes", fake_photo)
    monkeypatch.setattr(userbot, "download_file", fake_download)
    return calls


@pytest.fixture(autouse=True)
def clean(client):
    if client.get("/api/setup-status").json().get("needs_setup"):
        client.post("/api/setup", json=ADMIN)
    client.post("/api/login", json=ADMIN)
    db = main.store.get_sync()
    db["orders"] = {}
    db["shop_selections"] = {}
    db["bot_bindings"] = {}
    db["sales"] = []
    for pl in list(db.get("plans", [])):
        client.delete(f"/api/plans/{pl['id']}")
    for ib in client.get("/api/inbounds").json()["inbounds"]:
        client.delete(f"/api/inbounds/{ib['uid']}")
    r = client.post("/api/settings", json={
        "shop_enabled": True,
        "shop_instructions": "کارت ۶۰۳۷-۹۹۱۱ به نام آرمین",
        "userbot_enabled": True,
        "userbot_token": USER_TOKEN,
        "telegram_bot_token": ALERT_TOKEN,
        "telegram_chat_id": ADMIN_CHAT,
        "currency": "تومان",
    })
    assert r.status_code == 200, r.text
    yield


def make_plan(client, name="یک‌ماهه", price=150000, **kw):
    body = {"name": name, "days": 30, "quota_gb": 50, "price": price}
    body.update(kw)
    return client.post("/api/plans", json=body).json()["plan"]


def ctx_for(client):
    return main._BotContext(main.store.get_sync(), "https://peyk.example.com")


def order_for(chat=CUSTOMER_CHAT, **kw):
    """Put a pending order straight into the store."""
    db = main.store.get_sync()
    oid = kw.pop("oid", "ord12345")
    row = {"chat": chat, "plan_id": kw.get("plan_id"), "plan_name": "یک‌ماهه",
           "price": 150000, "file_id": "f1", "created_at": 1.0,
           "status": "pending", "admin_message_id": 42}
    row.update(kw)
    db.setdefault("orders", {})[oid] = row
    return oid


def callback(data, chat=ADMIN_CHAT, message_id=42):
    return {"id": "cb1", "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}}}


# ------------------------------------------------------------------ listing
def test_only_priced_plans_are_for_sale(client):
    make_plan(client, name="فروشی", price=150000)
    make_plan(client, name="داخلی", price=0)
    names = [p["name"] for p in ctx_for(client).shop_plans()]
    assert names == ["فروشی"]


def test_the_shop_message_lists_price_and_size(client):
    make_plan(client, price=150000)
    plans = ctx_for(client).shop_plans()
    text = userbot.format_shop("Peyk", plans)
    assert "150,000" in text
    assert "50 گیگ" in text
    assert "30 روزه" in text


def test_unlimited_reads_as_unlimited(client):
    make_plan(client, quota_gb=0, days=0, price=99000)
    text = userbot.format_shop("Peyk", ctx_for(client).shop_plans())
    assert "نامحدود" in text
    assert "بدون انقضا" in text


def test_the_keyboard_carries_the_plan_id(client):
    plan = make_plan(client)
    kb = userbot.shop_keyboard(ctx_for(client).shop_plans())
    assert kb[0][0]["callback_data"] == f"buy:{plan['id']}"


def test_a_plan_name_cannot_inject_markup(client):
    make_plan(client, name="<b>x</b>")
    text = userbot.format_shop("Peyk", ctx_for(client).shop_plans())
    assert "<b>x</b>" not in text


# ------------------------------------------------------------------ payment
def test_payment_details_include_the_sellers_instructions(client):
    make_plan(client, price=150000)
    plan = ctx_for(client).shop_plans()[0]
    text = userbot.format_payment("Peyk", plan, "کارت ۶۰۳۷")
    assert "کارت ۶۰۳۷" in text
    assert "150,000" in text
    assert "رسید" in text


# ------------------------------------------------------------------ receipts
@pytest.mark.anyio
async def test_a_receipt_without_a_chosen_plan_is_refused(client, anyio_backend):
    ctx = ctx_for(client)
    assert (await ctx.submit_receipt(CUSTOMER_CHAT, "f1"))["error"] == "no-order"


@pytest.mark.anyio
async def test_a_receipt_after_choosing_a_plan_is_accepted(client, anyio_backend):
    plan = make_plan(client)
    ctx = ctx_for(client)
    ctx.select_plan(CUSTOMER_CHAT, plan["id"])
    assert (await ctx.submit_receipt(CUSTOMER_CHAT, "f1")).get("ok") is True
    assert ctx.pending_orders[CUSTOMER_CHAT]["plan_id"] == plan["id"]


@pytest.mark.anyio
async def test_a_second_receipt_while_one_is_pending_is_refused(client, anyio_backend):
    """Otherwise one customer can queue ten orders off a single payment."""
    plan = make_plan(client)
    order_for(plan_id=plan["id"])
    ctx = ctx_for(client)
    ctx.select_plan(CUSTOMER_CHAT, plan["id"])
    assert (await ctx.submit_receipt(CUSTOMER_CHAT, "f2"))["error"] == "pending"


def test_the_selection_survives_a_restart(client):
    """The chosen plan is stored, not just held in the worker's memory, or a
    redeploy between choosing and paying would lose the order."""
    plan = make_plan(client)
    main.store.get_sync().setdefault("shop_selections", {})[CUSTOMER_CHAT] = plan["id"]
    assert ctx_for(client).selected_plan(CUSTOMER_CHAT) == plan["id"]


def test_the_largest_photo_size_is_chosen():
    message = {"photo": [
        {"file_id": "small", "file_size": 500},
        {"file_id": "big", "file_size": 90000},
        {"file_id": "mid", "file_size": 9000},
    ]}
    assert userbot.largest_photo(message) == "big"


def test_a_message_without_a_photo_has_none():
    assert userbot.largest_photo({"text": "hello"}) is None
    assert userbot.largest_photo({"photo": []}) is None


# ------------------------------------------------------------------ approval
def test_approving_creates_the_account_and_records_the_sale(client):
    plan = make_plan(client, price=150000)
    oid = order_for(plan_id=plan["id"])

    client.portal.call(main._handle_order_callback, callback(f"ord:{oid}:ok"))

    inbounds = client.get("/api/inbounds").json()["inbounds"]
    assert len(inbounds) == 1
    assert inbounds[0]["quota_gb"] == 50
    sales = client.get("/api/sales").json()
    assert sales["total"] == 150000
    assert sales["recent"][0]["source"] == "shop"


def test_approving_binds_the_account_to_the_buyer(client):
    plan = make_plan(client)
    oid = order_for(plan_id=plan["id"])
    client.portal.call(main._handle_order_callback, callback(f"ord:{oid}:ok"))

    uid = client.get("/api/inbounds").json()["inbounds"][0]["uid"]
    assert main.store.get_sync()["bot_bindings"][CUSTOMER_CHAT] == uid


def test_an_order_can_only_be_approved_once(client):
    """A double tap on the button must not sell two accounts for one payment."""
    plan = make_plan(client)
    oid = order_for(plan_id=plan["id"])
    for _ in range(3):
        client.portal.call(main._handle_order_callback, callback(f"ord:{oid}:ok"))
    assert len(client.get("/api/inbounds").json()["inbounds"]) == 1
    assert client.get("/api/sales").json()["count"] == 1


def test_rejecting_creates_nothing(client):
    plan = make_plan(client)
    oid = order_for(plan_id=plan["id"])
    client.portal.call(main._handle_order_callback, callback(f"ord:{oid}:no"))

    assert client.get("/api/inbounds").json()["inbounds"] == []
    assert main.store.get_sync()["orders"][oid]["status"] == "rejected"


def test_a_rejected_order_cannot_then_be_approved(client):
    plan = make_plan(client)
    oid = order_for(plan_id=plan["id"])
    client.portal.call(main._handle_order_callback, callback(f"ord:{oid}:no"))
    client.portal.call(main._handle_order_callback, callback(f"ord:{oid}:ok"))
    assert client.get("/api/inbounds").json()["inbounds"] == []


def test_only_the_admin_chat_can_approve(client):
    """The dangerous one: a customer who guesses an order id must not be able
    to approve their own purchase."""
    plan = make_plan(client)
    oid = order_for(plan_id=plan["id"])

    client.portal.call(main._handle_order_callback,
                       callback(f"ord:{oid}:ok", chat=CUSTOMER_CHAT))

    assert client.get("/api/inbounds").json()["inbounds"] == []
    assert main.store.get_sync()["orders"][oid]["status"] == "pending"


def test_an_unknown_order_is_ignored(client):
    client.portal.call(main._handle_order_callback, callback("ord:nope:ok"))
    assert client.get("/api/inbounds").json()["inbounds"] == []


def test_a_deleted_plan_does_not_create_a_broken_account(client):
    plan = make_plan(client)
    oid = order_for(plan_id=plan["id"])
    client.delete(f"/api/plans/{plan['id']}")

    client.portal.call(main._handle_order_callback, callback(f"ord:{oid}:ok"))
    assert client.get("/api/inbounds").json()["inbounds"] == []
    assert main.store.get_sync()["orders"][oid]["status"] == "pending"


@pytest.mark.parametrize("data,expected", [
    ("ord:abc:ok", ("abc", True)),
    ("ord:abc:no", ("abc", False)),
    ("ord:abc", (None, None)),
    ("rn:abc:30", (None, None)),
    ("", (None, None)),
    ("ord:a:b:c", (None, None)),
])
def test_order_callback_parsing(data, expected):
    assert userbot.parse_order_callback(data) == expected


# ------------------------------------------------------------------ gating
@pytest.mark.anyio
async def test_buy_is_refused_while_the_shop_is_off(client, anyio_backend):
    class Ctx:
        panel_name = "Peyk"
        shop_enabled = False

        def bound_uid(self, chat):
            return None

    reply = reply_text(await userbot.handle_message({"chat": {"id": 1}, "text": "/buy"}, Ctx()))
    assert "فعال نیست" in reply


@pytest.mark.anyio
async def test_buy_says_so_when_no_plan_is_priced(client, anyio_backend):
    sent = []

    class Ctx:
        panel_name = "Peyk"
        shop_enabled = True

        def has_open_order(self, chat):
            return False

        def shop_plans(self):
            return []

        async def send_shop(self, chat, text, kb):
            sent.append(text)

        def bound_uid(self, chat):
            return None

    reply = reply_text(await userbot.handle_message({"chat": {"id": 1}, "text": "/buy"}, Ctx()))
    assert "پلنی" in reply
    assert sent == []


@pytest.mark.anyio
async def test_a_photo_is_ignored_while_the_shop_is_off(client, anyio_backend):
    class Ctx:
        panel_name = "Peyk"
        shop_enabled = False

        def bound_uid(self, chat):
            return None

    message = {"chat": {"id": 1}, "photo": [{"file_id": "x", "file_size": 10}]}
    assert await userbot.handle_message(message, Ctx()) is None


def test_buy_is_listed_in_the_help():
    assert "/buy" in userbot.HELP


# ------------------------------------------------------------------ housekeeping
def test_pending_orders_survive_pruning():
    """Trimming must never drop somebody who is still waiting for an answer."""
    orders = {}
    for i in range(main.MAX_ORDERS + 40):
        orders[f"o{i}"] = {"created_at": i,
                           "status": "approved" if i % 2 else "pending"}
    pending_before = sum(1 for v in orders.values() if v["status"] == "pending")

    main._prune_orders(orders)

    assert len(orders) <= main.MAX_ORDERS
    kept_pending = sum(1 for v in orders.values() if v["status"] == "pending")
    assert kept_pending == pending_before      # every waiting customer kept


def test_selections_are_capped():
    sel = {str(i): "plan" for i in range(main.MAX_SELECTIONS + 100)}
    main._prune_selections(sel)
    assert len(sel) == main.MAX_SELECTIONS


def test_upgrade_creates_the_orders_table():
    import storage
    db = {"schema_version": 16,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64, "inbounds": []}
    storage.normalize_db(db)
    assert db["orders"] == {}
