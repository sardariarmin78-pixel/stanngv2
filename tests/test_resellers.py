"""
Reseller sub-admins.

A reseller is a scoped login: it may create and manage its own users and
nothing else. Most of this file is the boundary — the interesting failure
mode is not "the feature does not work", it is "a reseller reached somebody
else's customer", so the isolation cases outnumber the happy ones.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main

OWNER = {"username": "rsowner", "password": "correct horse battery"}
SELLER = {"username": "seller_one", "password": "seller-pass-1234"}
OTHER = {"username": "seller_two", "password": "seller-pass-5678"}


@pytest.fixture(scope="module")
def owner():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=OWNER)
        c.post("/api/login", json=OWNER)
        yield c


@pytest.fixture(autouse=True)
def clean(owner):
    """Each test starts from one owner, no resellers, no users."""
    if owner.get("/api/setup-status").json().get("needs_setup"):
        owner.post("/api/setup", json=OWNER)
    owner.post("/api/login", json=OWNER)
    for r in owner.get("/api/resellers").json()["resellers"]:
        owner.delete(f"/api/resellers/{r['id']}?delete_users=1")
    for ib in owner.get("/api/inbounds").json()["inbounds"]:
        owner.delete(f"/api/inbounds/{ib['uid']}")
    yield


def make_reseller(owner, creds=SELLER, **kw):
    body = dict(creds)
    body.update(kw)
    r = owner.post("/api/resellers", json=body)
    assert r.status_code == 200, r.text
    return r.json()["reseller"]


def login_as(creds):
    """A second client, so the owner's cookie stays intact."""
    c = TestClient(main.app)
    r = c.post("/api/login", json=creds)
    assert r.status_code == 200, r.text
    return c


def make_user(client, name="Ali", **kw):
    payload = {"name": name, "quota_gb": 5}
    payload.update(kw)
    r = client.post("/api/inbounds", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["inbound"]


# ------------------------------------------------------------------ management
def test_owner_creates_a_reseller(owner):
    rs = make_reseller(owner)
    assert rs["username"] == SELLER["username"]
    assert rs["users"] == 0
    assert owner.get("/api/resellers").json()["resellers"][0]["id"] == rs["id"]


def test_the_password_hash_never_leaves_the_server(owner):
    make_reseller(owner)
    body = owner.get("/api/resellers").text
    assert "password_hash" not in body
    assert "salt" not in body
    assert SELLER["password"] not in body


def test_usernames_are_unique(owner):
    make_reseller(owner)
    r = owner.post("/api/resellers", json=SELLER)
    assert r.status_code == 400
    assert r.json()["detail"] == "username-taken"


def test_the_owners_own_name_is_reserved(owner):
    """Otherwise the login would not know which account was meant."""
    r = owner.post("/api/resellers",
                   json={"username": OWNER["username"], "password": "another-pass-99"})
    assert r.status_code == 400
    assert r.json()["detail"] == "username-taken"


def test_weak_passwords_are_refused(owner):
    r = owner.post("/api/resellers", json={"username": "shorty_pw", "password": "abc"})
    assert r.status_code == 400
    assert r.json()["detail"] == "weak-password"


def test_malformed_usernames_are_refused(owner):
    r = owner.post("/api/resellers", json={"username": "no spaces", "password": "long-enough-1"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid-username"


def test_quotas_round_trip(owner):
    rs = make_reseller(owner, max_users=25, max_traffic_gb=500)
    assert rs["max_users"] == 25
    assert rs["max_traffic_gb"] == 500
    owner.patch(f"/api/resellers/{rs['id']}", json={"max_users": 40})
    assert owner.get("/api/resellers").json()["resellers"][0]["max_users"] == 40


# ------------------------------------------------------------------ login
def test_a_reseller_can_sign_in(owner):
    make_reseller(owner)
    r = TestClient(main.app).post("/api/login", json=SELLER)
    assert r.status_code == 200
    assert r.json()["role"] == "reseller"


def test_the_owner_still_signs_in_as_owner(owner):
    make_reseller(owner)
    assert TestClient(main.app).post("/api/login", json=OWNER).json()["role"] == "owner"


def test_a_disabled_reseller_cannot_sign_in(owner):
    rs = make_reseller(owner)
    owner.patch(f"/api/resellers/{rs['id']}", json={"enabled": False})
    assert TestClient(main.app).post("/api/login", json=SELLER).status_code == 401


def test_disabling_kills_a_live_session(owner):
    """Not just the next login — the cookie already in their browser dies."""
    rs = make_reseller(owner)
    seller = login_as(SELLER)
    assert seller.get("/api/inbounds").status_code == 200

    owner.patch(f"/api/resellers/{rs['id']}", json={"enabled": False})
    assert seller.get("/api/inbounds").status_code == 401


def test_changing_the_password_kills_a_live_session(owner):
    rs = make_reseller(owner)
    seller = login_as(SELLER)
    assert seller.get("/api/inbounds").status_code == 200

    owner.patch(f"/api/resellers/{rs['id']}", json={"password": "a-brand-new-pass"})
    assert seller.get("/api/inbounds").status_code == 401
    assert TestClient(main.app).post("/api/login", json=SELLER).status_code == 401
    assert TestClient(main.app).post(
        "/api/login", json={"username": SELLER["username"],
                            "password": "a-brand-new-pass"}).status_code == 200


def test_deleting_the_reseller_kills_a_live_session(owner):
    rs = make_reseller(owner)
    seller = login_as(SELLER)
    owner.delete(f"/api/resellers/{rs['id']}")
    assert seller.get("/api/inbounds").status_code == 401


def test_me_reports_the_role_and_quota(owner):
    make_reseller(owner, max_users=10, max_traffic_gb=100)
    seller = login_as(SELLER)
    me = seller.get("/api/me").json()
    assert me["role"] == "reseller"
    assert me["quota"] == {"max_users": 10, "max_traffic_gb": 100}
    assert me["usage"]["users"] == 0


def test_a_reseller_is_not_shown_panel_settings(owner):
    """The settings blob carries bot tokens and the OTA repo."""
    make_reseller(owner)
    assert login_as(SELLER).get("/api/me").json()["settings"] == {}
    assert owner.get("/api/me").json()["settings"]


# ------------------------------------------------------------------ isolation
def test_a_reseller_only_sees_its_own_users(owner):
    make_reseller(owner)
    seller = login_as(SELLER)

    owners_user = make_user(owner, "OwnersCustomer")
    sellers_user = make_user(seller, "SellersCustomer")

    visible = [ib["uid"] for ib in seller.get("/api/inbounds").json()["inbounds"]]
    assert visible == [sellers_user["uid"]]
    assert owners_user["uid"] not in visible


def test_the_owner_sees_everything(owner):
    make_reseller(owner)
    seller = login_as(SELLER)
    mine = make_user(owner, "Mine")
    theirs = make_user(seller, "Theirs")

    uids = {ib["uid"] for ib in owner.get("/api/inbounds").json()["inbounds"]}
    assert {mine["uid"], theirs["uid"]} <= uids


def test_two_resellers_cannot_see_each_other(owner):
    make_reseller(owner, SELLER)
    make_reseller(owner, OTHER)
    one, two = login_as(SELLER), login_as(OTHER)

    a = make_user(one, "CustomerOfOne")
    b = make_user(two, "CustomerOfTwo")

    assert [ib["uid"] for ib in one.get("/api/inbounds").json()["inbounds"]] == [a["uid"]]
    assert [ib["uid"] for ib in two.get("/api/inbounds").json()["inbounds"]] == [b["uid"]]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/inbounds/{uid}/links"),
    ("get", "/api/inbounds/{uid}/qr"),
    ("get", "/api/inbounds/{uid}/history"),
    ("get", "/api/inbounds/{uid}/sub"),
    ("patch", "/api/inbounds/{uid}"),
    ("post", "/api/inbounds/{uid}/renew"),
    ("post", "/api/inbounds/{uid}/reset-usage"),
    ("post", "/api/inbounds/{uid}/regenerate"),
    ("post", "/api/inbounds/{uid}/rotate-link"),
    ("delete", "/api/inbounds/{uid}"),
])
def test_a_stranger_uid_is_a_404_everywhere(owner, method, path):
    """404 rather than 403 on purpose: telling a reseller "that exists but is
    not yours" would let them enumerate which uids are in use."""
    make_reseller(owner)
    seller = login_as(SELLER)
    victim = make_user(owner, "NotYours")

    url = path.format(uid=victim["uid"])
    # renew validates its body before it ever looks at the uid, so it needs a
    # valid one here or the 400 would mask the check under test.
    body = {"days": 30} if url.endswith("/renew") else {"name": "Hijacked"}
    kwargs = {"json": body} if method in ("patch", "post") else {}
    r = getattr(seller, method)(url, **kwargs)
    assert r.status_code == 404, f"{method} {url} -> {r.status_code}"


def test_a_refused_delete_leaves_the_user_alone(owner):
    make_reseller(owner)
    seller = login_as(SELLER)
    victim = make_user(owner, "Survivor")

    assert seller.delete(f"/api/inbounds/{victim['uid']}").status_code == 404
    assert main.inbound_by_uid(main.store.get_sync(), victim["uid"]) is not None


def test_bulk_action_cannot_reach_across(owner):
    """The dangerous shape: a valid request with somebody else's uid in the list."""
    make_reseller(owner)
    seller = login_as(SELLER)
    mine = make_user(seller, "Mine")
    victim = make_user(owner, "Victim")

    r = seller.post("/api/inbounds/bulk-action",
                    json={"uids": [mine["uid"], victim["uid"]], "action": "disable"})
    assert r.status_code == 200
    assert r.json()["affected"] == 1

    db = main.store.get_sync()
    assert main.inbound_by_uid(db, victim["uid"])["enabled"] is True
    assert main.inbound_by_uid(db, mine["uid"])["enabled"] is False


def test_bulk_action_with_only_foreign_uids_is_a_404(owner):
    make_reseller(owner)
    seller = login_as(SELLER)
    victim = make_user(owner, "Victim")
    r = seller.post("/api/inbounds/bulk-action",
                    json={"uids": [victim["uid"]], "action": "delete"})
    assert r.status_code == 404
    assert main.inbound_by_uid(main.store.get_sync(), victim["uid"]) is not None


def test_stats_are_scoped_to_the_caller(owner):
    make_reseller(owner)
    seller = login_as(SELLER)
    make_user(owner, "A")
    make_user(owner, "B")
    make_user(seller, "C")

    assert owner.get("/stats").json()["inbounds_count"] == 3
    assert seller.get("/stats").json()["inbounds_count"] == 1


def test_a_reseller_gets_no_panel_wide_traffic_history(owner):
    """The hourly series is aggregated across every user on the panel."""
    make_reseller(owner)
    assert login_as(SELLER).get("/stats").json()["hourly"] == []


# ------------------------------------------------------------------ owner-only
OWNER_ONLY = [
    ("get", "/api/endpoints"),
    ("post", "/api/settings"),
    ("get", "/api/backup"),
    ("get", "/api/2fa/status"),
    ("get", "/api/login-log"),
    ("get", "/api/notify/status"),
    ("get", "/api/userbot"),
    ("get", "/api/ota/check"),
    ("get", "/api/cleanup"),
    ("get", "/api/resellers"),
    ("post", "/api/resellers"),
    ("post", "/api/plans"),
    ("post", "/api/endpoints"),
    ("post", "/api/logout-all"),
]


@pytest.mark.parametrize("method,path", OWNER_ONLY)
def test_panel_configuration_is_owner_only(owner, method, path):
    make_reseller(owner)
    seller = login_as(SELLER)
    kwargs = {"json": {}} if method == "post" else {}
    r = getattr(seller, method)(path, **kwargs)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_403_not_401_so_the_ui_does_not_loop_to_login(owner):
    make_reseller(owner)
    r = login_as(SELLER).get("/api/backup")
    assert r.status_code == 403
    assert r.json()["detail"] == "owner-only"


def test_resellers_can_read_plans_for_bulk_creation(owner):
    """Reading is allowed; the plan editor is not."""
    owner.post("/api/plans", json={"name": "30d", "days": 30, "quota_gb": 50})
    make_reseller(owner)
    seller = login_as(SELLER)
    assert len(seller.get("/api/plans").json()["plans"]) == 1
    assert seller.post("/api/plans", json={"name": "mine"}).status_code == 403


# ------------------------------------------------------------------ quotas
def test_the_user_quota_stops_creation(owner):
    make_reseller(owner, max_users=2)
    seller = login_as(SELLER)
    make_user(seller, "One")
    make_user(seller, "Two")

    r = seller.post("/api/inbounds", json={"name": "Three"})
    assert r.status_code == 403
    assert r.json()["detail"] == "reseller-user-limit"


def test_a_bulk_run_over_quota_creates_nothing(owner):
    """All-or-nothing: a half-applied bulk would leave the seller unsure what
    they actually sold."""
    make_reseller(owner, max_users=5)
    seller = login_as(SELLER)

    r = seller.post("/api/inbounds/bulk", json={"count": 10, "prefix": "batch"})
    assert r.status_code == 403
    assert seller.get("/api/inbounds").json()["inbounds"] == []


def test_a_bulk_run_inside_quota_succeeds(owner):
    make_reseller(owner, max_users=5)
    seller = login_as(SELLER)
    r = seller.post("/api/inbounds/bulk", json={"count": 5, "prefix": "batch"})
    assert r.status_code == 200
    assert r.json()["created"] == 5


def test_the_traffic_quota_stops_creation(owner):
    make_reseller(owner, max_traffic_gb=1)
    seller = login_as(SELLER)
    ib = make_user(seller, "Heavy")

    main.inbound_by_uid(main.store.get_sync(), ib["uid"])["used_down"] = 2 * 1024 ** 3

    r = seller.post("/api/inbounds", json={"name": "Another"})
    assert r.status_code == 403
    assert r.json()["detail"] == "reseller-traffic-limit"
    assert owner.get("/api/resellers").json()["resellers"][0]["used_gb"] == 2.0


def test_zero_means_unlimited(owner):
    make_reseller(owner, max_users=0, max_traffic_gb=0)
    seller = login_as(SELLER)
    for i in range(6):
        make_user(seller, f"U{i}")
    assert len(seller.get("/api/inbounds").json()["inbounds"]) == 6


def test_the_owner_is_not_subject_to_reseller_quotas(owner):
    make_reseller(owner, max_users=1)
    for i in range(4):
        make_user(owner, f"O{i}")
    assert len(owner.get("/api/inbounds").json()["inbounds"]) == 4


# ------------------------------------------------------------------ deletion
def test_deleting_a_reseller_hands_its_users_to_the_owner(owner):
    """The default, because closing a sales account must not silently cut off
    the customers that account already sold to."""
    rs = make_reseller(owner)
    seller = login_as(SELLER)
    ib = make_user(seller, "PaidCustomer")

    r = owner.delete(f"/api/resellers/{rs['id']}")
    assert r.json()["users"] == 0

    row = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    assert row is not None
    assert row["owner"] is None
    assert ib["uid"] in {i["uid"] for i in owner.get("/api/inbounds").json()["inbounds"]}


def test_deleting_a_reseller_with_its_users_is_opt_in(owner):
    rs = make_reseller(owner)
    seller = login_as(SELLER)
    ib = make_user(seller, "GoneToo")

    r = owner.delete(f"/api/resellers/{rs['id']}?delete_users=1")
    assert r.json()["users"] == 1
    assert main.inbound_by_uid(main.store.get_sync(), ib["uid"]) is None


def test_deleting_an_unknown_reseller_is_a_404(owner):
    assert owner.delete("/api/resellers/deadbeefdeadbeef").status_code == 404


# ------------------------------------------------------------------ own credentials
def test_a_reseller_changes_its_own_password(owner):
    """Not the owner's — the account row it checks and writes has to be its own."""
    make_reseller(owner)
    seller = login_as(SELLER)

    r = seller.post("/api/change-password",
                    json={"old_password": SELLER["password"],
                          "new_password": "chosen-by-the-seller"})
    assert r.status_code == 200, r.text

    # the owner's login is untouched
    assert TestClient(main.app).post("/api/login", json=OWNER).status_code == 200
    assert TestClient(main.app).post(
        "/api/login", json={"username": SELLER["username"],
                            "password": "chosen-by-the-seller"}).status_code == 200

    # and the session that made the change keeps working
    assert seller.get("/api/inbounds").status_code == 200


def test_a_reseller_cannot_change_the_password_it_does_not_know(owner):
    make_reseller(owner)
    seller = login_as(SELLER)
    r = seller.post("/api/change-password",
                    json={"old_password": OWNER["password"], "new_password": "hijacked-123"})
    assert r.status_code == 401
    assert TestClient(main.app).post("/api/login", json=OWNER).status_code == 200


def test_a_reseller_cannot_rename_itself(owner):
    make_reseller(owner)
    seller = login_as(SELLER)
    r = seller.post("/api/change-password",
                    json={"old_password": SELLER["password"], "new_username": "promoted"})
    assert r.status_code == 403
    assert r.json()["detail"] == "owner-only"


def test_the_owner_can_still_rename_itself(owner):
    make_reseller(owner)
    r = owner.post("/api/change-password",
                   json={"old_password": OWNER["password"], "new_username": "rsowner2"})
    assert r.status_code == 200
    owner.post("/api/change-password",
               json={"old_password": OWNER["password"], "new_username": OWNER["username"]})
    assert TestClient(main.app).post("/api/login", json=OWNER).status_code == 200


def test_the_owner_cannot_take_a_resellers_name(owner):
    make_reseller(owner)
    r = owner.post("/api/change-password",
                   json={"old_password": OWNER["password"], "new_username": SELLER["username"]})
    assert r.status_code == 400
    assert r.json()["detail"] == "username-taken"


# ------------------------------------------------------------------ migration
def test_existing_users_belong_to_the_owner_after_upgrade():
    """Everything created before resellers existed predates the concept, so it
    has to land with the owner rather than with nobody."""
    import storage
    db = {"schema_version": 11,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64,
          "inbounds": [{"uid": "abc123", "uuid": "u-1", "name": "Old"}]}
    storage.normalize_db(db)
    assert db["inbounds"][0]["owner"] is None
    assert db["resellers"] == []


def test_junk_reseller_rows_are_dropped_on_load():
    import storage
    db = {"schema_version": 12,
          "admin": {"username": "a", "password_hash": "x", "salt": "y"},
          "secret_key": "k" * 64,
          "inbounds": [],
          "resellers": [{"id": "r1", "username": "ok"}, {"nonsense": True}, "string"]}
    storage.normalize_db(db)
    assert [r["id"] for r in db["resellers"]] == ["r1"]
