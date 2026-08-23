"""
The in-panel update check.

This had a bug worth remembering: when a repository has no releases at all,
the resolver used to fall back to the running version, so the panel reported
"you have the newest version" to someone whose repo had never published one.
A reassuring green message is the worst possible way to report a
misconfiguration, so the two states are kept apart here.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import main

ADMIN = {"username": "otaadmin", "password": "correct horse battery"}
REPO = "someone/peyk"


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
    client.post("/api/settings", json={"ota_repo": REPO})
    yield


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    """Answers the two GitHub endpoints the resolver calls, in order."""

    def __init__(self, release=None, tags=None):
        self.release = release or FakeResponse(404)
        self.tags = tags or FakeResponse(200, [])
        self.calls = []

    async def get(self, url, *a, **kw):
        self.calls.append(url)
        return self.release if "releases/latest" in url else self.tags

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def resolve(client, fake):
    return client.portal.call(
        main._resolve_latest_release, REPO, main.APP_VERSION, fake)


# ------------------------------------------------------------------ resolver
def test_a_published_release_is_found(client):
    fake = FakeClient(release=FakeResponse(200, {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/x/y/releases/tag/v9.9.9",
        "zipball_url": "https://api.github.com/zip",
    }))
    latest, url, zip_url = resolve(client, fake)
    assert latest == "9.9.9"          # the leading v is stripped
    assert zip_url == "https://api.github.com/zip"
    assert url.endswith("v9.9.9")


def test_it_falls_back_to_tags(client):
    """A repo can carry tags without ever cutting a formal release."""
    fake = FakeClient(tags=FakeResponse(200, [
        {"name": "v3.4.0", "zipball_url": "https://api.github.com/tagzip"},
    ]))
    latest, url, zip_url = resolve(client, fake)
    assert latest == "3.4.0"
    assert zip_url == "https://api.github.com/tagzip"
    assert "releases/tag/v3.4.0" in url


def test_no_releases_and_no_tags_returns_none(client):
    """The regression: this used to return the running version instead."""
    latest, url, zip_url = resolve(client, FakeClient())
    assert latest is None
    assert zip_url is None
    assert url == f"https://github.com/{REPO}/releases"


def test_an_empty_tag_name_is_not_a_version(client):
    fake = FakeClient(tags=FakeResponse(200, [{"name": "", "zipball_url": "z"}]))
    assert resolve(client, fake)[0] is None


def test_a_release_without_a_tag_falls_through_to_tags(client):
    fake = FakeClient(
        release=FakeResponse(200, {"tag_name": ""}),
        tags=FakeResponse(200, [{"name": "v2.0.0", "zipball_url": "z"}]),
    )
    assert resolve(client, fake)[0] == "2.0.0"
    assert any("tags" in c for c in fake.calls)


# ------------------------------------------------------------------ endpoint
@pytest.fixture
def github(monkeypatch):
    """Swap the http client the endpoint builds for a scripted one."""
    holder = {}

    def install(release=None, tags=None):
        fake = FakeClient(release, tags)
        holder["fake"] = fake
        monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **kw: fake)
        return fake

    return install


def test_check_reports_an_available_update(client, github):
    github(release=FakeResponse(200, {
        "tag_name": "v99.0.0", "html_url": "u", "zipball_url": "z"}))
    body = client.get("/api/ota/check").json()
    assert body["update_available"] is True
    assert body["latest"] == "99.0.0"
    assert body["no_releases"] is False


def test_check_reports_being_current(client, github):
    github(release=FakeResponse(200, {
        "tag_name": "v" + main.APP_VERSION, "html_url": "u", "zipball_url": "z"}))
    body = client.get("/api/ota/check").json()
    assert body["update_available"] is False
    assert body["no_releases"] is False
    assert body["latest"] == main.APP_VERSION


def test_an_older_release_is_not_an_update(client, github):
    github(release=FakeResponse(200, {
        "tag_name": "v0.0.1", "html_url": "u", "zipball_url": "z"}))
    assert client.get("/api/ota/check").json()["update_available"] is False


def test_check_says_no_releases_rather_than_up_to_date(client, github):
    """The bug this file exists for: silence from GitHub must not read as
    reassurance."""
    github()
    body = client.get("/api/ota/check").json()
    assert body["no_releases"] is True
    assert body["update_available"] is False
    assert body["latest"] is None
    assert body["current"] == main.APP_VERSION


def test_update_refuses_when_nothing_is_published(client, github):
    github()
    r = client.post("/api/ota/update")
    assert r.status_code == 400
    assert r.json()["detail"] == "no-releases-published"


def test_update_declines_when_already_current(client, github):
    github(release=FakeResponse(200, {
        "tag_name": "v" + main.APP_VERSION, "html_url": "u", "zipball_url": "z"}))
    body = client.post("/api/ota/update").json()
    assert body["ok"] is False
    assert body["reason"] == "already-up-to-date"


def test_check_needs_a_repo(client):
    client.post("/api/settings", json={"ota_repo": ""})
    r = client.get("/api/ota/check")
    assert r.status_code == 400
    assert r.json()["detail"] == "no-repo-configured"


def test_check_is_owner_only(client):
    client.post("/api/resellers",
                json={"username": "ota_seller", "password": "seller-pass-1234"})
    seller = TestClient(main.app)
    seller.post("/api/login", json={"username": "ota_seller", "password": "seller-pass-1234"})
    assert seller.get("/api/ota/check").status_code == 403


# ------------------------------------------------------------------ comparison
@pytest.mark.parametrize("older,newer", [
    ("3.4.0", "3.4.1"),
    ("3.4.0", "3.5.0"),
    ("3.9.0", "3.10.0"),     # not a string comparison
    ("2.0.0", "10.0.0"),
    ("3.4", "3.4.1"),
])
def test_version_ordering(older, newer):
    assert main._ver_tuple(newer) > main._ver_tuple(older)


def test_equal_versions_are_not_an_update():
    assert not main._ver_tuple("3.4.0") > main._ver_tuple("3.4.0")


def test_a_junk_version_sorts_lowest():
    assert main._ver_tuple("") == (0,)
    assert main._ver_tuple(None) == (0,)
    assert main._ver_tuple("3.4.0") > main._ver_tuple("not-a-version")
