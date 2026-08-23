"""
Reading exports from other panels.

The parser is where migrations go wrong, so most of this file feeds it the
shapes real panels actually emit -- including the awkward ones, like 3x-ui
storing its client list as a JSON string inside a field.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import migrate

ADMIN = {"username": "imadmin", "password": "correct horse battery"}
SELLER = {"username": "im_seller", "password": "seller-pass-1234"}
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
    for ib in client.get("/api/inbounds").json()["inbounds"]:
        client.delete(f"/api/inbounds/{ib['uid']}")
    for r in client.get("/api/resellers").json()["resellers"]:
        client.delete(f"/api/resellers/{r['id']}?delete_users=1")
    yield


# ------------------------------------------------------------------ fixtures
def marzban_doc(**over):
    user = {
        "username": "ali",
        "proxies": {"vless": {"id": "35e4e39c-7d5c-4f4b-8b71-558e4f37ff53"},
                    "vmess": {}},
        "data_limit": 50 * GB,
        "used_traffic": 10 * GB,
        "expire": int(time.time()) + 30 * 86400,
        "status": "active",
        "note": "پرداخت شده",
    }
    user.update(over)
    return {"users": [user]}


def xui_doc(**over):
    client = {
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "email": "sara@peyk.local",
        "totalGB": 20 * GB,
        "up": 1 * GB,
        "down": 2 * GB,
        "expiryTime": int((time.time() + 15 * 86400) * 1000),   # milliseconds
        "enable": True,
        "limitIp": 2,
    }
    client.update(over)
    # settings arrives as a JSON *string*, which is the shape that trips parsers
    return {"id": 1, "remark": "inbound-1", "settings": json.dumps({"clients": [client]})}


# ------------------------------------------------------------------ marzban
def test_marzban_is_detected():
    source, rows = migrate.detect_and_parse(marzban_doc())
    assert source == "marzban"
    assert len(rows) == 1


def test_marzban_fields_land_correctly():
    _, rows = migrate.detect_and_parse(marzban_doc())
    row = rows[0]
    assert row["name"] == "ali"
    assert row["quota_gb"] == 50
    assert row["used_down"] == 10 * GB
    assert row["enabled"] is True
    assert row["note"] == "پرداخت شده"
    assert row["source_uuid"].startswith("35e4e39c")


def test_marzban_zero_limit_means_unlimited():
    _, rows = migrate.detect_and_parse(marzban_doc(data_limit=0, expire=0))
    assert rows[0]["quota_gb"] == 0
    assert rows[0]["expire_at"] is None


@pytest.mark.parametrize("status,enabled", [
    ("active", True), ("on_hold", True),
    ("disabled", False), ("limited", False), ("expired", False),
])
def test_marzban_status_maps_to_enabled(status, enabled):
    _, rows = migrate.detect_and_parse(marzban_doc(status=status))
    assert rows[0]["enabled"] is enabled


def test_a_bare_marzban_list_works():
    """Some exports drop the wrapper object."""
    source, rows = migrate.detect_and_parse(marzban_doc()["users"])
    assert source == "marzban"
    assert rows[0]["name"] == "ali"


# ------------------------------------------------------------------ 3x-ui
def test_xui_is_detected_through_the_settings_string():
    source, rows = migrate.detect_and_parse(xui_doc())
    assert source == "3x-ui"
    assert len(rows) == 1


def test_xui_fields_land_correctly():
    _, rows = migrate.detect_and_parse(xui_doc())
    row = rows[0]
    assert row["name"] == "sara"                 # local part of the email
    assert row["quota_gb"] == 20
    assert row["used_down"] == 3 * GB            # up + down folded together
    assert row["max_connections"] == 2


def test_xui_milliseconds_become_seconds():
    _, rows = migrate.detect_and_parse(xui_doc())
    assert abs(rows[0]["expire_at"] - (time.time() + 15 * 86400)) < 5


def test_xui_negative_expiry_is_not_a_date():
    """A negative expiryTime means "N ms after first use", which has no
    equivalent here -- better no expiry than a date in 1970."""
    _, rows = migrate.detect_and_parse(xui_doc(expiryTime=-2592000000))
    assert rows[0]["expire_at"] is None


def test_xui_disabled_client_stays_disabled():
    _, rows = migrate.detect_and_parse(xui_doc(enable=False))
    assert rows[0]["enabled"] is False


def test_a_plain_clients_list_works():
    doc = {"clients": [{"email": "x", "totalGB": 0, "expiryTime": 0}]}
    source, rows = migrate.detect_and_parse(doc)
    assert source == "3x-ui"
    assert rows[0]["name"] == "x"


def test_a_multi_inbound_export_is_merged():
    a = xui_doc(email="one@x")
    b = xui_doc(email="two@x")
    source, rows = migrate.detect_and_parse({"obj": [a, b]})
    assert source == "3x-ui"
    assert sorted(r["name"] for r in rows) == ["one", "two"]


# ------------------------------------------------------------------ peyk
def test_a_peyk_backup_can_be_read_back():
    doc = {"schema_version": 15, "settings": {}, "inbounds": [
        {"uid": "a1", "uuid": "u", "name": "Reza", "quota_gb": 5,
         "used_up": GB, "used_down": GB, "enabled": True, "max_connections": 3},
    ]}
    source, rows = migrate.detect_and_parse(doc)
    assert source == "peyk"
    assert rows[0]["name"] == "Reza"
    assert rows[0]["quota_gb"] == 5
    assert rows[0]["used_down"] == 2 * GB


# ------------------------------------------------------------------ refusals
def test_unknown_shapes_are_refused():
    for doc in ({"something": "else"}, {"users": []}, [], [1, 2, 3]):
        with pytest.raises(migrate.ImportError_) as e:
            migrate.detect_and_parse(doc)
        assert e.value.reason in ("unknown-format", "invalid-json")


def test_broken_json_is_refused():
    with pytest.raises(migrate.ImportError_) as e:
        migrate.detect_and_parse("{not json")
    assert e.value.reason == "invalid-json"


def test_a_json_string_is_accepted():
    source, rows = migrate.detect_and_parse(json.dumps(marzban_doc()))
    assert source == "marzban"
    assert rows[0]["name"] == "ali"


def test_an_absurd_file_is_refused():
    doc = {"users": [{"username": f"u{i}", "data_limit": 0} for i in range(6000)]}
    with pytest.raises(migrate.ImportError_) as e:
        migrate.detect_and_parse(doc)
    assert e.value.reason == "too-many-users"


def test_control_characters_are_stripped_from_names():
    _, rows = migrate.detect_and_parse(marzban_doc(username="ev\x00il\x1b[31m"))
    assert "\x00" not in rows[0]["name"]
    assert "\x1b" not in rows[0]["name"]


def test_a_nameless_row_still_gets_a_name():
    _, rows = migrate.detect_and_parse(marzban_doc(username=""))
    assert rows[0]["name"] == "imported-1"


def test_garbage_numbers_do_not_crash():
    _, rows = migrate.detect_and_parse(
        marzban_doc(data_limit="abc", expire="soon", used_traffic=None))
    assert rows[0]["quota_gb"] == 0
    assert rows[0]["expire_at"] is None


# ------------------------------------------------------------------ planning
def test_duplicate_names_are_skipped_not_overwritten():
    """Overwriting a live customer with an import would be unrecoverable."""
    _, rows = migrate.detect_and_parse(marzban_doc())
    plan = migrate.plan_import(rows, {"ali"})
    assert plan["importable"] == 0
    assert plan["skipped"][0]["reason"] == "duplicate-name"


def test_duplicates_inside_one_file_are_skipped_too():
    doc = {"users": [{"username": "same", "data_limit": 0},
                     {"username": "same", "data_limit": 0}]}
    _, rows = migrate.detect_and_parse(doc)
    plan = migrate.plan_import(rows, set())
    assert plan["importable"] == 1
    assert len(plan["skipped"]) == 1


def test_an_expired_account_is_imported_but_disabled():
    """A seller migrating wants the whole customer list, lapsed ones included."""
    _, rows = migrate.detect_and_parse(
        marzban_doc(expire=int(time.time()) - 86400, status="expired"))
    plan = migrate.plan_import(rows, set())
    assert plan["importable"] == 1
    assert plan["rows"][0]["enabled"] is False
    assert plan["rows"][0]["lapsed"] == "expired"


def test_a_used_up_account_is_flagged_as_such():
    _, rows = migrate.detect_and_parse(
        marzban_doc(data_limit=10 * GB, used_traffic=11 * GB))
    plan = migrate.plan_import(rows, set())
    assert plan["rows"][0]["lapsed"] == "quota"
    assert plan["rows"][0]["enabled"] is False


# ------------------------------------------------------------------ endpoints
def test_preview_changes_nothing(client):
    r = client.post("/api/import/preview", json={"data": marzban_doc()})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "marzban"
    assert body["importable"] == 1
    assert body["sample"][0]["name"] == "ali"
    assert client.get("/api/inbounds").json()["inbounds"] == []


def test_import_creates_the_users(client):
    r = client.post("/api/import", json={"data": marzban_doc()})
    assert r.status_code == 200
    assert r.json()["imported"] == 1

    ib = client.get("/api/inbounds").json()["inbounds"][0]
    assert ib["name"] == "ali"
    assert ib["quota_gb"] == 50


def test_imported_users_get_fresh_credentials(client):
    """The old uuid is not reused: host and path change on a move between
    panels, so an imported customer needs a new link regardless."""
    client.post("/api/import", json={"data": marzban_doc()})
    ib = client.get("/api/inbounds").json()["inbounds"][0]
    assert ib["uuid"] != "35e4e39c-7d5c-4f4b-8b71-558e4f37ff53"
    assert ib["sub_token"] and ib["sub_token"] != ib["uid"]


def test_usage_carries_over_so_quotas_stay_honest(client):
    client.post("/api/import", json={"data": marzban_doc()})
    row = main.inbound_by_uid(main.store.get_sync(),
                              client.get("/api/inbounds").json()["inbounds"][0]["uid"])
    assert row["used_up"] + row["used_down"] == 10 * GB


def test_importing_twice_does_not_duplicate(client):
    client.post("/api/import", json={"data": marzban_doc()})
    second = client.post("/api/import", json={"data": marzban_doc()})
    assert second.status_code == 400
    assert second.json()["detail"] == "nothing-to-import"
    assert len(client.get("/api/inbounds").json()["inbounds"]) == 1


def test_a_bad_file_is_refused_by_the_endpoint(client):
    r = client.post("/api/import", json={"data": {"nope": 1}})
    assert r.status_code == 400
    assert r.json()["detail"] == "unknown-format"


def test_import_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.post("/api/import", json={"data": {}}).status_code == 401


# ------------------------------------------------------------------ resellers
def test_an_imported_user_belongs_to_the_importer(client):
    client.post("/api/resellers", json=SELLER)
    seller = TestClient(main.app)
    seller.post("/api/login", json=SELLER)

    seller.post("/api/import", json={"data": marzban_doc()})
    assert len(seller.get("/api/inbounds").json()["inbounds"]) == 1
    assert len(client.get("/api/inbounds").json()["inbounds"]) == 1   # owner sees it too


def test_import_respects_the_reseller_quota(client):
    client.post("/api/resellers", json=dict(SELLER, max_users=2))
    seller = TestClient(main.app)
    seller.post("/api/login", json=SELLER)

    doc = {"users": [{"username": f"u{i}", "data_limit": 0} for i in range(5)]}
    r = seller.post("/api/import", json={"data": doc})
    assert r.status_code == 403
    assert seller.get("/api/inbounds").json()["inbounds"] == []


def test_preview_warns_a_reseller_before_they_try(client):
    client.post("/api/resellers", json=dict(SELLER, max_users=2))
    seller = TestClient(main.app)
    seller.post("/api/login", json=SELLER)

    doc = {"users": [{"username": f"u{i}", "data_limit": 0} for i in range(5)]}
    body = seller.post("/api/import/preview", json={"data": doc}).json()
    assert body["over_quota"] == 3


def test_a_reseller_does_not_collide_with_the_owners_names(client):
    """Names are unique per owner, not panel-wide: two sellers can both have
    a customer called Ali."""
    client.post("/api/import", json={"data": marzban_doc()})
    client.post("/api/resellers", json=SELLER)
    seller = TestClient(main.app)
    seller.post("/api/login", json=SELLER)

    r = seller.post("/api/import", json={"data": marzban_doc()})
    assert r.status_code == 200
    assert r.json()["imported"] == 1
