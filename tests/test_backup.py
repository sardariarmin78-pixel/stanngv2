"""
Off-box backup and self-restore.

Railway discards the filesystem on every redeploy, so db.json — every user,
the admin account, every setting — is gone and the next visitor lands on
/setup. These tests cover the recovery path, which by definition only runs
when something has already gone wrong and so never gets exercised by hand.

Telegram is stubbed: the point is the panel's logic, not their API.
"""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import reset_panel

import backup
import main

ADMIN = {"username": "bkadmin", "password": "correct horse battery"}


@pytest.fixture(scope="module")
def client():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/login", json=ADMIN)
        yield c


@pytest.fixture(autouse=True)
def ensure_session(client):
    """Several tests here deliberately wipe the panel to simulate a redeploy.

    Re-establish the shared client's session before each test rather than
    letting the first wipe cascade into 401s for everything after it.
    """
    if client.get("/api/setup-status").json().get("needs_setup"):
        client.post("/api/setup", json=ADMIN)
    client.post("/api/login", json=ADMIN)
    yield


class FakeTelegram:
    """Stands in for the bot API, remembering only the pinned document."""

    def __init__(self):
        self.uploads = []
        self.pinned = None
        self.fail_send = False
        self.fail_pin = False

    async def send_backup(self, token, chat_id, payload, filename, caption=""):
        if self.fail_send:
            raise backup.BackupError("chat not found")
        self.uploads.append({"payload": payload, "filename": filename, "caption": caption})
        if self.fail_pin:
            return {"message_id": len(self.uploads), "pinned": False, "pin_error": "no rights"}
        self.pinned = payload
        return {"message_id": len(self.uploads), "pinned": True}

    async def restore_latest(self, token, chat_id):
        if self.pinned is None:
            return None
        return json.loads(self.pinned)


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(backup, "send_backup", fake.send_backup)
    monkeypatch.setattr(backup, "restore_latest", fake.restore_latest)
    return fake


def configure(client, enabled=True):
    return client.post("/api/settings", json={
        "telegram_bot_token": "123456789:AAEhBOweik6ad9r_ZeuN65HDdvBcQnKxyz0",
        "telegram_chat_id": "987654321",
        "auto_backup_enabled": enabled,
    })


# ------------------------------------------------------------------ status
def test_backup_reports_unconfigured(client):
    client.post("/api/settings", json={"telegram_bot_token": "", "telegram_chat_id": ""})
    body = client.get("/api/backup/telegram").json()
    assert body["configured"] is False
    assert client.post("/api/backup/telegram").status_code == 400


def test_backup_requires_auth():
    reset_panel()
    with TestClient(main.app) as c:
        c.post("/api/setup", json=ADMIN)
        c.post("/api/logout")
        assert c.get("/api/backup/telegram").status_code == 401
        assert c.post("/api/backup/telegram").status_code == 401
        assert c.post("/api/backup/telegram/restore").status_code == 401


# ------------------------------------------------------------------ backup
def test_manual_backup_uploads_the_database(client, telegram):
    configure(client)
    client.post("/api/inbounds", json={"name": "Ali", "quota_gb": 5})

    r = client.post("/api/backup/telegram")
    assert r.status_code == 200
    assert len(telegram.uploads) == 1

    sent = json.loads(telegram.uploads[0]["payload"])
    assert sent["admin"]["username"] == ADMIN["username"]
    assert any(i["name"] == "Ali" for i in sent["inbounds"])
    # the filename carries the brand and a timestamp, not a fixed name
    assert telegram.uploads[0]["filename"].endswith(".json")
    assert "کاربران" in telegram.uploads[0]["caption"]


def test_backup_records_outcome(client, telegram):
    configure(client)
    client.post("/api/backup/telegram")
    last = client.get("/api/backup/telegram").json()["last"]
    assert last["ok"] is True
    assert last["pinned"] is True
    assert last["detail"] == "manual"


def test_backup_failure_is_reported_not_swallowed(client, telegram):
    configure(client)
    telegram.fail_send = True
    r = client.post("/api/backup/telegram")
    assert r.status_code == 502
    assert "chat not found" in r.json()["detail"]

    last = client.get("/api/backup/telegram").json()["last"]
    assert last["ok"] is False


def test_upload_without_pin_still_counts_as_backup(client, telegram):
    """Pinning can fail on a group where the bot lacks rights. The copy is
    still safe; only unattended self-restore is affected."""
    configure(client)
    telegram.fail_pin = True
    assert client.post("/api/backup/telegram").status_code == 200
    last = client.get("/api/backup/telegram").json()["last"]
    assert last["ok"] is True
    assert last["pinned"] is False


# ------------------------------------------------------------------ restore
def test_restore_brings_users_back(client, telegram):
    configure(client)
    created = client.post("/api/inbounds", json={"name": "SurvivesWipe", "quota_gb": 9}).json()
    client.post("/api/backup/telegram")

    # simulate the wipe
    client.delete(f"/api/inbounds/{created['inbound']['uid']}")
    assert not any(i["name"] == "SurvivesWipe"
                   for i in client.get("/api/inbounds").json()["inbounds"])

    assert client.post("/api/backup/telegram/restore").status_code == 200
    client.post("/api/login", json=ADMIN)
    names = [i["name"] for i in client.get("/api/inbounds").json()["inbounds"]]
    assert "SurvivesWipe" in names


def test_restore_without_a_backup_is_404(client, telegram):
    configure(client)
    assert telegram.pinned is None
    assert client.post("/api/backup/telegram/restore").status_code == 404


# ------------------------------------------------------------------ self-restore
def test_self_restore_recovers_a_blank_container(client, telegram, monkeypatch):
    """The whole point: a redeploy wipes the disk, and the panel puts itself
    back together from the pinned backup using only environment credentials."""
    configure(client)
    client.post("/api/inbounds", json={"name": "Ghost", "quota_gb": 3})
    client.post("/api/backup/telegram")

    monkeypatch.setattr(main, "BOOTSTRAP_BOT_TOKEN", "123456789:AAEh")
    monkeypatch.setattr(main, "BOOTSTRAP_CHAT_ID", "987654321")

    reset_panel()  # the wipe: no admin, no users
    assert main.store.get_sync().get("admin") is None

    assert client.portal.call(main._self_restore_if_empty) is True
    db = main.store.get_sync()
    assert db["admin"]["username"] == ADMIN["username"]
    assert any(i["name"] == "Ghost" for i in db["inbounds"])


def test_self_restore_never_overwrites_a_live_panel(client, telegram, monkeypatch):
    """A configured panel must not be replaced by whatever is pinned in a chat."""
    configure(client)
    client.post("/api/inbounds", json={"name": "Old", "quota_gb": 1})
    client.post("/api/backup/telegram")
    client.post("/api/inbounds", json={"name": "NewerThanBackup", "quota_gb": 1})

    monkeypatch.setattr(main, "BOOTSTRAP_BOT_TOKEN", "123456789:AAEh")
    monkeypatch.setattr(main, "BOOTSTRAP_CHAT_ID", "987654321")

    assert client.portal.call(main._self_restore_if_empty) is False
    names = [i["name"] for i in main.store.get_sync()["inbounds"]]
    assert "NewerThanBackup" in names


def test_self_restore_needs_environment_credentials(client, telegram, monkeypatch):
    """Settings live in the database, which is exactly what is gone. Only the
    environment can bootstrap recovery."""
    configure(client)
    client.post("/api/backup/telegram")
    monkeypatch.setattr(main, "BOOTSTRAP_BOT_TOKEN", "")
    monkeypatch.setattr(main, "BOOTSTRAP_CHAT_ID", "")

    reset_panel()
    assert client.portal.call(main._self_restore_if_empty) is False


def test_self_restore_survives_telegram_being_down(client, telegram, monkeypatch):
    """A failed recovery must leave a bootable panel, not crash startup."""
    async def boom(token, chat):
        raise backup.BackupError("telegram-unreachable")

    monkeypatch.setattr(backup, "restore_latest", boom)
    monkeypatch.setattr(main, "BOOTSTRAP_BOT_TOKEN", "123456789:AAEh")
    monkeypatch.setattr(main, "BOOTSTRAP_CHAT_ID", "987654321")

    reset_panel()
    assert client.portal.call(main._self_restore_if_empty) is False
    # still usable: setup is reachable
    assert client.get("/api/setup-status").json()["needs_setup"] is True


# ------------------------------------------------------------------ helpers
def test_restore_rejects_a_foreign_document(client, telegram):
    """getChat returns whatever happens to be pinned; it may not be ours.

    Restoring an arbitrary JSON file would replace the panel with nonsense.
    """
    configure(client)
    telegram.pinned = json.dumps({"hello": "world", "inbounds": []})
    before = len(main.store.get_sync()["inbounds"])

    r = client.post("/api/backup/telegram/restore")
    assert r.status_code == 400
    assert len(main.store.get_sync()["inbounds"]) == before


def test_filename_is_sanitised():
    name = backup.backup_filename("پنل من / Peyk")
    assert "/" not in name and " " not in name
    assert name.endswith(".json")
    assert backup.backup_filename("").startswith("peyk-")


def test_summary_counts_active_users():
    import time
    db = {"inbounds": [
        {"name": "a", "enabled": True},
        {"name": "b", "enabled": False},
        {"name": "c", "enabled": True, "expire_at": time.time() - 100},
    ]}
    text = backup.summarise(db)
    assert "3" in text            # total
    assert "فعال: 1" in text      # b disabled, c expired


def test_storage_ephemeral_detection(monkeypatch):
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.delenv("RENDER_DISK_PATH", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    assert main._storage_looks_ephemeral() is False

    # on Railway with no volume attached: the case that eats databases
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert main._storage_looks_ephemeral() is True

    # volume attached and the data directory sits inside it
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", main.DATA_DIR_PATH)
    assert main._storage_looks_ephemeral() is False
