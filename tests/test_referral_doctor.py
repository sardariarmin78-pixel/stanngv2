"""
Referral bonuses and the configuration self-check.

The referral rules are the interesting half. A bonus scheme is a payout, and
anything that pays out gets farmed unless the conditions are exact, so most
of these tests are about the ways someone could collect twice.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import userbot

ADMIN = {"username": "refadmin", "password": "correct horse battery"}
USER_TOKEN = "123456789:AAFakeUserBotTokenForTests_012345678"
ALERT_TOKEN = "987654321:AAFakeAlertBotTokenForTests_012345"
ADMIN_CHAT = "5000"
DAY = 86400


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    async def fake_call(token, method, **params):
        return {"message_id": 1}

    async def fake_photo(token, chat_id, blob, caption, keyboard=None):
        return {"message_id": 1}

    async def fake_download(token, file_id, max_bytes):
        return b"jpeg"

    monkeypatch.setattr(userbot, "_call", fake_call)
    monkeypatch.setattr(userbot, "send_photo_bytes", fake_photo)
    monkeypatch.setattr(userbot, "download_file", fake_download)


@pytest.fixture(autouse=True)
def clean(client):
    if client.get("/api/setup-status").json().get("needs_setup"):
        client.post("/api/setup", json=ADMIN)
    client.post("/api/login", json=ADMIN)
    db = main.store.get_sync()
    db["referrals"] = {}
    db["bot_bindings"] = {}
    db["orders"] = {}
    db["vouchers"] = []
    db["sales"] = []
    db["bot_username"] = "PeykTestBot"
    for pl in list(db.get("plans", [])):
        client.delete(f"/api/plans/{pl['id']}")
    for ib in client.get("/api/inbounds").json()["inbounds"]:
        client.delete(f"/api/inbounds/{ib['uid']}")
    r = client.post("/api/settings", json={
        "referral_enabled": True,
        "referral_bonus_days": 7,
        "userbot_enabled": True,
        "userbot_token": USER_TOKEN,
        "telegram_bot_token": ALERT_TOKEN,
        "telegram_chat_id": ADMIN_CHAT,
        "voucher_redeem_enabled": True,
        "shop_enabled": True,
        "shop_instructions": "کارت ۶۰۳۷",
        "public_domain": "peyk.example.com",
    })
    assert r.status_code == 200, r.text
    yield


def make_plan(client, price=150000, days=30):
    return client.post("/api/plans", json={
        "name": "یک‌ماهه", "days": days, "quota_gb": 50, "price": price}).json()["plan"]


def make_user(client, name="Referrer", **kw):
    payload = {"name": name, "quota_gb": 10, "expire_days": 30}
    payload.update(kw)
    return client.post("/api/inbounds", json=payload).json()["inbound"]


def expiry_of(uid):
    return main.inbound_by_uid(main.store.get_sync(), uid)["expire_at"]


# ------------------------------------------------------------------ codes
def test_a_code_is_derived_from_the_uid(client):
    ib = make_user(client)
    code = main.referral_code_of(ib)
    assert code.startswith("ref_")
    assert code[4:] == ib["uid"][:12]


def test_rotating_the_link_does_not_change_the_invite_code(client):
    """Someone who shared their invite must not have it die because they
    rotated a leaked subscription URL."""
    ib = make_user(client)
    before = main.referral_code_of(ib)
    client.post(f"/api/inbounds/{ib['uid']}/rotate-link")
    after = main.referral_code_of(main.inbound_by_uid(main.store.get_sync(), ib["uid"]))
    assert before == after


def test_a_code_resolves_back_to_its_owner(client):
    ib = make_user(client)
    found = main.inbound_by_referral(main.store.get_sync(), main.referral_code_of(ib))
    assert found["uid"] == ib["uid"]


@pytest.mark.parametrize("code", ["", "ref_", "nonsense", "ref_zzzzzzzzzzzz", None])
def test_a_bad_code_resolves_to_nothing(client, code):
    make_user(client)
    assert main.inbound_by_referral(main.store.get_sync(), code) is None


# ------------------------------------------------------------------ recording
def test_an_invite_is_recorded_but_not_paid(client):
    """Opening a link is not a purchase, so nothing is granted yet."""
    ref = make_user(client)
    db = main.store.get_sync()
    assert main.record_referral(db, "999", main.referral_code_of(ref)) is True
    assert db["referrals"]["999"]["paid"] is False
    assert expiry_of(ref["uid"]) == ref["expire_at"]


def test_an_existing_customer_cannot_be_invited(client):
    """Otherwise a customer re-enters through a friend's link for free days."""
    ref = make_user(client)
    mine = make_user(client, "Existing")
    db = main.store.get_sync()
    db["bot_bindings"]["999"] = mine["uid"]
    assert main.record_referral(db, "999", main.referral_code_of(ref)) is False


def test_a_second_invite_does_not_replace_the_first(client):
    a = make_user(client, "A")
    b = make_user(client, "B")
    db = main.store.get_sync()
    main.record_referral(db, "999", main.referral_code_of(a))
    assert main.record_referral(db, "999", main.referral_code_of(b)) is False
    assert db["referrals"]["999"]["referrer_uid"] == a["uid"]


def test_nothing_is_recorded_while_the_feature_is_off(client):
    client.post("/api/settings", json={"referral_enabled": False})
    ref = make_user(client)
    assert main.record_referral(main.store.get_sync(), "999",
                                main.referral_code_of(ref)) is False


def test_a_zero_day_bonus_disables_it(client):
    client.post("/api/settings", json={"referral_bonus_days": 0})
    ref = make_user(client)
    assert main.record_referral(main.store.get_sync(), "999",
                                main.referral_code_of(ref)) is False


# ------------------------------------------------------------------ payout
def test_both_sides_are_paid_on_a_voucher_purchase(client):
    plan = make_plan(client)
    ref = make_user(client)
    before = expiry_of(ref["uid"])

    db = main.store.get_sync()
    main.record_referral(db, "999", main.referral_code_of(ref))
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]

    new = client.portal.call(main.redeem_voucher, code, "999")

    assert expiry_of(ref["uid"]) == pytest.approx(before + 7 * DAY, abs=5)
    # 30 day plan plus the 7 day bonus
    assert new["expire_at"] == pytest.approx(new["created_at"] + 37 * DAY, abs=5)


def test_the_payout_happens_once(client):
    plan = make_plan(client)
    ref = make_user(client)
    before = expiry_of(ref["uid"])
    db = main.store.get_sync()
    main.record_referral(db, "999", main.referral_code_of(ref))

    for _ in range(3):
        code = client.post("/api/vouchers",
                           json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]
        client.portal.call(main.redeem_voucher, code, "999")

    assert expiry_of(ref["uid"]) == pytest.approx(before + 7 * DAY, abs=5)


def test_a_trial_does_not_pay_out(client):
    """Trials are free, so paying for them would be a bonus mill."""
    client.post("/api/settings", json={"trial_enabled": True,
                                       "trial_selfserve_enabled": True})
    ref = make_user(client)
    before = expiry_of(ref["uid"])
    db = main.store.get_sync()
    main.record_referral(db, "888", main.referral_code_of(ref))

    client.portal.call(main.claim_trial, "888")

    assert expiry_of(ref["uid"]) == before
    assert main.store.get_sync()["referrals"]["888"]["paid"] is False


def test_a_shop_purchase_pays_out(client):
    plan = make_plan(client)
    ref = make_user(client)
    before = expiry_of(ref["uid"])
    db = main.store.get_sync()
    main.record_referral(db, "777", main.referral_code_of(ref))
    db.setdefault("orders", {})["ord1"] = {
        "chat": "777", "plan_id": plan["id"], "plan_name": "یک‌ماهه",
        "price": 150000, "file_id": "f", "created_at": 1.0,
        "status": "pending", "admin_message_id": 9}

    client.portal.call(main._handle_order_callback, {
        "id": "cb", "data": "ord:ord1:ok",
        "message": {"message_id": 9, "chat": {"id": ADMIN_CHAT}}})

    assert expiry_of(ref["uid"]) == pytest.approx(before + 7 * DAY, abs=5)


def test_a_deleted_referrer_still_pays_the_newcomer(client):
    """Their friend's account being gone is not the new customer's fault."""
    plan = make_plan(client)
    ref = make_user(client)
    db = main.store.get_sync()
    main.record_referral(db, "999", main.referral_code_of(ref))
    client.delete(f"/api/inbounds/{ref['uid']}")

    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]
    new = client.portal.call(main.redeem_voucher, code, "999")
    assert new["expire_at"] == pytest.approx(new["created_at"] + 37 * DAY, abs=5)


def test_a_purchase_without_an_invite_gets_no_bonus(client):
    plan = make_plan(client)
    code = client.post("/api/vouchers",
                       json={"count": 1, "plan_id": plan["id"]}).json()["vouchers"][0]["code"]
    new = client.portal.call(main.redeem_voucher, code, "999")
    assert new["expire_at"] == pytest.approx(new["created_at"] + 30 * DAY, abs=5)


def test_a_bonus_extends_rather_than_replaces(client):
    """Someone with 20 days left must end on 27, not 7."""
    ref = make_user(client, expire_days=20)
    row = main.inbound_by_uid(main.store.get_sync(), ref["uid"])
    before = row["expire_at"]
    main._extend(row, 7)
    assert row["expire_at"] == pytest.approx(before + 7 * DAY, abs=5)


def test_a_bonus_on_an_expired_account_counts_from_now(client):
    ref = make_user(client, expire_days=30)
    row = main.inbound_by_uid(main.store.get_sync(), ref["uid"])
    row["expire_at"] = 1000.0                 # long past
    main._extend(row, 7)
    import time as _t
    assert row["expire_at"] == pytest.approx(_t.time() + 7 * DAY, abs=5)


# ------------------------------------------------------------------ the bot
def test_invite_needs_a_subscription(client):
    class Ctx:
        panel_name = "Peyk"
        referral_enabled = True

        def bound_uid(self, chat):
            return None

    reply = client.portal.call(userbot.handle_message,
                               {"chat": {"id": 1}, "text": "/invite"}, Ctx())
    assert "/bind" in reply


def test_invite_returns_a_deep_link(client):
    ib = make_user(client)
    ctx = main._BotContext(main.store.get_sync(), "https://peyk.example.com")
    info = ctx.invite_info(ib["uid"])
    assert info["link"] == f"https://t.me/PeykTestBot?start={main.referral_code_of(ib)}"
    assert info["days"] == 7


def test_invite_survives_a_missing_bot_username(client):
    """The panel only learns the @name when the seller tests the connection."""
    main.store.get_sync()["bot_username"] = ""
    ib = make_user(client)
    ctx = main._BotContext(main.store.get_sync(), "https://peyk.example.com")
    assert ctx.invite_info(ib["uid"])["link"] == ""
    assert ctx.invite_info(ib["uid"])["code"].startswith("ref_")


def test_start_with_a_referral_payload_is_noticed(client):
    ref = make_user(client)
    ctx = main._BotContext(main.store.get_sync(), "https://peyk.example.com")
    assert ctx.note_referral("999", main.referral_code_of(ref)) is True
    assert ctx.pending_referrals["999"] == main.referral_code_of(ref)


def test_invite_is_listed_in_the_help():
    assert "/invite" in userbot.HELP


# ------------------------------------------------------------------ diagnostics
def codes(db):
    return {c["code"] for c in main.run_diagnostics(db)}


def test_a_missing_domain_is_an_error(client):
    db = main.store.get_sync()
    db["settings"]["public_domain"] = ""
    assert "no-public-domain" in codes(db)


def test_a_configured_panel_is_quiet_about_the_basics(client):
    db = main.store.get_sync()
    db["settings"]["public_domain"] = "peyk.example.com"
    assert "no-public-domain" not in codes(db)


def test_a_shop_with_no_priced_plan_is_an_error(client):
    db = main.store.get_sync()
    db["plans"] = []
    found = codes(db)
    assert "shop-without-plans" in found


def test_a_shop_without_payment_details_is_an_error(client):
    db = main.store.get_sync()
    db["settings"]["shop_instructions"] = ""
    assert "shop-without-instructions" in codes(db)


def test_bot_features_without_a_bot_are_an_error(client):
    """Exactly the class of silent misconfiguration this page exists for."""
    db = main.store.get_sync()
    db["settings"]["userbot_enabled"] = False
    db["settings"]["userbot_token"] = ""
    assert "bot-features-without-bot" in codes(db)


def test_referrals_without_a_known_bot_name_warn(client):
    db = main.store.get_sync()
    db["bot_username"] = ""
    assert "referral-without-username" in codes(db)


def test_a_missing_ota_repo_warns(client):
    db = main.store.get_sync()
    db["settings"]["ota_repo"] = ""
    assert "no-ota-repo" in codes(db)


def test_everything_reported_is_actually_a_problem(client):
    """Only failures are returned; a passing check is never listed."""
    db = main.store.get_sync()
    assert all(item["ok"] is False for item in main.run_diagnostics(db))


def test_the_endpoint_counts_by_severity(client):
    body = client.get("/api/diagnostics").json()
    assert body["errors"] == sum(1 for i in body["issues"] if i["level"] == "error")
    assert body["warnings"] == sum(1 for i in body["issues"] if i["level"] == "warn")


def test_diagnostics_are_owner_only(client):
    client.post("/api/resellers",
                json={"username": "diag_seller", "password": "seller-pass-1234"})
    seller = TestClient(main.app)
    seller.post("/api/login", json={"username": "diag_seller", "password": "seller-pass-1234"})
    assert seller.get("/api/diagnostics").status_code == 403


def test_diagnostics_never_leak_secrets(client):
    """The page names what is wrong, never the value of a token."""
    body = client.get("/api/diagnostics").text
    assert USER_TOKEN not in body
    assert ALERT_TOKEN not in body


# ------------------------------------------------------------------ migration
def test_upgrade_creates_the_referral_table():
    import storage
    db = {"schema_version": 17,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64, "inbounds": []}
    storage.normalize_db(db)
    assert db["referrals"] == {}
    assert db["bot_username"] == ""
