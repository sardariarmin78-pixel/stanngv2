"""
Multi-endpoint subscriptions.

Several entry points (extra hostnames or clean CDN IPs) all route to the one
backend, so a single subscription hands the user multiple routes. Against
per-IP blocking that is what actually helps: one edge going dark leaves the
others working.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main

ADMIN = {"username": "epadmin", "password": "correct horse battery"}


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


@pytest.fixture(autouse=True)
def clean_endpoints(client):
    """Each test starts with no entry points configured."""
    r = client.get("/api/endpoints")
    assert r.status_code == 200, f"shared session is broken: {r.status_code} {r.text}"
    for ep in r.json()["endpoints"]:
        client.delete(f"/api/endpoints/{ep['id']}")
    yield


def make_user(client, **kw):
    payload = {"name": "epuser", "quota_gb": 10}
    payload.update(kw)
    return client.post("/api/inbounds", json=payload).json()["inbound"]


def add_ep(client, **kw):
    payload = {"name": "Germany", "address": "de.example.com"}
    payload.update(kw)
    r = client.post("/api/endpoints", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["endpoint"]


# ------------------------------------------------------------------ CRUD
def test_endpoint_crud(client):
    ep = add_ep(client, name="Finland", address="fi.example.com", port=8443)
    assert ep["port"] == 8443
    assert ep["enabled"] is True
    assert ep["health"] == {"ok": None, "ts": None, "latency_ms": None}

    listed = client.get("/api/endpoints").json()["endpoints"]
    assert [e["id"] for e in listed] == [ep["id"]]

    r = client.patch(f"/api/endpoints/{ep['id']}",
                     json={"name": "Finland 2", "address": "fi2.example.com"})
    assert r.json()["endpoint"]["address"] == "fi2.example.com"

    assert client.delete(f"/api/endpoints/{ep['id']}").status_code == 200
    assert client.delete(f"/api/endpoints/{ep['id']}").status_code == 404


def test_endpoint_validation(client):
    assert client.post("/api/endpoints", json={"address": ""}).status_code == 400
    assert client.post("/api/endpoints", json={"address": "bad host!"}).status_code == 400
    assert client.post("/api/endpoints",
                       json={"address": "ok.com", "port": 70000}).status_code == 400
    assert client.post("/api/endpoints",
                       json={"address": "ok.com", "fp": "nope"}).status_code == 400
    assert client.post("/api/endpoints",
                       json={"address": "ok.com", "alpn": "nope"}).status_code == 400
    # bare IPv4 and IPv6 literals are valid entry points
    assert client.post("/api/endpoints", json={"address": "104.16.1.1"}).status_code == 200
    assert client.post("/api/endpoints", json={"address": "2606:4700::1111"}).status_code == 200


# ------------------------------------------------------------------ subscription
def test_no_endpoints_keeps_single_config(client):
    """Existing installs must see no change until they configure endpoints."""
    ib = make_user(client)
    body = client.get(f"/sub/{ib['sub_token']}").text
    assert body.count("vless://") == 1
    assert body.startswith("vless://")


def test_subscription_emits_one_config_per_endpoint(client):
    ib = make_user(client)
    add_ep(client, name="Germany", address="de.example.com", sort=1)
    add_ep(client, name="Finland", address="fi.example.com", sort=2)
    add_ep(client, name="Cloudflare", address="104.16.1.1", sort=3)

    lines = [ln for ln in client.get(f"/sub/{ib['sub_token']}").text.splitlines() if ln.strip()]
    assert len(lines) == 3
    assert all(ln.startswith("vless://") for ln in lines)
    # sort order is respected and each carries its own remark
    assert "Germany" in lines[0] and "de.example.com" in lines[0]
    assert "Finland" in lines[1]
    assert "104.16.1.1" in lines[2]
    # one shared identity across every route
    assert all(ib["uuid"] in ln for ln in lines)
    assert all(f"path=/ws/{ib['uid']}" in ln for ln in lines)


def test_disabled_endpoint_is_excluded(client):
    ib = make_user(client)
    add_ep(client, name="Live", address="live.example.com")
    off = add_ep(client, name="Dead", address="dead.example.com")
    client.patch(f"/api/endpoints/{off['id']}",
                 json={"name": "Dead", "address": "dead.example.com", "enabled": False})

    body = client.get(f"/sub/{ib['sub_token']}").text
    assert "live.example.com" in body
    assert "dead.example.com" not in body


def test_bare_ip_endpoint_keeps_routing_host(client):
    """Dialling a clean CDN IP still has to send the Host the proxy routes on,
    otherwise the request never reaches the backend."""
    ib = make_user(client)
    add_ep(client, name="CF", address="104.16.1.1", host="panel.example.com")

    line = client.get(f"/sub/{ib['sub_token']}").text.strip()
    assert line.startswith("vless://")
    assert "@104.16.1.1:443?" in line
    assert "host=panel.example.com" in line
    # SNI follows the routing host when not overridden
    assert "sni=panel.example.com" in line


def test_per_endpoint_sni_and_port(client):
    ib = make_user(client)
    add_ep(client, name="Fronted", address="cdn.example.com",
           host="panel.example.com", sni="fronting.example.com", port=2053)
    line = client.get(f"/sub/{ib['sub_token']}").text.strip()
    assert "@cdn.example.com:2053?" in line
    assert "sni=fronting.example.com" in line
    assert "host=panel.example.com" in line


def test_per_endpoint_fingerprint_overrides_default(client):
    ib = make_user(client, fp="chrome")
    add_ep(client, name="Edge", address="a.example.com", fp="firefox")
    add_ep(client, name="Plain", address="b.example.com", sort=2)
    lines = client.get(f"/sub/{ib['sub_token']}").text.splitlines()
    assert "fp=firefox" in lines[0]
    assert "fp=chrome" in lines[1]


def test_links_api_returns_all_configs(client):
    ib = make_user(client)
    add_ep(client, name="A", address="a.example.com", sort=1)
    add_ep(client, name="B", address="b.example.com", sort=2)

    r = client.get(f"/api/inbounds/{ib['uid']}/links").json()
    assert len(r["links"]["configs"]) == 2
    assert [c["name"] for c in r["links"]["configs"]] == ["A", "B"]
    # legacy field still points at the primary route
    assert r["links"]["tls"] == r["links"]["configs"][0]["link"]


def test_json_subscription_includes_configs(client):
    ib = make_user(client)
    add_ep(client, name="A", address="a.example.com")
    body = client.get(f"/sub/{ib['sub_token']}/json").json()
    assert len(body["links"]["configs"]) == 1
    assert body["links"]["tls"].startswith("vless://")


def test_userinfo_header_survives_multi_endpoint(client):
    ib = make_user(client, quota_gb=3)
    add_ep(client, name="A", address="a.example.com")
    add_ep(client, name="B", address="b.example.com")
    info = client.get(f"/sub/{ib['sub_token']}").headers["Subscription-Userinfo"]
    assert f"total={3 * 1024 ** 3}" in info


def test_qr_switches_to_subscription_when_multi(client):
    """A single-config QR would pin the user to one route; the subscription
    URL carries them all and keeps refreshing."""
    ib = make_user(client)
    assert client.get(f"/api/inbounds/{ib['uid']}/qr").status_code == 200

    add_ep(client, name="A", address="a.example.com")
    add_ep(client, name="B", address="b.example.com")
    r = client.get(f"/api/inbounds/{ib['uid']}/qr")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")


def test_endpoint_limit(client):
    for i in range(main.MAX_ENDPOINTS):
        add_ep(client, name=f"e{i}", address=f"e{i}.example.com")
    r = client.post("/api/endpoints", json={"address": "one.too.many.com"})
    assert r.status_code == 400
    assert r.json()["detail"] == "endpoint-limit-reached"


def test_endpoint_test_records_health(client):
    """Unreachable host must report a failure rather than raising."""
    ep = add_ep(client, name="Nowhere", address="unreachable.invalid")
    r = client.post(f"/api/endpoints/{ep['id']}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["latency_ms"] is not None
    stored = client.get("/api/endpoints").json()["endpoints"][0]
    assert stored["health"]["ok"] is False
    assert stored["health"]["ts"]


def test_endpoint_test_404(client):
    assert client.post("/api/endpoints/missing/test").status_code == 404


def test_endpoints_require_auth(client):
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.get("/api/endpoints").status_code == 401
        assert c.post("/api/endpoints", json={"address": "x.com"}).status_code == 401
    # reset_panel() wiped the admin this module's client was signed in as.
    client.post("/api/login", json=ADMIN)


# ------------------------------------------------------------------ non-ASCII names
def test_persian_user_name_does_not_break_subscription(client):
    """HTTP headers are latin-1 by spec. Putting the user's name straight into
    Profile-Title turned every Persian-named user's subscription into a 500 —
    which, for a Persian-language panel, is most of them."""
    ib = make_user(client, name="علی رضایی")
    add_ep(client, name="آلمان", address="de.example.com")

    r = client.get(f"/sub/{ib['sub_token']}")
    assert r.status_code == 200
    assert r.text.count("vless://") == 1
    # the header is transported base64-encoded rather than dropped or mangled
    title = r.headers["Profile-Title"]
    assert title.startswith("base64:")
    import base64
    assert "علی رضایی" in base64.b64decode(title[7:]).decode("utf-8")


def test_ascii_name_keeps_plain_title(client):
    ib = make_user(client, name="Ali")
    title = client.get(f"/sub/{ib['sub_token']}").headers["Profile-Title"]
    assert not title.startswith("base64:")
    assert title.endswith("-Ali")


@pytest.mark.parametrize("name", ["علی", "Ali · CF", "日本", "🇩🇪 Frankfurt", "Müller"])
def test_subscription_survives_any_name(client, name):
    ib = make_user(client, name=name)
    add_ep(client, name=name, address="x.example.com")
    r = client.get(f"/sub/{ib['sub_token']}")
    assert r.status_code == 200, f"{name!r} broke the subscription"
    assert r.text.startswith("vless://")
    assert client.get(f"/sub/{ib['sub_token']}/json").status_code == 200
    assert client.get(f"/api/inbounds/{ib['uid']}/qr").status_code == 200


def test_non_ascii_endpoint_name_in_remark(client):
    ib = make_user(client, name="Ali")
    add_ep(client, name="آلمان — کلادفلر", address="de.example.com")
    import urllib.parse
    line = client.get(f"/sub/{ib['sub_token']}").text.strip()
    remark = urllib.parse.unquote(line.split("#")[-1])
    assert "آلمان — کلادفلر" in remark
