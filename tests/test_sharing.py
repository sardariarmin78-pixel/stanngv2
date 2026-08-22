"""
Anti-sharing detection.

The hard part is not spotting a shared account, it is *not* accusing an
honest one. A phone on Iranian mobile data changes address every few
minutes, so counting raw addresses would flag nearly every real customer.
Detection counts networks instead, and most of this file exists to hold
that distinction in place.
"""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main

ADMIN = {"username": "shadmin", "password": "correct horse battery"}


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
    for ib in client.get("/api/inbounds").json()["inbounds"]:
        client.delete(f"/api/inbounds/{ib['uid']}")
    client.post("/api/settings", json={"sharing_detect_enabled": True,
                                       "sharing_threshold": 4,
                                       "sharing_window_hours": 24,
                                       "sharing_auto_disable": False})
    yield


def make_user(client, name="Ali", **kw):
    payload = {"name": name, "quota_gb": 5}
    payload.update(kw)
    return client.post("/api/inbounds", json=payload).json()["inbound"]


def seen_from(uid, ips, ago=0):
    """Write addresses straight into the log, as the flush would."""
    row = main.inbound_by_uid(main.store.get_sync(), uid)
    log = row.setdefault("ip_log", {})
    for ip in ips:
        log[ip] = time.time() - ago
    return row


# ------------------------------------------------------------------ networks
def test_addresses_collapse_to_their_network():
    assert main._network_of("5.117.20.31") == "5.117.20.0/24"
    assert main._network_of("5.117.20.200") == "5.117.20.0/24"
    assert main._network_of("91.99.1.4") == "91.99.1.0/24"


def test_ipv6_collapses_to_a_64():
    a = main._network_of("2a01:4f8:1c1c:aaaa:1::1")
    b = main._network_of("2a01:4f8:1c1c:aaaa:9999::42")
    assert a == b == "2a01:4f8:1c1c:aaaa::/64"


def test_garbage_does_not_crash_the_collapse():
    assert main._network_of("") == ""
    assert main._network_of(None) == ""
    assert main._network_of("not-an-ip") == "not-an-ip"


# ------------------------------------------------------------------ counting
def test_a_phone_changing_address_is_one_network(client):
    """The whole reason networks are counted rather than addresses."""
    ib = make_user(client)
    row = seen_from(ib["uid"], [f"5.117.20.{n}" for n in range(1, 30)])

    report = main.sharing_report(row, 86400)
    assert report["ips"] == 29
    assert report["networks"] == 1


def test_separate_households_count_separately(client):
    ib = make_user(client)
    row = seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"])
    assert main.sharing_report(row, 86400)["networks"] == 4


def test_addresses_outside_the_window_do_not_count(client):
    ib = make_user(client)
    row = seen_from(ib["uid"], ["5.117.20.4"])
    seen_from(ib["uid"], ["91.99.1.7", "178.22.3.9"], ago=86400 * 3)

    assert main.sharing_report(row, 86400)["networks"] == 1
    assert main.sharing_report(row, 86400 * 7)["networks"] == 3


def test_an_empty_log_reports_nothing(client):
    ib = make_user(client)
    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    report = main.sharing_report(row, 86400)
    assert report == {"ips": 0, "networks": 0, "last_seen": None, "recent": []}


# ------------------------------------------------------------------ flagging
def test_below_the_threshold_nothing_is_flagged(client):
    ib = make_user(client)
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9"])   # 3 < 4

    assert client.portal.call(main._run_sharing_check) == []
    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert not row.get("sharing_flagged")


def test_crossing_the_threshold_flags_once(client):
    ib = make_user(client, name="Sharer")
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"])

    transitions = client.portal.call(main._run_sharing_check)
    assert [t[0] for t in transitions] == ["flagged"]
    assert transitions[0][2] == "Sharer"
    assert transitions[0][3] == 4

    # a second sweep with no change stays quiet
    assert client.portal.call(main._run_sharing_check) == []


def test_the_flag_clears_itself_when_the_behaviour_stops(client):
    """The seller should not have to reset a flag by hand."""
    ib = make_user(client)
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"])
    client.portal.call(main._run_sharing_check)

    # the evidence ages out of the window
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"],
              ago=86400 * 2)
    transitions = client.portal.call(main._run_sharing_check)
    assert [t[0] for t in transitions] == ["cleared"]
    assert not main.inbound_by_uid(main.store.get_sync(), ib["uid"])["sharing_flagged"]


def test_detection_off_means_no_flagging(client):
    ib = make_user(client)
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"])
    client.post("/api/settings", json={"sharing_detect_enabled": False})

    assert client.portal.call(main._run_sharing_check) == []
    assert not main.inbound_by_uid(main.store.get_sync(), ib["uid"]).get("sharing_flagged")


def test_auto_disable_cuts_the_account_off(client):
    ib = make_user(client)
    client.post("/api/settings", json={"sharing_auto_disable": True})
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"])

    client.portal.call(main._run_sharing_check)
    assert main.inbound_by_uid(main.store.get_sync(), ib["uid"])["enabled"] is False
    client.post("/api/settings", json={"sharing_auto_disable": False})


def test_auto_disable_off_leaves_the_account_running(client):
    """Flagging is information; cutting off a paying customer is a decision."""
    ib = make_user(client)
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"])

    client.portal.call(main._run_sharing_check)
    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert row["enabled"] is True
    assert row["sharing_flagged"] is True


# ------------------------------------------------------------------ settings
def test_the_window_has_a_floor():
    """A ten-minute window would flag one person walking between two wifis."""
    conf = main._sharing_settings({"settings": {"sharing_window_hours": 0}})
    assert conf["window"] >= main.SHARING_MIN_WINDOW


def test_the_threshold_cannot_be_one():
    """A threshold of one would flag every account on first use."""
    conf = main._sharing_settings({"settings": {"sharing_threshold": 1}})
    assert conf["threshold"] >= 2


def test_settings_survive_garbage():
    conf = main._sharing_settings({"settings": {"sharing_window_hours": "abc",
                                                "sharing_threshold": None}})
    assert conf["threshold"] == 4
    assert conf["window"] == main.SHARING_DEFAULT_WINDOW


def test_settings_are_validated_by_the_api(client):
    assert client.post("/api/settings",
                       json={"sharing_threshold": "abc"}).status_code == 400
    r = client.post("/api/settings", json={"sharing_threshold": 99})
    assert r.json()["settings"]["sharing_threshold"] == 50      # clamped


# ------------------------------------------------------------------ storage
def test_the_log_is_pruned_to_the_window(client):
    ib = make_user(client)
    row = seen_from(ib["uid"], ["1.1.1.1"], ago=86400 * 5)
    main._fold_ip_log(row, {"2.2.2.2": time.time()}, 86400)

    assert "1.1.1.1" not in row["ip_log"]
    assert "2.2.2.2" in row["ip_log"]


def test_the_log_is_capped(client):
    """An account open for a year must not carry a year of addresses."""
    ib = make_user(client)
    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    now = time.time()
    fresh = {f"10.0.{i // 256}.{i % 256}": now - i for i in range(200)}
    main._fold_ip_log(row, fresh, 86400)

    assert len(row["ip_log"]) == main.MAX_TRACKED_IPS
    # the newest survive, the oldest are dropped
    assert "10.0.0.0" in row["ip_log"]
    assert "10.0.0.199" not in row["ip_log"]


def test_recording_does_not_touch_the_database(client):
    """Writing on every connect is what the buffered design exists to avoid."""
    ib = make_user(client)
    main.runtime["pending_ips"].clear()
    main._record_ip(ib["uid"], "5.117.20.4")

    assert main.runtime["pending_ips"][ib["uid"]]["5.117.20.4"]
    assert main.inbound_by_uid(main.store.get_sync(), ib["uid"])["ip_log"] == {}
    main.runtime["pending_ips"].clear()


def test_the_flush_folds_buffered_addresses_in(client):
    ib = make_user(client)
    main._record_ip(ib["uid"], "5.117.20.4")
    client.portal.call(main._fold_pending_traffic)

    assert "5.117.20.4" in main.inbound_by_uid(main.store.get_sync(), ib["uid"])["ip_log"]


def test_upgrade_gives_existing_users_an_empty_log():
    import storage
    db = {"schema_version": 12,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64,
          "inbounds": [{"uid": "abc123", "uuid": "u-1", "name": "Old"}]}
    storage.normalize_db(db)
    assert db["inbounds"][0]["ip_log"] == {}


# ------------------------------------------------------------------ api
def test_the_listing_does_not_ship_the_whole_log(client):
    """40 addresses per user would dominate the payload."""
    ib = make_user(client)
    seen_from(ib["uid"], [f"5.117.20.{n}" for n in range(1, 30)])

    row = client.get("/api/inbounds").json()["inbounds"][0]
    assert "ip_log" not in row
    assert row["sharing"] == {"ips": 29, "networks": 1}


def test_the_listing_omits_the_summary_when_detection_is_off(client):
    make_user(client)
    client.post("/api/settings", json={"sharing_detect_enabled": False})
    body = client.get("/api/inbounds").json()
    assert "sharing" not in body["inbounds"][0]
    assert body["sharing_threshold"] == 0


def test_the_detail_endpoint_lists_where_it_was_used(client):
    ib = make_user(client)
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7"])

    r = client.get(f"/api/inbounds/{ib['uid']}/networks").json()
    assert r["networks"] == 2
    assert r["window_hours"] == 24
    assert {e["network"] for e in r["recent"]} == {"5.117.20.0/24", "91.99.1.0/24"}


def test_clearing_the_flag_starts_the_window_over(client):
    ib = make_user(client)
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"])
    client.portal.call(main._run_sharing_check)

    assert client.post(f"/api/inbounds/{ib['uid']}/clear-flag").status_code == 200
    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert row["ip_log"] == {}
    assert row["sharing_flagged"] is False


def test_the_manual_sweep_reports_who_it_flagged(client):
    ib = make_user(client, name="Busted")
    seen_from(ib["uid"], ["5.117.20.4", "91.99.1.7", "178.22.3.9", "2.144.8.1"])

    r = client.post("/api/sharing/check").json()
    assert [f["name"] for f in r["flagged"]] == ["Busted"]


def test_the_manual_sweep_refuses_while_disabled(client):
    client.post("/api/settings", json={"sharing_detect_enabled": False})
    r = client.post("/api/sharing/check")
    assert r.status_code == 400
    assert r.json()["detail"] == "sharing-disabled"


def test_the_sweep_is_owner_only(client):
    client.post("/api/resellers", json={"username": "sh_seller",
                                        "password": "seller-pass-123"})
    seller = TestClient(main.app)
    seller.post("/api/login", json={"username": "sh_seller", "password": "seller-pass-123"})
    assert seller.post("/api/sharing/check").status_code == 403


def test_a_reseller_sees_its_own_users_networks_only(client):
    """The detail view is per-user data, so it follows the same ownership rule."""
    client.post("/api/resellers", json={"username": "sh_seller2",
                                        "password": "seller-pass-456"})
    seller = TestClient(main.app)
    seller.post("/api/login", json={"username": "sh_seller2", "password": "seller-pass-456"})

    victim = make_user(client, "NotYours")
    assert seller.get(f"/api/inbounds/{victim['uid']}/networks").status_code == 404
    assert seller.post(f"/api/inbounds/{victim['uid']}/clear-flag").status_code == 404


def test_networks_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.get("/api/inbounds/x/networks").status_code == 401
