"""
Xray-core data plane.

The binary is not installed in CI, so these cover the parts that are pure
logic: the generated config, the stats arithmetic, and the fact that the
panel falls back cleanly to its own relay when xray is absent.

Two of these encode fixes made to the upstream implementation this was
adapted from — the missing outbound restrictions, and restarting xray on
every user change.
"""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main
import xray_manager

ADMIN = {"username": "xrayadmin", "password": "correct horse battery"}

USERS = [
    {"uid": "aaa", "uuid": "11111111-2222-3333-4444-555555555555", "enabled": True},
    {"uid": "bbb", "uuid": "66666666-7777-8888-9999-000000000000", "enabled": True},
    {"uid": "ccc", "uuid": "99999999-9999-9999-9999-999999999999", "enabled": False},
]


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


# ------------------------------------------------------------------ config
def test_config_has_an_inbound_per_transport():
    cfg = xray_manager.build_config(
        USERS, {"xray_transports": ["vless-ws", "vmess-ws", "vless-xhttp"]})
    tags = [i["tag"] for i in cfg["inbounds"]]
    assert "api" in tags
    for expected in ("inbound-vless-ws", "inbound-vmess-ws", "inbound-vless-xhttp"):
        assert expected in tags
    # each proxy inbound listens on loopback only; nginx owns the public port
    for ib in cfg["inbounds"]:
        assert ib["listen"] == "127.0.0.1"


def test_only_selected_transports_are_built():
    cfg = xray_manager.build_config(USERS, {"xray_transports": ["vless-ws"]})
    tags = [i["tag"] for i in cfg["inbounds"]]
    assert "inbound-vless-ws" in tags
    assert "inbound-vmess-ws" not in tags


def test_unknown_transport_falls_back_to_defaults():
    assert xray_manager.enabled_transports({"xray_transports": ["nonsense"]}) \
        == xray_manager.DEFAULT_TRANSPORTS
    assert xray_manager.enabled_transports({}) == xray_manager.DEFAULT_TRANSPORTS
    assert xray_manager.enabled_transports({"xray_transports": "notalist"}) \
        == xray_manager.DEFAULT_TRANSPORTS


def test_disabled_users_are_not_clients():
    cfg = xray_manager.build_config(USERS, {"xray_transports": ["vless-ws"]})
    ws = next(i for i in cfg["inbounds"] if i["tag"] == "inbound-vless-ws")
    emails = [c["email"] for c in ws["settings"]["clients"]]
    assert emails == ["aaa", "bbb"]
    assert "ccc" not in emails


def test_client_email_is_the_uid():
    """Per-user stats come back keyed by email, so it has to be the uid."""
    cfg = xray_manager.build_config(USERS, {"xray_transports": ["vless-ws"]})
    ws = next(i for i in cfg["inbounds"] if i["tag"] == "inbound-vless-ws")
    for client_entry, expected in zip(ws["settings"]["clients"], USERS):
        assert client_entry["email"] == expected["uid"]
        assert client_entry["id"] == expected["uuid"]


def test_private_ranges_are_blackholed():
    """Upstream ships routing rules that only tag the stats API, leaving a
    proxied client able to reach 127.0.0.1 and the cloud metadata endpoint."""
    cfg = xray_manager.build_config(USERS)
    outbound_tags = {o["tag"] for o in cfg["outbounds"]}
    assert "blocked" in outbound_tags

    blocking = [r for r in cfg["routing"]["rules"]
                if r.get("outboundTag") == "blocked" and "ip" in r]
    assert blocking, "no rule blackholes private destinations"

    blocked = blocking[0]["ip"]
    for cidr in ("127.0.0.0/8", "169.254.0.0/16", "10.0.0.0/8",
                 "192.168.0.0/16", "172.16.0.0/12", "::1/128"):
        assert cidr in blocked, f"{cidr} is reachable through the tunnel"


def test_api_service_allows_live_user_changes():
    """HandlerService is what makes adding a user possible without a restart
    that would drop every other user's connection."""
    cfg = xray_manager.build_config(USERS)
    assert "HandlerService" in cfg["api"]["services"]
    assert "StatsService" in cfg["api"]["services"]


def test_dns_prefers_doh():
    cfg = xray_manager.build_config(USERS)
    assert cfg["dns"]["servers"][0].startswith("https+local://")


def test_config_is_json_serialisable(tmp_path, monkeypatch):
    target = str(tmp_path / "config.json")
    monkeypatch.setattr(xray_manager, "XRAY_CONFIG", target)
    xray_manager.write_config(USERS, {"xray_transports": ["vless-ws", "vless-xhttp"]})
    written = json.load(open(target, encoding="utf-8"))
    assert written["inbounds"]
    assert not any(f.endswith(".tmp") for f in __import__("os").listdir(tmp_path))


# ------------------------------------------------------------------ stats
def test_stats_parsing_extracts_per_user_totals():
    raw = json.dumps({"stat": [
        {"name": "user>>>aaa>>>traffic>>>uplink", "value": 100},
        {"name": "user>>>aaa>>>traffic>>>downlink", "value": 900},
        {"name": "user>>>bbb>>>traffic>>>uplink", "value": 5},
        {"name": "inbound>>>x>>>traffic>>>uplink", "value": 9999},
    ]})
    parsed = xray_manager._parse_stats(raw)
    assert parsed == {"aaa": {"up": 100, "down": 900}, "bbb": {"up": 5, "down": 0}}


def test_stats_parsing_survives_non_json():
    raw = 'name: "user>>>aaa>>>traffic>>>uplink" value: 42'
    assert xray_manager._parse_stats(raw)["aaa"]["up"] == 42
    assert xray_manager._parse_stats("") == {}
    assert xray_manager._parse_stats("garbage") == {}


@pytest.mark.anyio
async def test_first_poll_only_sets_a_baseline(anyio_backend, monkeypatch):
    """Xray's counters are cumulative for the process lifetime. Treating the
    first reading as a delta would re-bill the whole history on every panel
    restart."""
    readings = [
        {"stat": [{"name": "user>>>aaa>>>traffic>>>downlink", "value": 5_000}]},
        {"stat": [{"name": "user>>>aaa>>>traffic>>>downlink", "value": 8_000}]},
    ]

    async def fake_api(*args, **kwargs):
        return 0, json.dumps(readings.pop(0)), ""

    monkeypatch.setattr(xray_manager, "_api", fake_api)
    monkeypatch.setattr(xray_manager, "running", lambda: True)
    monkeypatch.setattr(xray_manager, "_previous_stats", {})
    monkeypatch.setattr(xray_manager, "_counters_valid", False)

    assert await xray_manager.stats_deltas() == {}          # baseline only
    assert await xray_manager.stats_deltas() == {"aaa": {"up": 0, "down": 3_000}}


@pytest.mark.anyio
async def test_counter_reset_is_treated_as_a_fresh_delta(anyio_backend, monkeypatch):
    """If xray restarts underneath the panel its counters go back to zero."""
    readings = [
        {"stat": [{"name": "user>>>aaa>>>traffic>>>downlink", "value": 9_000}]},
        {"stat": [{"name": "user>>>aaa>>>traffic>>>downlink", "value": 9_500}]},
        {"stat": [{"name": "user>>>aaa>>>traffic>>>downlink", "value": 200}]},
    ]

    async def fake_api(*args, **kwargs):
        return 0, json.dumps(readings.pop(0)), ""

    monkeypatch.setattr(xray_manager, "_api", fake_api)
    monkeypatch.setattr(xray_manager, "running", lambda: True)
    monkeypatch.setattr(xray_manager, "_previous_stats", {})
    monkeypatch.setattr(xray_manager, "_counters_valid", False)

    await xray_manager.stats_deltas()
    assert await xray_manager.stats_deltas() == {"aaa": {"up": 0, "down": 500}}
    # counters went backwards -> the new value is the delta, never negative
    assert await xray_manager.stats_deltas() == {"aaa": {"up": 0, "down": 200}}


@pytest.mark.anyio
async def test_stats_are_empty_when_xray_is_not_running(anyio_backend):
    assert await xray_manager.stats_deltas() == {}


# ------------------------------------------------------------------ fallback
def test_panel_falls_back_to_the_python_relay():
    """No binary installed is the normal case for a plain checkout."""
    assert xray_manager.available() is False
    assert main.xray_active() is False


def test_links_stay_on_the_per_user_path_without_xray(client):
    """The Python relay identifies users by path, so it must not change."""
    ib = client.post("/api/inbounds", json={"name": "Ali"}).json()["inbound"]
    body = client.get(f"/sub/{ib['uid']}").text
    assert f"path=/ws/{ib['uid']}" in body
    assert "/vl-ws" not in body
    assert body.count("vless://") == 1


def test_describe_reports_absence_honestly():
    info = xray_manager.describe()
    assert info["available"] is False
    assert info["running"] is False
    assert info["binary"] is None
    assert "vless-xhttp" in info["transports"]


# ------------------------------------------------------------------ settings
def test_transport_setting_is_validated(client):
    assert client.post("/api/settings",
                       json={"xray_transports": "notalist"}).status_code == 400
    assert client.post("/api/settings",
                       json={"xray_transports": ["bogus"]}).status_code == 400
    r = client.post("/api/settings", json={"xray_transports": ["vless-ws", "bogus"]})
    assert r.status_code == 200
    assert r.json()["settings"]["xray_transports"] == ["vless-ws"]
