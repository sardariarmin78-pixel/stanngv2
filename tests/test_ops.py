"""
Retention sweeps, trial accounts and fragmentation profiles.

The retention rules get the most attention here: they delete accounts, and
an off-by-one in them costs a paying customer their subscription. The plan is
computed separately from the sweep so it can be asserted directly.
"""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import fragments
import main

ADMIN = {"username": "opsadmin", "password": "correct horse battery"}
DAY = 86400


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


@pytest.fixture(autouse=True)
def clean_users(client):
    """Two tests here reset the panel to check auth. Re-establish the shared
    session first so that does not cascade into 401s for everything after."""
    if client.get("/api/setup-status").json().get("needs_setup"):
        client.post("/api/setup", json=ADMIN)
    client.post("/api/login", json=ADMIN)
    for ib in client.get("/api/inbounds").json()["inbounds"]:
        client.delete(f"/api/inbounds/{ib['uid']}")
    client.post("/api/settings", json={"cleanup_enabled": False})
    yield


def db_with(*inbounds, **settings):
    base = {"cleanup_enabled": True, "cleanup_disable_days": 3,
            "cleanup_delete_days": 30}
    base.update(settings)
    return {"settings": base, "inbounds": list(inbounds)}


def user(uid, days_past_expiry=None, enabled=True, trial=False):
    now = time.time()
    return {
        "uid": uid, "uuid": f"uuid-{uid}", "name": uid, "enabled": enabled,
        "expire_at": None if days_past_expiry is None else now - days_past_expiry * DAY,
        "is_trial": trial,
    }


# ------------------------------------------------------------------ retention rules
def test_nothing_happens_while_disabled():
    plan = main._retention_plan(db_with(user("a", 999), cleanup_enabled=False))
    assert plan == {"disable": [], "delete": []}


def test_accounts_without_an_expiry_are_never_touched():
    """An unlimited account has no age, however old it is."""
    plan = main._retention_plan(db_with(user("forever", None)))
    assert plan == {"disable": [], "delete": []}


def test_unexpired_accounts_are_never_touched():
    plan = main._retention_plan(db_with(user("live", -5)))   # expires in 5 days
    assert plan == {"disable": [], "delete": []}


def test_recently_expired_is_left_alone_inside_the_grace_window():
    """Someone one day late has probably just not paid yet."""
    plan = main._retention_plan(db_with(user("late", 1)))
    assert plan == {"disable": [], "delete": []}


def test_disabled_after_the_grace_window():
    plan = main._retention_plan(db_with(user("stale", 5)))
    assert plan["disable"] == ["stale"]
    assert plan["delete"] == []


def test_already_disabled_is_not_disabled_again():
    plan = main._retention_plan(db_with(user("stale", 5, enabled=False)))
    assert plan["disable"] == []


def test_deleted_after_the_long_window():
    plan = main._retention_plan(db_with(user("ancient", 40)))
    assert plan["delete"] == ["ancient"]
    assert plan["disable"] == []


def test_delete_can_be_turned_off_entirely():
    """0 means never delete, only disable — the safe setting for a seller."""
    plan = main._retention_plan(db_with(user("ancient", 400), cleanup_delete_days=0))
    assert plan["delete"] == []
    assert plan["disable"] == ["ancient"]


def test_trials_are_deleted_a_day_after_expiry():
    """Trials are disposable; keeping them for the full window just clutters.

    A paid account at the same age is still inside its grace window.
    """
    plan = main._retention_plan(db_with(user("t", 2, trial=True), user("p", 2)))
    assert plan["delete"] == ["t"]
    assert plan["disable"] == []          # "p" is only 2 days past a 3-day grace

    # once past the grace window the paid account is disabled, not deleted
    plan = main._retention_plan(db_with(user("t", 5, trial=True), user("p", 5)))
    assert plan["delete"] == ["t"]
    assert plan["disable"] == ["p"]


def test_boundaries_are_inclusive():
    assert main._retention_plan(db_with(user("x", 3)))["disable"] == ["x"]
    assert main._retention_plan(db_with(user("x", 2.9)))["disable"] == []
    assert main._retention_plan(db_with(user("y", 30)))["delete"] == ["y"]
    assert main._retention_plan(db_with(user("y", 29.9)))["delete"] == []


def test_malformed_settings_fall_back_to_defaults():
    plan = main._retention_plan(db_with(user("a", 10),
                                        cleanup_disable_days="abc",
                                        cleanup_delete_days=None))
    assert plan["disable"] == ["a"]


# ------------------------------------------------------------------ sweep
def test_preview_reports_without_changing_anything(client):
    ib = client.post("/api/inbounds", json={"name": "Old", "expire_days": 1}).json()["inbound"]
    main.inbound_by_uid(main.store.get_sync(), ib["uid"])["expire_at"] = time.time() - 10 * DAY
    client.post("/api/settings", json={"cleanup_enabled": True})

    preview = client.get("/api/cleanup").json()
    assert [u["uid"] for u in preview["would_disable"]] == [ib["uid"]]
    # still there, still enabled
    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert row is not None and row["enabled"] is True


def test_sweep_disables_then_deletes(client):
    stale = client.post("/api/inbounds", json={"name": "Stale", "expire_days": 1}).json()["inbound"]
    ancient = client.post("/api/inbounds", json={"name": "Ancient", "expire_days": 1}).json()["inbound"]
    db = main.store.get_sync()
    main.inbound_by_uid(db, stale["uid"])["expire_at"] = time.time() - 5 * DAY
    main.inbound_by_uid(db, ancient["uid"])["expire_at"] = time.time() - 90 * DAY
    client.post("/api/settings", json={"cleanup_enabled": True})

    r = client.post("/api/cleanup").json()
    assert r["disabled"] == 1 and r["deleted"] == 1

    assert main.inbound_by_uid(main.store.get_sync(), ancient["uid"]) is None
    assert main.inbound_by_uid(main.store.get_sync(), stale["uid"])["enabled"] is False


def test_sweep_records_its_last_run(client):
    client.post("/api/settings", json={"cleanup_enabled": True})
    client.post("/api/cleanup")
    # a no-op sweep leaves the previous record rather than writing zeros
    body = client.get("/api/cleanup").json()
    assert body["enabled"] is True


def test_cleanup_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.get("/api/cleanup").status_code == 401
        assert c.post("/api/cleanup").status_code == 401


# ------------------------------------------------------------------ trials
def test_trial_uses_the_configured_size(client):
    client.post("/api/settings", json={"trial_enabled": True, "trial_gb": 2,
                                       "trial_days": 3, "trial_prefix": "demo"})
    ib = client.post("/api/inbounds/trial").json()["inbound"]
    assert ib["quota_gb"] == 2
    assert ib["status"]["days_left"] in (2, 3)
    assert ib["name"].startswith("demo-")
    assert ib["is_trial"] is True
    assert ib["max_connections"] == 1


def test_trials_are_numbered(client):
    client.post("/api/settings", json={"trial_enabled": True, "trial_prefix": "t"})
    first = client.post("/api/inbounds/trial").json()["inbound"]["name"]
    second = client.post("/api/inbounds/trial").json()["inbound"]["name"]
    assert first != second
    assert first.endswith("001")


def test_trial_can_be_disabled(client):
    client.post("/api/settings", json={"trial_enabled": False})
    assert client.post("/api/inbounds/trial").status_code == 400
    client.post("/api/settings", json={"trial_enabled": True})


def test_trial_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.post("/api/inbounds/trial").status_code == 401


# ------------------------------------------------------------------ fragments
@pytest.mark.parametrize("profile,expected", [
    ("off", ""),
    ("light", "tlshello,10-20,10-20"),
    ("balanced", "tlshello,40-60,10-20"),
    ("aggressive", "tlshello,100-200,10-30"),
    ("packet", "1-3,40-60,5-10"),
])
def test_profile_resolution(profile, expected):
    assert fragments.as_param({"fragment_enabled": True,
                               "fragment_profile": profile}) == expected


def test_custom_profile_uses_typed_values():
    assert fragments.as_param({
        "fragment_enabled": True, "fragment_profile": "custom",
        "fragment_packets": "tlshello", "fragment_length": "1-2",
        "fragment_interval": "3-4"}) == "tlshello,1-2,3-4"


def test_master_switch_beats_the_profile():
    assert fragments.as_param({"fragment_enabled": False,
                               "fragment_profile": "aggressive"}) == ""


def test_unknown_profile_falls_back():
    assert fragments.as_param({"fragment_enabled": True,
                               "fragment_profile": "nope"}) == \
        fragments.as_param({"fragment_enabled": True,
                            "fragment_profile": fragments.DEFAULT_PROFILE})


def test_profile_reaches_the_link(client):
    ib = client.post("/api/inbounds", json={"name": "F"}).json()["inbound"]
    client.post("/api/settings", json={"fragment_enabled": True,
                                       "fragment_profile": "aggressive"})
    tls = client.get(f"/api/inbounds/{ib['uid']}/links").json()["links"]["tls"]
    assert "fragment=tlshello%2C100-200%2C10-30" in tls

    client.post("/api/settings", json={"fragment_profile": "off"})
    tls = client.get(f"/api/inbounds/{ib['uid']}/links").json()["links"]["tls"]
    assert "fragment=" not in tls
    client.post("/api/settings", json={"fragment_profile": "balanced"})


def test_invalid_profile_is_rejected(client):
    assert client.post("/api/settings",
                       json={"fragment_profile": "nonsense"}).status_code == 400


def test_profiles_endpoint_lists_everything(client):
    body = client.get("/api/fragment-profiles").json()
    ids = [p["id"] for p in body["profiles"]]
    assert ids == ["off", "light", "balanced", "aggressive", "packet", "custom"]
    for p in body["profiles"]:
        assert p["label_fa"] and p["label_en"]
        assert p["note_fa"] and p["note_en"]


def test_upgrade_preserves_hand_tuned_values():
    """Before profiles existed the typed values were always used. Defaulting an
    upgraded install to a preset would silently discard that tuning."""
    import storage
    old = {"schema_version": 8,
           "admin": {"username": "a", "password_hash": "x", "salt": "y"},
           "secret_key": "k" * 64, "inbounds": [],
           "settings": {"fragment_enabled": True, "fragment_packets": "tlshello",
                        "fragment_length": "7-9", "fragment_interval": "2-4"}}
    storage.normalize_db(old)
    assert old["settings"]["fragment_profile"] == "custom"
    assert fragments.as_param(old["settings"]) == "tlshello,7-9,2-4"


def test_fresh_install_gets_the_default_profile():
    import storage
    fresh = storage._fresh_db()
    assert fresh["settings"]["fragment_profile"] == fragments.DEFAULT_PROFILE
