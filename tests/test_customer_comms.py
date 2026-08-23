"""
Messages that reach the customer rather than the owner.

Two features share this file because they share one risk: both talk to people
who are not the admin. A bug here does not annoy the seller, it annoys the
seller's paying customers, so the refusals get more attention than the happy
paths.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import userbot

ADMIN = {"username": "ccadmin", "password": "correct horse battery"}
GB = 1024 ** 3


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
    db["trial_claims"] = {}
    db["alerts_sent"] = {}
    for ib in client.get("/api/inbounds").json()["inbounds"]:
        client.delete(f"/api/inbounds/{ib['uid']}")
    _r = client.post("/api/settings", json={
        "notify_customer_enabled": True,
        "userbot_enabled": True,
        "userbot_token": "123456789:AAFakeTokenForTestsOnly_0123456789",
        "trial_enabled": True,
        "trial_selfserve_enabled": True,
        "trial_gb": 1,
        "trial_days": 1,
    })
    assert _r.status_code == 200, _r.text
    yield


def make_user(client, name="Ali", **kw):
    payload = {"name": name, "quota_gb": 10}
    payload.update(kw)
    return client.post("/api/inbounds", json=payload).json()["inbound"]


def bind(uid, chat="777"):
    main.store.get_sync().setdefault("bot_bindings", {})[str(chat)] = uid


def burn(uid, gb):
    main.inbound_by_uid(main.store.get_sync(), uid)["used_down"] = int(gb * GB)


def scan(client):
    return client.portal.call(main._scan_customer_nudges, main.store.get_sync())


# ------------------------------------------------------------------ reminders
def test_a_customer_near_their_quota_is_warned(client):
    ib = make_user(client)
    bind(ib["uid"])
    burn(ib["uid"], 9)          # 90% of 10 GB, past the default 80

    due = scan(client)
    assert [d[1] for d in due] == ["cust-quota"]
    assert "۹۰" in due[0][3] or "90" in due[0][3]
    assert due[0][2] == "777"


def test_a_customer_well_inside_their_quota_is_left_alone(client):
    ib = make_user(client)
    bind(ib["uid"])
    burn(ib["uid"], 2)
    assert scan(client) == []


def test_an_expiring_customer_is_warned(client):
    ib = make_user(client, expire_days=2)
    bind(ib["uid"])
    due = scan(client)
    assert [d[1] for d in due] == ["cust-expiry"]


def test_nobody_is_messaged_without_a_binding(client):
    """The only people reachable are those who ran /bind themselves."""
    ib = make_user(client)
    burn(ib["uid"], 9)
    assert scan(client) == []


def test_the_feature_is_off_by_default(client):
    client.post("/api/settings", json={"notify_customer_enabled": False})
    ib = make_user(client)
    bind(ib["uid"])
    burn(ib["uid"], 9)
    assert scan(client) == []


def test_nothing_is_sent_without_a_bot_token(client):
    client.post("/api/settings", json={"userbot_token": ""})
    ib = make_user(client)
    bind(ib["uid"])
    burn(ib["uid"], 9)
    assert scan(client) == []


def test_the_customer_cooldown_is_separate_from_the_owners(client):
    """Silencing one must not silence the other, or the seller loses their
    own alerts by turning on customer reminders."""
    ib = make_user(client)
    bind(ib["uid"])
    burn(ib["uid"], 9)

    main.store.get_sync()["alerts_sent"]["%s:quota" % ib["uid"]] = 9e12
    assert [d[1] for d in scan(client)] == ["cust-quota"]      # still due


def test_a_customer_is_not_nagged_twice(client):
    ib = make_user(client)
    bind(ib["uid"])
    burn(ib["uid"], 9)
    assert len(scan(client)) == 1

    main.store.get_sync()["alerts_sent"]["%s:cust-quota" % ib["uid"]] = 9e12
    assert scan(client) == []


def test_the_message_mentions_renew_when_it_is_available(client):
    client.post("/api/settings", json={"userbot_renew_enabled": True})
    ib = make_user(client)
    bind(ib["uid"])
    burn(ib["uid"], 9)
    assert "/renew" in scan(client)[0][3]


def test_the_message_says_contact_support_when_renew_is_off(client):
    client.post("/api/settings", json={"userbot_renew_enabled": False})
    ib = make_user(client)
    bind(ib["uid"])
    burn(ib["uid"], 9)
    text = scan(client)[0][3]
    assert "/renew" not in text
    assert "پشتیبانی" in text


def test_a_customer_name_cannot_inject_markup(client):
    ib = make_user(client, name="<b>evil</b>")
    bind(ib["uid"])
    burn(ib["uid"], 9)
    text = scan(client)[0][3]
    assert "<b>evil</b>" not in text
    assert "&lt;b&gt;evil&lt;/b&gt;" in text


# ------------------------------------------------------------------ trials
def claim(client, chat="555"):
    try:
        return client.portal.call(main.claim_trial, chat)
    except main.TrialError as e:
        return {"error": e.reason}


def test_a_customer_can_claim_one_trial(client):
    ib = claim(client)
    assert ib["is_trial"] is True
    assert ib["quota_gb"] == 1
    assert ib["max_connections"] == 1


def test_the_trial_binds_itself_to_the_chat(client):
    """So /status and /config work straight away with no second step."""
    ib = claim(client)
    assert main.store.get_sync()["bot_bindings"]["555"] == ib["uid"]


def test_a_second_claim_is_refused(client):
    claim(client)
    assert claim(client)["error"] == "already-claimed"
    assert len([i for i in main.store.get_sync()["inbounds"] if i.get("is_trial")]) == 1


def test_the_claim_outlives_the_trial(client):
    """Retention deletes trials a day after expiry. If the claim went with it,
    the same person could take a fresh trial every other day forever."""
    ib = claim(client)
    db = main.store.get_sync()
    db["inbounds"] = [i for i in db["inbounds"] if i["uid"] != ib["uid"]]
    db["bot_bindings"].pop("555", None)

    assert claim(client)["error"] == "already-claimed"


def test_someone_with_a_live_subscription_cannot_take_a_trial(client):
    """Otherwise a paying customer quietly collects a second free account."""
    ib = make_user(client)
    bind(ib["uid"], "555")
    assert claim(client)["error"] == "already-subscribed"


def test_a_stale_binding_does_not_block_a_trial(client):
    """Their old subscription was deleted, so they are a prospect again --
    the claim record is what stops repeats, not the binding."""
    main.store.get_sync()["bot_bindings"]["555"] = "deadbeefdeadbeef"
    assert "error" not in claim(client)


def test_self_serve_can_be_switched_off(client):
    client.post("/api/settings", json={"trial_selfserve_enabled": False})
    assert claim(client)["error"] == "disabled"


def test_turning_off_trials_entirely_also_stops_self_serve(client):
    client.post("/api/settings", json={"trial_enabled": False})
    assert claim(client)["error"] == "disabled"


def test_the_admin_button_still_works_when_self_serve_is_off(client):
    client.post("/api/settings", json={"trial_selfserve_enabled": False})
    assert client.post("/api/inbounds/trial").status_code == 200


def test_self_serve_trials_belong_to_the_owner(client):
    """A reseller's quota should not be spent by strangers finding the bot."""
    assert claim(client)["owner"] is None


# ------------------------------------------------------------------ the bot
class _Ctx:
    panel_name = "Peyk"
    trial_enabled = True

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def claim_trial(self, chat):
        self.calls.append(chat)
        return self.outcome

    def bound_uid(self, chat):
        return None


def msg(text, chat=42):
    return {"chat": {"id": chat}, "text": text}


@pytest.mark.anyio
async def test_the_bot_hands_over_the_trial_config(anyio_backend):
    ctx = _Ctx({"inbound": {"name": "trial-001"}, "sub_url": "https://e.com/sub/t",
                "configs": [{"label": "Main", "link": "vless://abc"}]})
    reply = await userbot.handle_message(msg("/trial"), ctx)
    assert "vless://abc" in reply
    assert ctx.calls == [42]


@pytest.mark.anyio
async def test_the_bot_explains_a_repeat_claim(anyio_backend):
    reply = await userbot.handle_message(msg("/trial"), _Ctx({"error": "already-claimed"}))
    assert "قبلاً" in reply


@pytest.mark.anyio
async def test_the_bot_points_an_existing_customer_at_status(anyio_backend):
    reply = await userbot.handle_message(msg("/trial"),
                                         _Ctx({"error": "already-subscribed"}))
    assert "/status" in reply


@pytest.mark.anyio
async def test_the_bot_refuses_while_self_serve_is_off(anyio_backend):
    ctx = _Ctx({})
    ctx.trial_enabled = False
    reply = await userbot.handle_message(msg("/trial"), ctx)
    assert "فعال نیست" in reply
    assert ctx.calls == []


def test_trial_is_listed_in_the_help():
    assert "/trial" in userbot.HELP


# ------------------------------------------------------------------ migration
def test_upgrade_creates_the_claims_table():
    import storage
    db = {"schema_version": 15,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64, "inbounds": []}
    storage.normalize_db(db)
    assert db["trial_claims"] == {}
