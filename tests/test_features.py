"""
Tests for the 1.6 feature set: plans, bulk operations, per-user traffic
history, TOTP two-factor auth, the login log, and Telegram alert scheduling.

Nothing here touches the network: the Telegram tests exercise the alert
*decision* logic, which is deliberately separated from delivery.
"""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import notify
import totp

ADMIN = {"username": "featadmin", "password": "correct horse battery"}


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


def make_plan(client, **kw):
    payload = {"name": "30d/50g", "days": 30, "quota_gb": 50, "max_connections": 2}
    payload.update(kw)
    r = client.post("/api/plans", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["plan"]


# ------------------------------------------------------------------ plans
def test_plan_crud(client):
    plan = make_plan(client, name="Starter")
    assert plan["quota_gb"] == 50
    assert plan["id"]

    listed = client.get("/api/plans").json()["plans"]
    assert any(p["id"] == plan["id"] for p in listed)

    r = client.patch(f"/api/plans/{plan['id']}", json={"name": "Starter+", "days": 60,
                                                      "quota_gb": 100, "max_connections": 3})
    assert r.json()["plan"]["days"] == 60

    assert client.delete(f"/api/plans/{plan['id']}").status_code == 200
    assert client.delete(f"/api/plans/{plan['id']}").status_code == 404


def test_plan_validation(client):
    assert client.post("/api/plans", json={"name": ""}).status_code == 400
    assert client.post("/api/plans", json={"name": "x", "quota_gb": "abc"}).status_code == 400
    assert client.post("/api/plans", json={"name": "x", "days": 99999}).status_code == 400


# ------------------------------------------------------------------ bulk create
def test_bulk_create_from_plan(client):
    plan = make_plan(client, name="Bulk", days=14, quota_gb=25, max_connections=1)
    r = client.post("/api/inbounds/bulk", json={
        "count": 5, "prefix": "cust", "plan_id": plan["id"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 5
    names = [i["name"] for i in body["inbounds"]]
    # Zero-padded so names sort correctly in the table.
    assert names == ["cust-1", "cust-2", "cust-3", "cust-4", "cust-5"]
    for ib in body["inbounds"]:
        assert ib["quota_gb"] == 25
        assert ib["max_connections"] == 1
        assert ib["status"]["days_left"] in (13, 14)
        assert ib["uuid"]
    # every user gets a distinct uuid and uid
    assert len({i["uuid"] for i in body["inbounds"]}) == 5
    assert len({i["uid"] for i in body["inbounds"]}) == 5


def test_bulk_create_pads_names(client):
    r = client.post("/api/inbounds/bulk", json={"count": 12, "prefix": "pad", "quota_gb": 1})
    names = [i["name"] for i in r.json()["inbounds"]]
    assert names[0] == "pad-01"
    assert names[-1] == "pad-12"


def test_bulk_create_start_index(client):
    r = client.post("/api/inbounds/bulk", json={
        "count": 3, "prefix": "seq", "start_index": 100, "quota_gb": 1})
    assert [i["name"] for i in r.json()["inbounds"]] == ["seq-100", "seq-101", "seq-102"]


def test_bulk_create_limits(client):
    assert client.post("/api/inbounds/bulk", json={"count": 0}).status_code == 400
    assert client.post("/api/inbounds/bulk", json={"count": 10_000}).status_code == 400
    assert client.post("/api/inbounds/bulk",
                       json={"count": 2, "plan_id": "nope"}).status_code == 404


# ------------------------------------------------------------------ bulk actions
def test_bulk_action_disable_and_enable(client):
    created = client.post("/api/inbounds/bulk", json={
        "count": 3, "prefix": "sw", "quota_gb": 5}).json()["inbounds"]
    uids = [i["uid"] for i in created]

    r = client.post("/api/inbounds/bulk-action", json={"action": "disable", "uids": uids})
    assert r.json()["affected"] == 3
    rows = {i["uid"]: i for i in client.get("/api/inbounds").json()["inbounds"]}
    assert all(rows[u]["status"]["live_enabled"] is False for u in uids)

    client.post("/api/inbounds/bulk-action", json={"action": "enable", "uids": uids})
    rows = {i["uid"]: i for i in client.get("/api/inbounds").json()["inbounds"]}
    assert all(rows[u]["status"]["live_enabled"] is True for u in uids)


def test_bulk_action_renew_and_reset(client):
    created = client.post("/api/inbounds/bulk", json={
        "count": 2, "prefix": "rn", "quota_gb": 5, "expire_days": 3}).json()["inbounds"]
    uids = [i["uid"] for i in created]

    for uid in uids:
        rec = main.inbound_by_uid(main.store.get_sync(), uid)
        rec["used_down"] = 1234

    client.post("/api/inbounds/bulk-action", json={"action": "renew", "uids": uids, "days": 30})
    client.post("/api/inbounds/bulk-action", json={"action": "reset-usage", "uids": uids})

    rows = {i["uid"]: i for i in client.get("/api/inbounds").json()["inbounds"]}
    for uid in uids:
        assert rows[uid]["status"]["days_left"] >= 32
        assert rows[uid]["used_down"] == 0


def test_bulk_action_delete(client):
    created = client.post("/api/inbounds/bulk", json={
        "count": 4, "prefix": "del", "quota_gb": 1}).json()["inbounds"]
    uids = [i["uid"] for i in created]
    r = client.post("/api/inbounds/bulk-action", json={"action": "delete", "uids": uids})
    assert r.json()["affected"] == 4
    remaining = {i["uid"] for i in client.get("/api/inbounds").json()["inbounds"]}
    assert not remaining & set(uids)


def test_bulk_action_validation(client):
    assert client.post("/api/inbounds/bulk-action",
                       json={"action": "nuke", "uids": ["x"]}).status_code == 400
    assert client.post("/api/inbounds/bulk-action",
                       json={"action": "delete", "uids": []}).status_code == 400
    assert client.post("/api/inbounds/bulk-action",
                       json={"action": "delete", "uids": ["missing"]}).status_code == 404


# ------------------------------------------------------------------ history
def test_history_is_dense_and_bounded(client):
    ib = client.post("/api/inbounds", json={"name": "hist", "quota_gb": 5}).json()["inbound"]
    uid = ib["uid"]
    rec = main.inbound_by_uid(main.store.get_sync(), uid)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    rec["history"] = [{"d": today, "up": 100, "down": 200}]

    r = client.get(f"/api/inbounds/{uid}/history")
    assert r.status_code == 200
    series = r.json()["history"]
    # One entry per retained day, gaps zero-filled so the chart has no holes.
    assert len(series) == 30
    assert series[-1]["d"] == today
    assert series[-1]["up"] == 100 and series[-1]["down"] == 200
    assert series[0]["up"] == 0 and series[0]["down"] == 0
    # dates must be strictly increasing
    assert [s["d"] for s in series] == sorted(s["d"] for s in series)


def test_history_excluded_from_listing(client):
    """90 daily buckets per user would dominate the /api/inbounds payload."""
    ib = client.post("/api/inbounds", json={"name": "nohist"}).json()["inbound"]
    assert "history" not in ib
    listed = client.get("/api/inbounds").json()["inbounds"]
    assert all("history" not in row for row in listed)


def test_history_records_traffic(client):
    ib = client.post("/api/inbounds", json={"name": "rec", "quota_gb": 5}).json()["inbound"]
    uid = ib["uid"]
    main.runtime["pending_traffic"][uid] = {"up": 500, "down": 700}
    client.portal.call(main._fold_pending_traffic)

    series = client.get(f"/api/inbounds/{uid}/history").json()["history"]
    assert series[-1]["up"] == 500
    assert series[-1]["down"] == 700


def test_history_404(client):
    assert client.get("/api/inbounds/deadbeefdeadbeef/history").status_code == 404


# ------------------------------------------------------------------ two-factor auth
def test_2fa_full_enrolment_and_login(client):
    assert client.get("/api/2fa/status").json()["enabled"] is False

    setup = client.post("/api/2fa/setup", json={}).json()
    secret = setup["secret"]
    assert secret and setup["uri"].startswith("otpauth://totp/")

    # A wrong code must not enable it.
    assert client.post("/api/2fa/enable", json={"code": "000000"}).status_code == 400
    assert client.get("/api/2fa/status").json()["enabled"] is False

    r = client.post("/api/2fa/enable", json={"code": totp.generate_code(secret)})
    assert r.status_code == 200
    codes = r.json()["recovery_codes"]
    assert len(codes) == 8

    status = client.get("/api/2fa/status").json()
    assert status["enabled"] is True
    assert status["recovery_remaining"] == 8

    # Password alone is no longer enough.
    with TestClient(main.app) as fresh:
        r = fresh.post("/api/login", json=ADMIN)
        assert r.status_code == 200
        assert r.json() == {"ok": False, "twofa_required": True}
        assert fresh.get("/api/inbounds").status_code == 401

        bad = fresh.post("/api/login", json={**ADMIN, "code": "123456"})
        assert bad.status_code == 401

        good = fresh.post("/api/login", json={**ADMIN, "code": totp.generate_code(secret)})
        assert good.status_code == 200
        assert fresh.get("/api/inbounds").status_code == 200

    # A recovery code works once, then is burned.
    with TestClient(main.app) as fresh:
        r = fresh.post("/api/login", json={**ADMIN, "code": codes[0]})
        assert r.status_code == 200
        assert r.json()["recovery_remaining"] == 7
    with TestClient(main.app) as fresh:
        assert fresh.post("/api/login", json={**ADMIN, "code": codes[0]}).status_code == 401

    # Disabling requires the password.
    assert client.post("/api/2fa/disable", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/2fa/disable", json={"password": ADMIN["password"]}).status_code == 200
    assert client.get("/api/2fa/status").json()["enabled"] is False

    with TestClient(main.app) as fresh:
        assert fresh.post("/api/login", json=ADMIN).status_code == 200


def test_2fa_qr_requires_pending_secret(client):
    assert client.get("/api/2fa/qr").status_code in (200, 404)
    client.post("/api/2fa/setup", json={})
    r = client.get("/api/2fa/qr")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")
    # leave 2FA off for the remaining tests
    main.store.get_sync()["twofa"] = {"enabled": False, "secret": "",
                                      "recovery_hashes": [], "confirmed_at": None}


def test_recovery_regeneration_requires_password(client):
    setup = client.post("/api/2fa/setup", json={}).json()
    client.post("/api/2fa/enable", json={"code": totp.generate_code(setup["secret"])})
    try:
        assert client.post("/api/2fa/recovery-codes", json={"password": "nope"}).status_code == 401
        r = client.post("/api/2fa/recovery-codes", json={"password": ADMIN["password"]})
        assert len(r.json()["recovery_codes"]) == 8
    finally:
        client.post("/api/2fa/disable", json={"password": ADMIN["password"]})


# ------------------------------------------------------------------ login log
def test_login_log_records_attempts(client):
    with TestClient(main.app) as fresh:
        fresh.post("/api/login", json={**ADMIN, "password": "definitely-wrong"})
        fresh.post("/api/login", json=ADMIN)

    entries = client.get("/api/login-log").json()["entries"]
    assert entries, "expected login attempts to be recorded"
    # newest first
    assert entries[0]["ts"] >= entries[-1]["ts"]
    assert any(e["ok"] for e in entries)
    assert any(not e["ok"] for e in entries)
    assert all("ip" in e and "method" in e for e in entries)


def test_login_log_is_bounded():
    from storage import MAX_LOGIN_LOG
    db = {"login_log": [{"ts": i, "ip": "1.1.1.1", "ok": True, "method": "password"}
                        for i in range(MAX_LOGIN_LOG + 50)]}
    import storage
    storage.normalize_db(db)
    assert len(db["login_log"]) == MAX_LOGIN_LOG
    # the newest entries survive the trim
    assert db["login_log"][-1]["ts"] == MAX_LOGIN_LOG + 49


# ------------------------------------------------------------------ notifications
def test_alerts_require_configuration(client):
    db = main.store.get_sync()
    assert client.portal.call(main._scan_alerts, db) == []
    assert client.post("/api/notify/test").status_code == 400


def test_settings_reject_malformed_bot_token(client):
    assert client.post("/api/settings", json={"telegram_bot_token": "not-a-token"}).status_code == 400
    r = client.post("/api/settings", json={
        "telegram_bot_token": "123456789:AAEhBOweik6ad9r_ZeuN65HDdvBcQnKxyz0",
        "telegram_chat_id": "987654321",
    })
    assert r.status_code == 200


def test_quota_and_expiry_alerts_are_scheduled(client):
    client.post("/api/settings", json={
        "telegram_bot_token": "123456789:AAEhBOweik6ad9r_ZeuN65HDdvBcQnKxyz0",
        "telegram_chat_id": "987654321",
        "notify_quota_enabled": True, "notify_quota_percent": 80,
        "notify_expiry_enabled": True, "notify_expiry_days": 3,
    })
    hot = client.post("/api/inbounds", json={"name": "hot", "quota_gb": 1}).json()["inbound"]
    soon = client.post("/api/inbounds", json={"name": "soon", "expire_days": 2}).json()["inbound"]

    main.inbound_by_uid(main.store.get_sync(), hot["uid"])["used_down"] = int(0.9 * 1024 ** 3)

    db = main.store.get_sync()
    due = client.portal.call(main._scan_alerts, db)
    kinds = {(uid, kind) for uid, kind, _ in due}
    assert (hot["uid"], "quota") in kinds
    assert (soon["uid"], "expiry") in kinds

    # Once recorded, the same alert is suppressed for the cooldown window.
    sent = db.setdefault("alerts_sent", {})
    for uid, kind, _ in due:
        notify.record_alert(sent, uid, kind)
    assert client.portal.call(main._scan_alerts, db) == []


def test_alert_cooldown_expires():
    sent = {}
    notify.record_alert(sent, "u1", "quota", now=1000)
    assert notify.should_alert(sent, "u1", "quota", now=1000 + 3600) is False
    assert notify.should_alert(sent, "u1", "quota", now=1000 + notify.ALERT_COOLDOWN + 1) is True
    assert notify.should_alert(sent, "u1", "expiry", now=1000) is True


def test_alert_table_is_bounded():
    sent = {}
    for i in range(notify.MAX_ALERT_RECORDS + 200):
        notify.record_alert(sent, f"u{i}", "quota", now=1000 + i)
    assert len(sent) <= notify.MAX_ALERT_RECORDS


def test_telegram_message_escapes_user_names():
    msg = notify.format_quota_alert("Panel", "<script>x</script>", 100, 200, 50)
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg
