"""
Plan pricing, the sales ledger, and voucher codes.

Two properties carry this file. The ledger must not rewrite history when a
plan is edited, and a voucher must create exactly one account no matter how
many times it is redeemed at once.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main


def reply_text(reply):
    """The words of a reply, without the keyboard wrapped around it.

    handle_message returns {text, keyboard} so no branch can forget the menu;
    these assertions are about the words, so they unwrap here.
    """
    if reply is None:
        return None
    return reply["text"] if isinstance(reply, dict) else reply


ADMIN = {"username": "cmadmin", "password": "correct horse battery"}
SELLER = {"username": "cm_seller", "password": "seller-pass-1234"}


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
    db["sales"] = []
    db["vouchers"] = []
    for pl in list(db.get("plans", [])):
        client.delete(f"/api/plans/{pl['id']}")
    for ib in client.get("/api/inbounds").json()["inbounds"]:
        client.delete(f"/api/inbounds/{ib['uid']}")
    for r in client.get("/api/resellers").json()["resellers"]:
        client.delete(f"/api/resellers/{r['id']}?delete_users=1")
    client.post("/api/settings", json={"voucher_redeem_enabled": True})
    yield


def make_plan(client, name="30d", price=150000, **kw):
    body = {"name": name, "days": 30, "quota_gb": 50, "price": price}
    body.update(kw)
    r = client.post("/api/plans", json=body)
    assert r.status_code == 200, r.text
    return r.json()["plan"]


def login_as(creds):
    c = TestClient(main.app)
    assert c.post("/api/login", json=creds).status_code == 200
    return c


# ------------------------------------------------------------------ pricing
def test_a_plan_carries_a_price(client):
    plan = make_plan(client, price=150000)
    assert plan["price"] == 150000


def test_price_defaults_to_zero(client):
    r = client.post("/api/plans", json={"name": "free", "days": 1})
    assert r.json()["plan"]["price"] == 0


def test_a_negative_price_is_refused(client):
    assert client.post("/api/plans",
                       json={"name": "bad", "price": -5}).status_code == 400


# ------------------------------------------------------------------ ledger
def test_selling_from_a_plan_records_the_sale(client):
    plan = make_plan(client, price=150000)
    client.post("/api/inbounds", json={"name": "Buyer", "plan_id": plan["id"]})

    sales = client.get("/api/sales").json()
    assert sales["count"] == 1
    assert sales["total"] == 150000
    assert sales["recent"][0]["plan_name"] == "30d"


def test_a_free_plan_is_not_a_sale(client):
    """Zero price is how a plan opts out of the revenue report."""
    plan = make_plan(client, name="trial", price=0)
    client.post("/api/inbounds", json={"name": "Freebie", "plan_id": plan["id"]})
    assert client.get("/api/sales").json()["count"] == 0


def test_a_user_made_without_a_plan_is_not_a_sale(client):
    client.post("/api/inbounds", json={"name": "Manual", "quota_gb": 10})
    assert client.get("/api/sales").json()["count"] == 0


def test_editing_a_plan_does_not_rewrite_history(client):
    """The whole reason the price is copied into the ledger."""
    plan = make_plan(client, price=100000)
    client.post("/api/inbounds", json={"name": "Buyer", "plan_id": plan["id"]})

    client.patch(f"/api/plans/{plan['id']}", json={"name": "30d", "price": 900000})

    sales = client.get("/api/sales").json()
    assert sales["total"] == 100000
    assert sales["recent"][0]["price"] == 100000


def test_deleting_the_customer_does_not_erase_the_sale(client):
    """Money that came in is not undone by tidying up an account."""
    plan = make_plan(client, price=100000)
    uid = client.post("/api/inbounds",
                      json={"name": "Buyer", "plan_id": plan["id"]}).json()["inbound"]["uid"]
    client.delete(f"/api/inbounds/{uid}")
    assert client.get("/api/sales").json()["total"] == 100000


def test_bulk_creation_records_every_seat(client):
    plan = make_plan(client, price=50000)
    client.post("/api/inbounds/bulk",
                json={"count": 4, "prefix": "b", "plan_id": plan["id"]})

    sales = client.get("/api/sales").json()
    assert sales["count"] == 4
    assert sales["total"] == 200000


def test_the_report_splits_by_seller(client):
    plan = make_plan(client, price=100000)
    client.post("/api/resellers", json=SELLER)
    seller = login_as(SELLER)

    client.post("/api/inbounds", json={"name": "Mine", "plan_id": plan["id"]})
    seller.post("/api/inbounds", json={"name": "Theirs", "plan_id": plan["id"]})

    report = client.get("/api/sales").json()
    assert report["total"] == 200000
    rows = {r["seller"]: r["total"] for r in report["by_seller"]}
    assert rows[None] == 100000                    # the owner's own sale
    assert rows[SELLER["username"]] == 100000


def test_a_reseller_sees_only_its_own_revenue(client):
    plan = make_plan(client, price=100000)
    client.post("/api/resellers", json=SELLER)
    seller = login_as(SELLER)

    client.post("/api/inbounds", json={"name": "Mine", "plan_id": plan["id"]})
    seller.post("/api/inbounds", json={"name": "Theirs", "plan_id": plan["id"]})

    report = seller.get("/api/sales").json()
    assert report["total"] == 100000
    assert report["count"] == 1


def test_the_window_excludes_older_sales(client):
    plan = make_plan(client, price=100000)
    client.post("/api/inbounds", json={"name": "Old", "plan_id": plan["id"]})
    main.store.get_sync()["sales"][0]["at"] -= 86400 * 60

    assert client.get("/api/sales?days=30").json()["count"] == 0
    assert client.get("/api/sales?days=90").json()["count"] == 1


def test_a_garbage_window_falls_back(client):
    assert client.get("/api/sales?days=abc").json()["days"] == 30
    assert client.get("/api/sales?days=99999").json()["days"] == 365


# ------------------------------------------------------------------ codes
def test_codes_avoid_the_characters_people_misread():
    """I/O/0/1 are the pairs that get typed wrong off a screen."""
    for _ in range(50):
        code = main.gen_voucher_code()
        assert not set("IO01") & set(code.replace("-", ""))
        assert len(code) == 14      # 12 characters plus two dashes


def test_a_typed_code_is_forgiving():
    canonical = "ABCD-EFGH-JKLM"
    for typed in ("abcd-efgh-jklm", "ABCDEFGHJKLM", "abcd efgh jklm",
                  " ABCD-EFGH-JKLM "):
        assert main.normalize_code(typed) == canonical


def test_a_wrong_length_code_is_rejected():
    assert main.normalize_code("ABC") == ""
    assert main.normalize_code("") == ""
    assert main.normalize_code(None) == ""


# ------------------------------------------------------------------ vouchers
def test_minting_a_batch(client):
    plan = make_plan(client, price=150000)
    r = client.post("/api/vouchers", json={"count": 5, "plan_id": plan["id"]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["vouchers"]) == 5
    assert len({v["code"] for v in body["vouchers"]}) == 5     # all distinct
    assert body["vouchers"][0]["price"] == 150000


def test_minting_needs_a_real_plan(client):
    assert client.post("/api/vouchers",
                       json={"count": 1, "plan_id": "nope"}).status_code == 404


def test_redeeming_creates_the_account(client):
    plan = make_plan(client, price=150000, days=30, quota_gb=50)
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]

    ib = client.post(f"/api/vouchers/{code}/redeem").json()["inbound"]
    assert ib["quota_gb"] == 50
    assert ib["expire_days"] == 30
    assert ib["enabled"] is True


def test_redeeming_records_the_sale(client):
    plan = make_plan(client, price=150000)
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]
    client.post(f"/api/vouchers/{code}/redeem")

    sales = client.get("/api/sales").json()
    assert sales["total"] == 150000
    assert sales["recent"][0]["source"] == "voucher"


def test_a_code_works_exactly_once(client):
    plan = make_plan(client)
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]

    assert client.post(f"/api/vouchers/{code}/redeem").status_code == 200
    second = client.post(f"/api/vouchers/{code}/redeem")
    assert second.status_code == 400
    assert second.json()["detail"] == "already-used"
    assert len(client.get("/api/inbounds").json()["inbounds"]) == 1


def test_simultaneous_redemption_creates_one_account(client):
    """The race that matters: a customer double-taps, or shares the code with
    a friend who tries it the same second."""
    plan = make_plan(client)
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]

    async def race():
        return await asyncio.gather(
            *[main.redeem_voucher(code) for _ in range(8)],
            return_exceptions=True,
        )

    results = client.portal.call(race)
    ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, main.VoucherError)]

    assert len(ok) == 1
    assert len(refused) == 7
    assert all(r.reason == "already-used" for r in refused)
    assert len(client.get("/api/inbounds").json()["inbounds"]) == 1


def test_an_unknown_code_is_refused(client):
    r = client.post("/api/vouchers/ABCD-EFGH-JKLM/redeem")
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid-code"


def test_a_malformed_code_is_refused(client):
    assert client.post("/api/vouchers/xyz/redeem").json()["detail"] == "invalid-code"


def test_an_unused_code_can_be_revoked(client):
    plan = make_plan(client)
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]

    assert client.delete(f"/api/vouchers/{code}").status_code == 200
    assert client.post(f"/api/vouchers/{code}/redeem").json()["detail"] == "invalid-code"


def test_a_used_code_cannot_be_revoked(client):
    """It is a sales record at that point, not a pending promise."""
    plan = make_plan(client)
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]
    client.post(f"/api/vouchers/{code}/redeem")

    r = client.delete(f"/api/vouchers/{code}")
    assert r.status_code == 400
    assert r.json()["detail"] == "voucher-already-used"


# ------------------------------------------------------------------ resellers
def test_a_resellers_voucher_creates_a_user_it_owns(client):
    plan = make_plan(client, price=100000)
    client.post("/api/resellers", json=SELLER)
    seller = login_as(SELLER)

    code = seller.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]
    client.post(f"/api/vouchers/{code}/redeem")      # redeemed by anyone

    # the customer belongs to the seller, not to whoever redeemed it
    assert len(seller.get("/api/inbounds").json()["inbounds"]) == 1
    assert seller.get("/api/sales").json()["total"] == 100000


def test_a_reseller_sees_only_its_own_codes(client):
    plan = make_plan(client)
    client.post("/api/resellers", json=SELLER)
    seller = login_as(SELLER)

    client.post("/api/vouchers", json={"count": 2, "plan_id": plan["id"]})
    seller.post("/api/vouchers", json={"count": 3, "plan_id": plan["id"]})

    assert len(seller.get("/api/vouchers").json()["vouchers"]) == 3
    assert len(client.get("/api/vouchers").json()["vouchers"]) == 5


def test_a_reseller_cannot_revoke_someone_elses_code(client):
    plan = make_plan(client)
    client.post("/api/resellers", json=SELLER)
    seller = login_as(SELLER)
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]

    assert seller.delete(f"/api/vouchers/{code}").status_code == 404


def test_unused_codes_count_against_the_quota(client):
    """Otherwise a seller mints a hundred codes on a five-user quota and
    ninety-five customers hit a wall at redemption."""
    plan = make_plan(client)
    client.post("/api/resellers", json=dict(SELLER, max_users=5))
    seller = login_as(SELLER)

    assert seller.post("/api/vouchers",
                       json={"count": 5, "plan_id": plan["id"]}).status_code == 200
    r = seller.post("/api/vouchers", json={"count": 1, "plan_id": plan["id"]})
    assert r.status_code == 403
    assert r.json()["detail"] == "reseller-user-limit"


def test_the_owner_is_not_quota_limited(client):
    plan = make_plan(client)
    assert client.post("/api/vouchers",
                       json={"count": 50, "plan_id": plan["id"]}).status_code == 200


# ------------------------------------------------------------------ the bot
class _Ctx:
    """Just enough of the bot context to drive the command."""

    panel_name = "Peyk"
    voucher_enabled = True

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def redeem(self, chat, code):
        self.calls.append((chat, code))
        return self.outcome

    def bound_uid(self, chat):
        return None


def msg(text, chat=42):
    return {"chat": {"id": chat}, "text": text}


@pytest.mark.anyio
async def test_the_bot_hands_back_a_config_on_success(anyio_backend):
    import userbot


    ctx = _Ctx({"inbound": {"name": "x"}, "sub_url": "https://e.com/sub/t",
                "configs": [{"label": "Main", "link": "vless://abc"}]})
    reply = reply_text(await userbot.handle_message(msg("/redeem ABCD-EFGH-JKLM"), ctx))

    assert "فعال شد" in reply
    assert "vless://abc" in reply
    assert ctx.calls == [(42, "ABCD-EFGH-JKLM")]


@pytest.mark.anyio
async def test_the_bot_explains_a_used_code(anyio_backend):
    import userbot
    reply = reply_text(await userbot.handle_message(
        msg("/redeem ABCD-EFGH-JKLM"), _Ctx({"error": "already-used"})))
    assert "قبلاً استفاده شده" in reply


@pytest.mark.anyio
async def test_the_bot_asks_for_the_code_when_missing(anyio_backend):
    import userbot
    reply = reply_text(await userbot.handle_message(msg("/redeem"), _Ctx({})))
    assert "/redeem" in reply


@pytest.mark.anyio
async def test_the_bot_refuses_while_the_feature_is_off(anyio_backend):
    import userbot
    ctx = _Ctx({})
    ctx.voucher_enabled = False
    reply = reply_text(await userbot.handle_message(msg("/redeem ABCD-EFGH-JKLM"), ctx))
    assert "فعال نیست" in reply
    assert ctx.calls == []


def test_redeem_is_listed_in_the_help():
    import userbot
    assert "/redeem" in userbot.HELP


# ------------------------------------------------------------------ migration
def test_upgrade_creates_the_new_tables():
    import storage
    db = {"schema_version": 13,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64, "inbounds": []}
    storage.normalize_db(db)
    assert db["sales"] == []
    assert db["vouchers"] == []
