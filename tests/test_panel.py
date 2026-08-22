"""
Peyk smoke tests.

Covers the flows that used to break silently: input validation on inbounds,
quota/expiry status, the subscription header, link generation, the SSRF guard
and the VLESS header parser.

Run with:   python -m pytest tests -q
       or:  python tests/test_panel.py
"""
import asyncio
import struct

# conftest.py points STANNG_DATA_DIR at a throwaway directory before storage is
# imported, so the real data/db.json is never touched by a test run.
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import reset_panel  # noqa: E402

import main  # noqa: E402
import vless_engine  # noqa: E402

ADMIN = {"username": "adminuser", "password": "correct horse battery"}


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        yield c


# ------------------------------------------------------------------ auth
def test_setup_rejects_weak_password_and_bad_username():
    reset_panel()
    with TestClient(main.app) as c:
        assert c.post("/api/setup", json={"username": "ok", "password": "longenough"}).status_code == 400
        assert c.post("/api/setup", json={"username": "gooduser", "password": "short"}).status_code == 400


def test_login_required_for_admin_endpoints():
    reset_panel()
    with TestClient(main.app) as c:
        for path in ("/api/inbounds", "/stats", "/api/backup", "/api/ota/check"):
            assert c.get(path).status_code == 401, path


def test_login_and_session(client):
    assert client.post("/api/login", json=ADMIN).status_code == 200
    me = client.get("/api/me").json()
    assert me["logged_in"] is True
    assert me["panel_name"] == "TestPanel"


def test_wrong_password_rejected(client):
    r = client.post("/api/login", json={"username": ADMIN["username"], "password": "nope-wrong"})
    assert r.status_code == 401


def test_security_headers_present(client):
    r = client.get("/login")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]


# ------------------------------------------------------------------ inbounds
def _make(client, **kw):
    payload = {"name": "Tester", "quota_gb": 1, "expire_days": 30}
    payload.update(kw)
    r = client.post("/api/inbounds", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["inbound"]


def test_create_and_list(client):
    ib = _make(client, name="Alice")
    assert ib["name"] == "Alice"
    assert ib["status"]["live_enabled"] is True
    uids = [i["uid"] for i in client.get("/api/inbounds").json()["inbounds"]]
    assert ib["uid"] in uids


def test_patch_rejects_non_numeric_quota(client):
    """The old build stored "abc" and then 500'd on every later listing."""
    ib = _make(client)
    r = client.patch(f"/api/inbounds/{ib['uid']}", json={"quota_gb": "abc"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid-quota_gb"
    # the listing still works
    assert client.get("/api/inbounds").status_code == 200


def test_create_rejects_out_of_range(client):
    assert client.post("/api/inbounds", json={"name": "x", "quota_gb": -5}).status_code == 400
    assert client.post("/api/inbounds", json={"name": "x", "expire_days": 99999}).status_code == 400
    assert client.post("/api/inbounds", json={"name": "x", "fp": "nonsense"}).status_code == 400


def test_editing_an_old_user_does_not_expire_them(client):
    """Renewing used to recompute expiry from created_at, instantly expiring
    anyone whose account was older than the new day count."""
    ib = _make(client, expire_days=30)
    uid = ib["uid"]

    db = main.store.get_sync()
    rec = main.inbound_by_uid(db, uid)
    rec["created_at"] -= 400 * 86400  # pretend the account is over a year old

    # Saving the row without touching expire_days must not move the expiry.
    before = rec["expire_at"]
    client.patch(f"/api/inbounds/{uid}", json={"name": "Renamed", "expire_days": 30})
    assert main.inbound_by_uid(main.store.get_sync(), uid)["expire_at"] == before

    # Changing it restarts the window from now, not from created_at.
    r = client.patch(f"/api/inbounds/{uid}", json={"expire_days": 10})
    assert r.status_code == 200
    assert r.json()["inbound"]["status"]["expired"] is False
    assert r.json()["inbound"]["status"]["days_left"] in (9, 10)


def test_renew_extends_from_existing_expiry(client):
    ib = _make(client, expire_days=5)
    uid = ib["uid"]
    r = client.post(f"/api/inbounds/{uid}/renew", json={"days": 30, "reset_usage": True})
    assert r.status_code == 200
    assert r.json()["inbound"]["status"]["days_left"] >= 34


def test_renew_from_now_when_already_expired(client):
    ib = _make(client, expire_days=1)
    uid = ib["uid"]
    rec = main.inbound_by_uid(main.store.get_sync(), uid)
    rec["expire_at"] = 1000.0  # long past
    assert main.inbound_status(rec)["expired"] is True

    r = client.post(f"/api/inbounds/{uid}/renew", json={"days": 7})
    assert r.json()["inbound"]["status"]["expired"] is False
    assert r.json()["inbound"]["status"]["days_left"] in (6, 7)


def test_quota_exceeded_disables_user(client):
    ib = _make(client, quota_gb=1)
    rec = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    rec["used_down"] = 2 * 1024 ** 3
    st = main.inbound_status(rec)
    assert st["quota_exceeded"] is True
    assert st["live_enabled"] is False


def test_pending_traffic_counts_towards_quota(client):
    """Bytes buffered since the last flush must still count, or a fast transfer
    overshoots the limit by everything that arrived in the flush window."""
    ib = _make(client, quota_gb=1)
    uid = ib["uid"]
    main.runtime["pending_traffic"][uid] = {"up": 0, "down": 2 * 1024 ** 3}
    try:
        rec = main.inbound_by_uid(main.store.get_sync(), uid)
        assert main.inbound_status(rec)["quota_exceeded"] is True
    finally:
        main.runtime["pending_traffic"].pop(uid, None)


def test_request_cap(client):
    ib = _make(client, max_requests=2)
    rec = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    rec["request_count"] = 2
    assert main.inbound_status(rec)["request_exceeded"] is True
    assert main.inbound_status(rec)["live_enabled"] is False


def test_delete(client):
    ib = _make(client)
    assert client.delete(f"/api/inbounds/{ib['uid']}").status_code == 200
    assert client.delete(f"/api/inbounds/{ib['uid']}").status_code == 404


def test_regenerate_rotates_uuid(client):
    ib = _make(client)
    old = ib["uuid"]
    new = client.post(f"/api/inbounds/{ib['uid']}/regenerate").json()["inbound"]["uuid"]
    assert new != old


# ------------------------------------------------------------------ subscription
def test_subscription_userinfo_header(client):
    ib = _make(client, quota_gb=2, expire_days=10)
    uid = ib["uid"]
    rec = main.inbound_by_uid(main.store.get_sync(), uid)
    rec["used_up"] = 1000
    rec["used_down"] = 2000

    r = client.get(f"/sub/{ib['sub_token']}")
    assert r.status_code == 200
    info = r.headers["Subscription-Userinfo"]
    assert "upload=1000" in info
    assert "download=2000" in info
    assert f"total={2 * 1024 ** 3}" in info
    assert "expire=" in info
    assert r.text.startswith("vless://")
    # The dummy 127.0.0.1 "info configs" are gone.
    assert "127.0.0.1" not in r.text
    assert "00000000-0000-0000-0000-000000000000" not in r.text


def test_link_contains_expected_params(client):
    ib = _make(client)
    links = client.get(f"/api/inbounds/{ib['uid']}/links").json()
    tls = links["links"]["tls"]
    assert tls.startswith("vless://")
    assert "security=tls" in tls
    assert "type=ws" in tls
    assert f"path=/ws/{ib['uid']}" in tls   # '/' must stay unescaped for v2rayNG
    assert "alpn=http/1.1" in tls
    assert links["sub_url"].endswith(f"/sub/{ib['sub_token']}")


def test_fragment_settings_reach_the_link(client):
    """fragment_* were stored and displayed but never used when building links."""
    ib = _make(client)
    client.post("/api/settings", json={
        "fragment_enabled": True, "fragment_profile": "custom",
        "fragment_packets": "tlshello",
        "fragment_length": "40-60", "fragment_interval": "5-10",
    })
    tls = client.get(f"/api/inbounds/{ib['uid']}/links").json()["links"]["tls"]
    assert "fragment=tlshello%2C40-60%2C5-10" in tls

    client.post("/api/settings", json={"fragment_enabled": False})
    tls = client.get(f"/api/inbounds/{ib['uid']}/links").json()["links"]["tls"]
    assert "fragment=" not in tls
    client.post("/api/settings", json={"fragment_enabled": True,
                                       "fragment_profile": "balanced"})


def test_public_status_reports_reason(client):
    ib = _make(client, quota_gb=1)
    rec = main.inbound_by_uid(main.store.get_sync(), ib["uid"])
    rec["used_down"] = 5 * 1024 ** 3
    body = client.get(f"/api/status/{ib['sub_token']}").json()
    assert body["enabled"] is False
    assert body["reason"] == "quota"


def test_qr_returns_png(client):
    ib = _make(client)
    r = client.get(f"/api/inbounds/{ib['uid']}/qr")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")


def test_unknown_uid_is_404(client):
    assert client.get("/sub/deadbeefdeadbeef").status_code == 404
    assert client.get("/api/status/deadbeefdeadbeef").status_code == 404
    assert client.get("/status/deadbeefdeadbeef").status_code == 404


# ------------------------------------------------------------------ settings
def test_settings_validation(client):
    assert client.post("/api/settings", json={"ota_repo": "not a repo!!"}).status_code == 400
    assert client.post("/api/settings", json={"ota_repo": "owner/name"}).status_code == 200
    # a full GitHub URL is accepted and normalised
    r = client.post("/api/settings", json={"ota_repo": "https://github.com/owner/name"})
    assert r.json()["settings"]["ota_repo"] == "owner/name"
    # a bare handle becomes a t.me link
    r = client.post("/api/settings", json={"telegram_contact": "@someone"})
    assert r.json()["settings"]["telegram_contact"] == "https://t.me/someone"


def test_branding_override(client):
    client.post("/api/settings", json={"panel_name": "MyVPN"})
    assert client.get("/api/me").json()["panel_name"] == "MyVPN"
    assert b"MyVPN" in client.get("/login").content
    client.post("/api/settings", json={"panel_name": ""})
    assert client.get("/api/me").json()["panel_name"] == "TestPanel"


def test_backup_and_restore_roundtrip(client):
    ib = _make(client, name="SurvivesRestore")
    backup = client.get("/api/backup").json()
    assert backup["admin"]["username"] == ADMIN["username"]

    client.delete(f"/api/inbounds/{ib['uid']}")
    assert client.get(f"/api/status/{ib['sub_token']}").status_code == 404

    # Restore signs the caller out, so log back in before asserting.
    assert client.post("/api/restore", json={"db": backup}).status_code == 200
    client.post("/api/login", json=ADMIN)
    names = [i["name"] for i in client.get("/api/inbounds").json()["inbounds"]]
    assert "SurvivesRestore" in names


def test_restore_rejects_garbage(client):
    assert client.post("/api/restore", json={"db": {"nope": 1}}).status_code == 400


# ------------------------------------------------------------------ SSRF guard
@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
    "169.254.169.254",  # cloud metadata service
    "100.64.0.1", "::1", "localhost",
])
def test_relay_refuses_internal_destinations(host):
    async def go():
        with pytest.raises(vless_engine.VlessError):
            await vless_engine.resolve_target(host, 80)
    asyncio.run(go())


def test_relay_allows_public_destination():
    async def go():
        infos = await vless_engine.resolve_target("1.1.1.1", 443)
        assert infos
    asyncio.run(go())


def test_allow_private_opt_in():
    async def go():
        infos = await vless_engine.resolve_target("127.0.0.1", 80, allow_private=True)
        assert infos
    asyncio.run(go())


# ------------------------------------------------------------------ VLESS parser
def _vless_request(uuid_hex: str, cmd: int = 1, port: int = 443, host: str = "example.com"):
    body = bytes([0]) + bytes.fromhex(uuid_hex) + bytes([0]) + bytes([cmd])
    body += struct.pack(">H", port)
    body += bytes([2, len(host)]) + host.encode()
    return body


def test_header_parses_domain_target():
    uid = "0123456789abcdef0123456789abcdef"
    h = vless_engine.parse_vless_header(_vless_request(uid) + b"payload")
    assert h is not None
    assert h.addr == "example.com"
    assert h.port == 443
    assert h.cmd == vless_engine.CMD_TCP


def test_header_rejects_mux_and_bad_version():
    uid = "0123456789abcdef0123456789abcdef"
    assert vless_engine.parse_vless_header(_vless_request(uid, cmd=3) + b"x") is None
    bad = bytearray(_vless_request(uid) + b"x")
    bad[0] = 9  # wrong protocol version
    assert vless_engine.parse_vless_header(bytes(bad)) is None


def test_header_rejects_truncated_input():
    uid = "0123456789abcdef0123456789abcdef"
    full = _vless_request(uid) + b"payload"
    for cut in range(1, len(full)):
        vless_engine.parse_vless_header(full[:cut])  # must not raise


def test_header_rejects_port_zero():
    uid = "0123456789abcdef0123456789abcdef"
    assert vless_engine.parse_vless_header(_vless_request(uid, port=0) + b"x") is None


def test_udp_packet_framing():
    buf = bytearray(struct.pack(">H", 3) + b"abc" + struct.pack(">H", 2) + b"de" + b"\x00")
    assert list(vless_engine._iter_udp_packets(buf)) == [b"abc", b"de"]
    assert bytes(buf) == b"\x00"  # partial tail is preserved


# ------------------------------------------------------------------ proxy trust
def test_forwarded_for_uses_trusted_hop(monkeypatch):
    """With one proxy in front, the client-supplied leftmost value must lose."""
    class H:
        def __init__(self, v): self._v = v
        def get(self, k): return self._v if k == "x-forwarded-for" else None

    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 1)
    assert main._forwarded_for(H("1.2.3.4")) == "1.2.3.4"
    # attacker forges "9.9.9.9"; the real proxy appends the true address
    assert main._forwarded_for(H("9.9.9.9, 1.2.3.4")) == "1.2.3.4"

    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 2)
    assert main._forwarded_for(H("9.9.9.9, 1.2.3.4, 10.0.0.1")) == "1.2.3.4"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ------------------------------------------------------------------ host / port
@pytest.mark.parametrize("raw,expected", [
    ("example.com", ("example.com", None)),
    ("example.com:8443", ("example.com", "8443")),
    ("https://example.com:8443/", ("example.com", "8443")),
    ("http://example.com/path", ("example.com", None)),
    ("[::1]:8000", ("::1", "8000")),
    ("[2606:4700::1111]", ("2606:4700::1111", None)),
    ("2606:4700:4700::1111", ("2606:4700:4700::1111", None)),
    ("a.com, b.com", ("a.com", None)),
    ("", ("", None)),
])
def test_split_host_port(raw, expected):
    assert main._split_host_port(raw) == expected


def test_public_domain_with_port_does_not_leak_into_vless_host(client):
    """A port in public_domain must reach the sub URL but never the VLESS host,
    which would produce `@host:8443:443` and break the config."""
    ib = _make(client)
    client.post("/api/settings", json={"public_domain": "panel.example.com:8443"})
    try:
        r = client.get(f"/api/inbounds/{ib['uid']}/links").json()
        assert "@panel.example.com:443?" in r["links"]["tls"]
        assert "8443:443" not in r["links"]["tls"]
        # scheme follows the request (no x-forwarded-proto here), the port survives
        assert r["sub_url"] == f"http://panel.example.com:8443/sub/{ib['sub_token']}"
    finally:
        client.post("/api/settings", json={"public_domain": ""})


def test_public_domain_strips_scheme(client):
    ib = _make(client)
    client.post("/api/settings", json={"public_domain": "https://vpn.example.com/"})
    try:
        r = client.get(f"/api/inbounds/{ib['uid']}/links").json()
        assert "@vpn.example.com:443?" in r["links"]["tls"]
        assert r["sub_url"] == f"http://vpn.example.com/sub/{ib['sub_token']}"
    finally:
        client.post("/api/settings", json={"public_domain": ""})


def test_userinfo_includes_unflushed_traffic(client):
    ib = _make(client, quota_gb=5)
    uid = ib["uid"]
    main.runtime["pending_traffic"][uid] = {"up": 111, "down": 222}
    try:
        info = client.get(f"/sub/{ib['sub_token']}").headers["Subscription-Userinfo"]
        assert "upload=111" in info
        assert "download=222" in info
    finally:
        main.runtime["pending_traffic"].pop(uid, None)


def test_forwarded_proto_drives_link_scheme(client):
    """Behind a TLS-terminating proxy the links must come back as https."""
    ib = _make(client)
    r = client.get(f"/api/inbounds/{ib['uid']}/links",
                   headers={"x-forwarded-proto": "https", "x-forwarded-host": "vpn.example.com"}).json()
    assert r["sub_url"] == f"https://vpn.example.com/sub/{ib['sub_token']}"
    assert r["status_url"] == f"https://vpn.example.com/status/{ib['sub_token']}"


# ------------------------------------------------------------------ 2.0 rename
def test_legacy_env_names_still_work(monkeypatch):
    """2.0 renamed STANNG_* to PEYK_*. The old names must keep working: a
    deployment with STANNG_DATA_DIR set would otherwise come up pointing at an
    empty database the moment it updated."""
    monkeypatch.delenv("PEYK_PANEL_NAME", raising=False)
    monkeypatch.delenv("STANNG_PANEL_NAME", raising=False)
    assert main._env_str("PANEL_NAME", "fallback") == "fallback"

    monkeypatch.setenv("STANNG_PANEL_NAME", "FromLegacy")
    assert main._env_str("PANEL_NAME", "fallback") == "FromLegacy"

    # the new name wins when both are present
    monkeypatch.setenv("PEYK_PANEL_NAME", "FromNew")
    assert main._env_str("PANEL_NAME", "fallback") == "FromNew"


def test_legacy_env_names_work_for_integers(monkeypatch):
    monkeypatch.delenv("PEYK_PROXY_HOPS", raising=False)
    monkeypatch.setenv("STANNG_PROXY_HOPS", "3")
    assert main._env_int("PROXY_HOPS", 1) == 3
    monkeypatch.setenv("PEYK_PROXY_HOPS", "2")
    assert main._env_int("PROXY_HOPS", 1) == 2
    monkeypatch.setenv("PEYK_PROXY_HOPS", "not-a-number")
    assert main._env_int("PROXY_HOPS", 1) == 1


def test_legacy_data_dir_is_honoured():
    """The single most damaging thing the rename could have broken."""
    import storage
    default = storage.resolve_data_dir({})
    assert default.endswith("data")
    assert storage.resolve_data_dir({"STANNG_DATA_DIR": "/legacy"}) == "/legacy"
    assert storage.resolve_data_dir({"PEYK_DATA_DIR": "/new"}) == "/new"
    # new name wins when both are set
    assert storage.resolve_data_dir(
        {"PEYK_DATA_DIR": "/new", "STANNG_DATA_DIR": "/legacy"}) == "/new"


def test_platform_env_vars_are_not_prefixed(monkeypatch):
    """PORT and friends are injected by Railway/Render under plain names.

    Reading them through the PEYK_/STANNG_ lookup made the server bind 8000
    regardless of what the platform asked for, so it came up healthy and
    never received a single request.
    """
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.delenv("PEYK_PORT", raising=False)
    monkeypatch.delenv("STANNG_PORT", raising=False)
    assert main._platform_env("PORT", 8000) == "9123"
    # and the prefixed lookup must NOT see it
    assert main._env_raw("PORT") is None
