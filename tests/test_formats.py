"""
Clash and sing-box subscription formats.

The output is parsed with real YAML/JSON parsers here, not string-matched:
a subscription that a client refuses to load is worse than no subscription,
and hand-emitted YAML is exactly where quoting bugs hide.

PyYAML is a test-only dependency — the app emits YAML directly so it keeps
its "no extra runtime deps" property.
"""
import base64
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import subscription as sub

ADMIN = {"username": "fmtadmin", "password": "correct horse battery"}


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


@pytest.fixture(autouse=True)
def clean(client):
    r = client.get("/api/endpoints")
    assert r.status_code == 200, r.text
    for ep in r.json()["endpoints"]:
        client.delete(f"/api/endpoints/{ep['id']}")
    yield


def make_user(client, **kw):
    payload = {"name": "fmtuser", "quota_gb": 10}
    payload.update(kw)
    return client.post("/api/inbounds", json=payload).json()["inbound"]


def add_ep(client, **kw):
    payload = {"name": "Germany", "address": "de.example.com"}
    payload.update(kw)
    r = client.post("/api/endpoints", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["endpoint"]


# ------------------------------------------------------------------ detection
@pytest.mark.parametrize("ua,expected", [
    ("clash-verge/1.5", sub.FORMAT_CLASH),
    ("ClashforWindows/0.20", sub.FORMAT_CLASH),
    ("mihomo/1.18", sub.FORMAT_CLASH),
    ("Stash/2.0", sub.FORMAT_CLASH),
    ("sing-box 1.9.0", sub.FORMAT_SINGBOX),
    ("SFI/1.9.0 (io.nekohasekai.sfa)", sub.FORMAT_SINGBOX),
    ("Karing/1.0", sub.FORMAT_SINGBOX),
    # 1.4.1 deliberately moved these off base64 onto plain text to fix a
    # parsing problem; sniffing must not quietly undo that.
    ("v2rayN/6.23", sub.FORMAT_PLAIN),
    ("v2rayNG/1.8.5", sub.FORMAT_PLAIN),
    ("Shadowrocket/1.9", sub.FORMAT_PLAIN),
    ("curl/8.0", sub.FORMAT_PLAIN),
    ("", sub.FORMAT_PLAIN),
])
def test_user_agent_detection(ua, expected):
    assert sub.detect_format(None, ua) == expected


@pytest.mark.parametrize("explicit,expected", [
    ("clash", sub.FORMAT_CLASH), ("clash-meta", sub.FORMAT_CLASH),
    ("meta", sub.FORMAT_CLASH),
    ("singbox", sub.FORMAT_SINGBOX), ("sing-box", sub.FORMAT_SINGBOX),
    ("base64", sub.FORMAT_BASE64), ("b64", sub.FORMAT_BASE64),
    ("v2ray", sub.FORMAT_PLAIN), ("plain", sub.FORMAT_PLAIN), ("txt", sub.FORMAT_PLAIN),
    ("CLASH", sub.FORMAT_CLASH), ("  clash  ", sub.FORMAT_CLASH),
])
def test_explicit_format_overrides_user_agent(explicit, expected):
    # A deliberate choice must win over whatever the client claims to be.
    assert sub.detect_format(explicit, "sing-box 1.9") == expected
    assert sub.detect_format(explicit, "curl/8.0") == expected


def test_unknown_explicit_format_falls_back_to_user_agent():
    """A typo in ?format= must not downgrade a client that would have been
    detected correctly — fall through to sniffing rather than forcing plain."""
    assert sub.detect_format("nonsense", "clash-verge/1.5") == sub.FORMAT_CLASH
    assert sub.detect_format("nonsense", "curl/8.0") == sub.FORMAT_PLAIN
    assert sub.detect_format("", "clash-verge/1.5") == sub.FORMAT_CLASH
    assert sub.detect_format(None, "") == sub.FORMAT_PLAIN


# ------------------------------------------------------------------ clash
def test_clash_output_is_valid_yaml(client):
    ib = make_user(client, name="Ali")
    add_ep(client, name="Germany", address="de.example.com", sort=1)
    add_ep(client, name="Finland", address="fi.example.com", port=2053, sort=2)

    r = client.get(f"/sub/{ib['uid']}?format=clash")
    assert r.status_code == 200
    assert r.headers["X-Subscription-Format"] == "clash"
    assert "yaml" in r.headers["content-type"]

    doc = yaml.safe_load(r.text)
    assert len(doc["proxies"]) == 2
    for p in doc["proxies"]:
        assert p["type"] == "vless"
        assert p["network"] == "ws"
        assert p["tls"] is True
        assert p["uuid"] == ib["uuid"]
        assert p["ws-opts"]["path"] == f"/ws/{ib['uid']}"

    assert doc["proxies"][0]["server"] == "de.example.com"
    assert doc["proxies"][1]["server"] == "fi.example.com"
    assert doc["proxies"][1]["port"] == 2053


def test_clash_group_covers_every_endpoint(client):
    """The point of multi-location: the client latency-tests all routes and
    moves off a blocked one on its own."""
    ib = make_user(client)
    add_ep(client, name="A", address="a.example.com", sort=1)
    add_ep(client, name="B", address="b.example.com", sort=2)
    add_ep(client, name="C", address="c.example.com", sort=3)

    doc = yaml.safe_load(client.get(f"/sub/{ib['uid']}?format=clash").text)
    groups = {g["name"]: g for g in doc["proxy-groups"]}
    proxy_names = [p["name"] for p in doc["proxies"]]

    assert groups["Auto"]["type"] == "url-test"
    assert groups["Auto"]["proxies"] == proxy_names
    assert groups["Select"]["proxies"] == ["Auto"] + proxy_names

    # The catch-all rule must reference the group by bare name — quoting it
    # makes Clash look for a group whose name includes the quotes.
    assert "MATCH,Select" in doc["rules"]


def test_clash_survives_persian_and_special_names(client):
    ib = make_user(client, name="علی رضایی")
    add_ep(client, name='آلمان "یک": CF', address="de.example.com")

    doc = yaml.safe_load(client.get(f"/sub/{ib['uid']}?format=clash").text)
    name = doc["proxies"][0]["name"]
    assert "علی رضایی" in name
    assert 'آلمان "یک": CF' in name
    # and the group references the identical string
    groups = {g["name"]: g for g in doc["proxy-groups"]}
    assert groups["Auto"]["proxies"] == [name]


def test_clash_deduplicates_proxy_names(client):
    """Two endpoints named the same would otherwise silently collapse."""
    ib = make_user(client, name="Ali")
    add_ep(client, name="Same", address="a.example.com", sort=1)
    add_ep(client, name="Same", address="b.example.com", sort=2)

    doc = yaml.safe_load(client.get(f"/sub/{ib['uid']}?format=clash").text)
    names = [p["name"] for p in doc["proxies"]]
    assert len(set(names)) == 2, f"names collided: {names}"


def test_clash_host_header_for_bare_ip(client):
    ib = make_user(client)
    add_ep(client, name="CF", address="104.16.1.1", host="panel.example.com")
    doc = yaml.safe_load(client.get(f"/sub/{ib['uid']}?format=clash").text)
    p = doc["proxies"][0]
    assert p["server"] == "104.16.1.1"
    assert p["ws-opts"]["headers"]["Host"] == "panel.example.com"
    assert p["servername"] == "panel.example.com"


# ------------------------------------------------------------------ sing-box
def test_singbox_output_is_valid_json(client):
    ib = make_user(client, name="Ali")
    add_ep(client, name="Germany", address="de.example.com", sort=1)
    add_ep(client, name="Finland", address="fi.example.com", sort=2)

    r = client.get(f"/sub/{ib['uid']}?format=singbox")
    assert r.status_code == 200
    assert r.headers["X-Subscription-Format"] == "singbox"
    assert "json" in r.headers["content-type"]

    doc = json.loads(r.text)
    vless = [o for o in doc["outbounds"] if o["type"] == "vless"]
    assert len(vless) == 2
    for o in vless:
        assert o["uuid"] == ib["uuid"]
        assert o["tls"]["enabled"] is True
        assert o["tls"]["utls"]["enabled"] is True
        assert o["transport"]["type"] == "ws"
        assert o["transport"]["path"] == f"/ws/{ib['uid']}"


def test_singbox_urltest_and_selector_reference_real_tags(client):
    """A tag typo here produces a config sing-box refuses to start."""
    ib = make_user(client)
    add_ep(client, name="A", address="a.example.com", sort=1)
    add_ep(client, name="B", address="b.example.com", sort=2)

    doc = json.loads(client.get(f"/sub/{ib['uid']}?format=singbox").text)
    tags = {o["tag"] for o in doc["outbounds"]}
    urltest = next(o for o in doc["outbounds"] if o["type"] == "urltest")
    selector = next(o for o in doc["outbounds"] if o["type"] == "selector")

    assert set(urltest["outbounds"]) <= tags
    assert set(selector["outbounds"]) <= tags
    assert selector["default"] in tags
    assert doc["route"]["final"] in tags


def test_singbox_random_fingerprint_is_mapped(client):
    """utls has no "random" fingerprint; sending it fails validation."""
    ib = make_user(client, fp="random")
    add_ep(client, name="A", address="a.example.com")
    doc = json.loads(client.get(f"/sub/{ib['uid']}?format=singbox").text)
    o = next(x for x in doc["outbounds"] if x["type"] == "vless")
    assert o["tls"]["utls"]["fingerprint"] != "random"


def test_singbox_persian_names(client):
    ib = make_user(client, name="علی")
    add_ep(client, name="آلمان", address="de.example.com")
    doc = json.loads(client.get(f"/sub/{ib['uid']}?format=singbox").text)
    tags = [o["tag"] for o in doc["outbounds"] if o["type"] == "vless"]
    assert any("علی" in t and "آلمان" in t for t in tags)


# ------------------------------------------------------------------ base64 / plain
def test_base64_decodes_to_the_plain_list(client):
    ib = make_user(client)
    add_ep(client, name="A", address="a.example.com", sort=1)
    add_ep(client, name="B", address="b.example.com", sort=2)

    plain = client.get(f"/sub/{ib['uid']}?format=plain").text
    encoded = client.get(f"/sub/{ib['uid']}?format=base64").text
    assert base64.b64decode(encoded).decode("utf-8") == plain
    assert plain.count("vless://") == 2


def test_base64_is_opt_in_only(client):
    """Reachable by explicit request, never by sniffing."""
    ib = make_user(client)
    r = client.get(f"/sub/{ib['uid']}", headers={"User-Agent": "v2rayNG/1.8.5"})
    assert r.headers["X-Subscription-Format"] == "plain"
    r = client.get(f"/sub/{ib['uid']}?format=base64")
    assert r.headers["X-Subscription-Format"] == "base64"


def test_plain_remains_the_default(client):
    """Existing subscriptions must not change dialect under anyone's feet."""
    ib = make_user(client)
    add_ep(client, name="A", address="a.example.com")
    r = client.get(f"/sub/{ib['uid']}")
    assert r.headers["X-Subscription-Format"] == "plain"
    assert r.text.startswith("vless://")


def test_user_agent_switches_format_without_a_query_param(client):
    ib = make_user(client)
    add_ep(client, name="A", address="a.example.com")

    r = client.get(f"/sub/{ib['uid']}", headers={"User-Agent": "clash-verge/1.5"})
    assert r.headers["X-Subscription-Format"] == "clash"
    assert yaml.safe_load(r.text)["proxies"]

    r = client.get(f"/sub/{ib['uid']}", headers={"User-Agent": "SFI/1.9"})
    assert r.headers["X-Subscription-Format"] == "singbox"
    assert json.loads(r.text)["outbounds"]


# ------------------------------------------------------------------ shared behaviour
@pytest.mark.parametrize("fmt", ["plain", "base64", "clash", "singbox"])
def test_every_format_carries_userinfo_and_survives_no_endpoints(client, fmt):
    ib = make_user(client, name="Ali", quota_gb=7)
    r = client.get(f"/sub/{ib['uid']}?format={fmt}")
    assert r.status_code == 200
    assert f"total={7 * 1024 ** 3}" in r.headers["Subscription-Userinfo"]
    assert r.text.strip()


@pytest.mark.parametrize("fmt", ["plain", "base64", "clash", "singbox"])
def test_unknown_uid_404s_in_every_format(client, fmt):
    assert client.get(f"/sub/deadbeefdeadbeef?format={fmt}").status_code == 404


@pytest.mark.parametrize("fmt,ext", [
    ("plain", "txt"), ("base64", "txt"), ("clash", "yaml"), ("singbox", "json"),
])
def test_filename_extension_matches_format(client, fmt, ext):
    ib = make_user(client)
    r = client.get(f"/sub/{ib['uid']}?format={fmt}")
    assert f'.{ext}"' in r.headers["Content-Disposition"]
