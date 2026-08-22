"""
Endpoint health monitoring and subscription-link rotation.

The rotation half carries the interesting property: revoking a leaked
subscription URL must not disturb configs already installed on the
customer's device. That only works because the public token and the
WebSocket path are separate values, so both are asserted here.
"""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main

ADMIN = {"username": "hladmin", "password": "correct horse battery"}


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
    for ep in client.get("/api/endpoints").json()["endpoints"]:
        client.delete(f"/api/endpoints/{ep['id']}")
    yield


def make_user(client, **kw):
    payload = {"name": "Ali", "quota_gb": 5}
    payload.update(kw)
    return client.post("/api/inbounds", json=payload).json()["inbound"]


# ------------------------------------------------------------------ rotation
def test_new_users_get_a_token_distinct_from_the_uid(client):
    """If they were the same, anyone holding a config could derive the
    subscription URL from the path inside it."""
    ib = make_user(client)
    assert ib["sub_token"]
    assert ib["sub_token"] != ib["uid"]


def test_subscription_resolves_by_token_not_uid(client):
    ib = make_user(client)
    assert client.get(f"/sub/{ib['sub_token']}").status_code == 200
    assert client.get(f"/sub/{ib['uid']}").status_code == 404


def test_rotation_kills_the_old_link(client):
    ib = make_user(client)
    old = ib["sub_token"]
    assert client.get(f"/sub/{old}").status_code == 200

    r = client.post(f"/api/inbounds/{ib['uid']}/rotate-link")
    assert r.status_code == 200
    new = r.json()["sub_token"]
    assert new != old

    assert client.get(f"/sub/{old}").status_code == 404
    assert client.get(f"/sub/{new}").status_code == 200


def test_rotation_leaves_installed_configs_working(client):
    """The whole point: revoke the leaked URL, do not cut off the customer.

    The uuid and the WebSocket path are what an installed config uses, and
    neither may change.
    """
    ib = make_user(client)
    before = client.get(f"/api/inbounds/{ib['uid']}/links").json()["links"]["tls"]

    client.post(f"/api/inbounds/{ib['uid']}/rotate-link")

    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert row["uuid"] == ib["uuid"]          # credential unchanged
    assert row["uid"] == ib["uid"]            # ws path unchanged
    after = client.get(f"/api/inbounds/{ib['uid']}/links").json()["links"]["tls"]
    assert after == before                    # the config itself is identical


def test_regenerate_still_cuts_them_off(client):
    """Rotation is the gentle option; /regenerate is the one that revokes
    access, and it must keep doing that."""
    ib = make_user(client)
    new_uuid = client.post(f"/api/inbounds/{ib['uid']}/regenerate").json()["inbound"]["uuid"]
    assert new_uuid != ib["uuid"]


def test_status_page_follows_the_token(client):
    ib = make_user(client)
    assert client.get(f"/status/{ib['sub_token']}").status_code == 200
    assert client.get(f"/api/status/{ib['sub_token']}").status_code == 200
    client.post(f"/api/inbounds/{ib['uid']}/rotate-link")
    assert client.get(f"/status/{ib['sub_token']}").status_code == 404


def test_generated_urls_use_the_token(client):
    ib = make_user(client)
    r = client.get(f"/api/inbounds/{ib['uid']}/links").json()
    assert r["sub_url"].endswith(f"/sub/{ib['sub_token']}")
    assert r["status_url"].endswith(f"/status/{ib['sub_token']}")
    assert ib["uid"] not in r["sub_url"]


def test_rotation_404s_for_unknown_user(client):
    assert client.post("/api/inbounds/deadbeefdeadbeef/rotate-link").status_code == 404


def test_rotation_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.post("/api/inbounds/x/rotate-link").status_code == 401


def test_upgrade_keeps_existing_links_alive():
    """Installs from before tokens existed must not have every subscription
    URL 404 the moment they update."""
    import storage
    db = {"schema_version": 10,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64,
          "inbounds": [{"uid": "abc123", "uuid": "u-1", "name": "Old"}]}
    storage.normalize_db(db)
    assert db["inbounds"][0]["sub_token"] == "abc123"


# ------------------------------------------------------------------ health config
def test_health_settings_have_sane_defaults():
    conf = main._health_settings({"settings": {}})
    assert conf["enabled"] is False
    assert conf["threshold"] == 3
    assert conf["interval"] >= main.HEALTH_MIN_INTERVAL


def test_health_interval_has_a_floor():
    """A one-minute setting must not become a tight probe loop."""
    conf = main._health_settings({"settings": {"health_interval_minutes": 1}})
    assert conf["interval"] >= main.HEALTH_MIN_INTERVAL


def test_health_settings_survive_garbage():
    conf = main._health_settings({"settings": {"health_interval_minutes": "abc",
                                               "health_fail_threshold": None}})
    assert conf["threshold"] == 3
    assert conf["interval"] >= main.HEALTH_MIN_INTERVAL


def test_health_settings_are_validated(client):
    assert client.post("/api/settings",
                       json={"health_fail_threshold": "abc"}).status_code == 400
    r = client.post("/api/settings", json={"health_fail_threshold": 99})
    assert r.json()["settings"]["health_fail_threshold"] == 20    # clamped


# ------------------------------------------------------------------ health sweep
@pytest.fixture
def failing_probe(monkeypatch):
    state = {"ok": False}

    async def probe(ep):
        return {"ok": state["ok"], "ts": time.time(), "latency_ms": 5,
                "detail": "HTTP 200" if state["ok"] else "ConnectError"}

    monkeypatch.setattr(main, "_probe_endpoint", probe)
    return state


def add_endpoint(client, **kw):
    payload = {"name": "Edge", "address": "edge.example.com"}
    payload.update(kw)
    return client.post("/api/endpoints", json=payload).json()["endpoint"]


def test_a_single_failure_does_not_alert(client, failing_probe):
    """One timed-out probe is usually the probe's fault; alerting on it trains
    the admin to ignore alerts."""
    add_endpoint(client)
    client.post("/api/settings", json={"health_check_enabled": True,
                                       "health_fail_threshold": 3})
    transitions = client.portal.call(main._run_health_checks)
    assert transitions == []
    ep = client.get("/api/endpoints").json()["endpoints"][0]
    assert ep["health"]["fail_count"] == 1
    assert ep["health"]["ok"] is False


def test_alert_fires_once_the_threshold_is_crossed(client, failing_probe):
    add_endpoint(client)
    client.post("/api/settings", json={"health_check_enabled": True,
                                       "health_fail_threshold": 3})
    for _ in range(2):
        assert client.portal.call(main._run_health_checks) == []
    transitions = client.portal.call(main._run_health_checks)
    assert [t[0] for t in transitions] == ["down"]

    # and it does not keep alerting on every subsequent sweep
    assert client.portal.call(main._run_health_checks) == []


def test_recovery_is_reported(client, failing_probe):
    add_endpoint(client)
    client.post("/api/settings", json={"health_check_enabled": True,
                                       "health_fail_threshold": 1})
    assert [t[0] for t in client.portal.call(main._run_health_checks)] == ["down"]

    failing_probe["ok"] = True
    assert [t[0] for t in client.portal.call(main._run_health_checks)] == ["up"]

    ep = client.get("/api/endpoints").json()["endpoints"][0]
    assert ep["health"]["fail_count"] == 0
    assert ep["health"]["alerted_down"] is False


def test_auto_disable_drops_a_dead_route_from_subscriptions(client, failing_probe):
    ib = make_user(client)
    add_endpoint(client, name="Dead", address="dead.example.com")
    add_endpoint(client, name="Live", address="live.example.com")
    client.post("/api/settings", json={"health_check_enabled": True,
                                       "health_fail_threshold": 1,
                                       "health_auto_disable": True})
    assert client.get(f"/sub/{ib['sub_token']}").text.count("vless://") == 2

    client.portal.call(main._run_health_checks)

    # both probes fail here, so both drop out and the fallback config remains
    body = client.get(f"/sub/{ib['sub_token']}").text
    assert "dead.example.com" not in body
    client.post("/api/settings", json={"health_auto_disable": False})


def test_auto_disable_off_keeps_the_endpoint(client, failing_probe):
    add_endpoint(client)
    client.post("/api/settings", json={"health_check_enabled": True,
                                       "health_fail_threshold": 1,
                                       "health_auto_disable": False})
    client.portal.call(main._run_health_checks)
    ep = client.get("/api/endpoints").json()["endpoints"][0]
    assert ep["enabled"] is True


def test_disabled_endpoints_are_not_probed(client, failing_probe):
    ep = add_endpoint(client)
    client.patch(f"/api/endpoints/{ep['id']}",
                 json={"name": "Edge", "address": "edge.example.com", "enabled": False})
    client.post("/api/settings", json={"health_check_enabled": True})
    assert client.portal.call(main._run_health_checks) == []
    stored = client.get("/api/endpoints").json()["endpoints"][0]
    assert not (stored.get("health") or {}).get("fail_count")


def test_check_all_endpoint(client, failing_probe):
    add_endpoint(client)
    client.post("/api/settings", json={"health_check_enabled": True,
                                       "health_fail_threshold": 1})
    body = client.post("/api/endpoints/check-all").json()
    assert body["ok"] is True
    assert [t["state"] for t in body["transitions"]] == ["down"]
    assert body["endpoints"][0]["health"]["ok"] is False


def test_check_all_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.post("/api/endpoints/check-all").status_code == 401
