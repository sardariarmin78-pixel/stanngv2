"""
Off-box backups over the admin's own Telegram bot.

Railway (and most container platforms) give a service an ephemeral
filesystem: everything outside a mounted volume is discarded on every
redeploy. For this panel that means db.json — every user, the admin account,
every setting — vanishes and the next visitor lands on /setup.

A volume fixes it and costs money. This costs nothing and also happens to
give off-box copies, which a volume does not.

The awkward part is finding the backup again *after* the data is gone: the
Bot API will not let a bot read its own sent messages, so there is nowhere to
look up the file. The trick is to pin the backup message. `getChat` returns
the pinned message, which carries the document's file_id, so the panel can
recover with nothing but the bot token and chat id — both of which live in
environment variables, the only thing a redeploy preserves.
"""
import json
import time
from typing import Optional, Tuple

import httpx

TELEGRAM_API = "https://api.telegram.org"
UPLOAD_TIMEOUT = 60
# getFile refuses anything above 20MB. A panel with thousands of users is
# still a few hundred KB, so this is a sanity bound rather than a real limit.
MAX_BACKUP_BYTES = 20 * 1024 * 1024


class BackupError(Exception):
    pass


async def _call(token: str, method: str, timeout: float = 20, **params) -> dict:
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=params)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise BackupError(f"telegram-unreachable: {e}") from e
    if not data.get("ok"):
        raise BackupError(data.get("description") or f"error-{r.status_code}")
    return data["result"]


async def send_backup(token: str, chat_id: str, payload: str,
                      filename: str, caption: str = "") -> dict:
    """Upload the database and pin it, replacing whatever was pinned before.

    Pinning is what makes the backup findable again after the filesystem is
    wiped — see the module docstring.
    """
    if not token or not chat_id:
        raise BackupError("not-configured")
    blob = payload.encode("utf-8")
    if len(blob) > MAX_BACKUP_BYTES:
        raise BackupError("backup-too-large")

    url = f"{TELEGRAM_API}/bot{token}/sendDocument"
    files = {"document": (filename, blob, "application/json")}
    data = {"chat_id": chat_id, "disable_notification": "true"}
    if caption:
        data["caption"] = caption[:1000]

    try:
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            r = await client.post(url, data=data, files=files)
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise BackupError(f"telegram-unreachable: {e}") from e
    if not body.get("ok"):
        raise BackupError(body.get("description") or f"error-{r.status_code}")

    message = body["result"]
    message_id = message.get("message_id")

    # Unpin the previous backup first so the chat keeps exactly one pinned
    # message and getChat always returns the newest.
    try:
        await _call(token, "unpinAllChatMessages", chat_id=chat_id)
    except BackupError:
        pass  # nothing pinned yet, or the bot lacks the right in a group
    try:
        await _call(token, "pinChatMessage", chat_id=chat_id,
                    message_id=message_id, disable_notification=True)
    except BackupError as e:
        # The upload succeeded; only self-restore is affected.
        return {"message_id": message_id, "pinned": False, "pin_error": str(e)}

    return {"message_id": message_id, "pinned": True}


async def find_latest_backup(token: str, chat_id: str) -> Optional[Tuple[str, str]]:
    """Return (file_id, file_name) of the pinned backup, or None."""
    if not token or not chat_id:
        return None
    chat = await _call(token, "getChat", chat_id=chat_id)
    pinned = chat.get("pinned_message") or {}
    doc = pinned.get("document") or {}
    file_id = doc.get("file_id")
    if not file_id:
        return None
    return file_id, doc.get("file_name") or "backup.json"


async def download_backup(token: str, file_id: str) -> dict:
    """Fetch a pinned backup and parse it."""
    info = await _call(token, "getFile", file_id=file_id)
    path = info.get("file_path")
    if not path:
        raise BackupError("no-file-path")
    url = f"{TELEGRAM_API}/file/bot{token}/{path}"
    try:
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            r = await client.get(url)
        if r.status_code != 200:
            raise BackupError(f"download-failed-{r.status_code}")
        if len(r.content) > MAX_BACKUP_BYTES:
            raise BackupError("backup-too-large")
        parsed = json.loads(r.content.decode("utf-8"))
    except BackupError:
        raise
    except (httpx.HTTPError, ValueError, UnicodeDecodeError) as e:
        raise BackupError(f"unreadable-backup: {e}") from e

    if not isinstance(parsed, dict) or not isinstance(parsed.get("inbounds"), list):
        raise BackupError("not-a-peyk-backup")
    return parsed


async def restore_latest(token: str, chat_id: str) -> Optional[dict]:
    """The whole recovery path: locate the pinned backup and return its data."""
    found = await find_latest_backup(token, chat_id)
    if not found:
        return None
    file_id, _name = found
    return await download_backup(token, file_id)


def backup_filename(panel_name: str) -> str:
    safe = "".join(c for c in (panel_name or "peyk") if c.isalnum() or c in "-_") or "peyk"
    return f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.json"


def summarise(db: dict) -> str:
    """A one-line caption so the chat is readable without opening files."""
    inbounds = db.get("inbounds") or []
    active = sum(1 for i in inbounds
                 if i.get("enabled", True) and not _is_expired(i))
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    return (f"🗄 پشتیبان خودکار — {stamp}\n"
            f"کاربران: {len(inbounds)} (فعال: {active})")


def _is_expired(inbound: dict) -> bool:
    expire_at = inbound.get("expire_at")
    return bool(expire_at) and time.time() >= expire_at
