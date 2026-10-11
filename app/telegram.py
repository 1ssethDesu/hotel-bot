"""Telegram guest channel: Bot API helpers + long polling (works without a public address)."""
import asyncio
import logging
import uuid

import httpx

from . import config
from .messenger import split_text

log = logging.getLogger("telegram")
API = "https://api.telegram.org"
PREFIX = "tg"                # guests are stored as "tg<chat id>" so they never clash with Messenger PSIDs
POLL_SECONDS = 25


def psid_for(chat_id) -> str:
    return f"{PREFIX}{chat_id}"


def is_telegram(psid: str) -> bool:
    return bool(psid) and psid.startswith(PREFIX) and psid[len(PREFIX):].lstrip("-").isdigit()


def _chat_id(psid: str) -> str:
    return psid[len(PREFIX):]


def _call(method: str, files=None, **params) -> dict:
    """Call the Bot API. Never raises; returns {"ok": bool, ...}."""
    if not config.TELEGRAM_BOT_TOKEN:
        log.info("[DRY RUN - no bot token] would call %s: %s", method, params)
        return {"ok": True, "dry_run": True}
    url = f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = httpx.post(url, data=params, files=files, timeout=30) if files else \
            httpx.post(url, json=params, timeout=20)
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.error("Telegram %s failed: %s", method, type(exc).__name__)   # the URL holds the token: don't log it
        return {"ok": False}
    if not data.get("ok"):
        log.error("Telegram %s error: %s", method, data.get("description"))
    return data


def send_text(psid: str, text: str, human: bool = False, quick_replies=None) -> bool:
    ok = True
    for part in split_text(text):
        ok = _call("sendMessage", chat_id=_chat_id(psid), text=part,
                   disable_web_page_preview=True).get("ok", False) and ok
    return ok


def send_image(psid: str, image_url: str) -> bool:
    """Our own /media files are uploaded directly, so photos also work when BASE_URL is not public."""
    prefix = f"{config.BASE_URL}/media/"
    path = config.UPLOAD_DIR / image_url[len(prefix):] if image_url.startswith(prefix) else None
    if path and path.is_file():
        with path.open("rb") as f:
            return _call("sendPhoto", files={"photo": (path.name, f)}, chat_id=_chat_id(psid)).get("ok", False)
    return _call("sendPhoto", chat_id=_chat_id(psid), photo=image_url).get("ok", False)


def typing_on(psid: str):
    if config.TELEGRAM_BOT_TOKEN:
        _call("sendChatAction", chat_id=_chat_id(psid), action="typing")


def mark_seen(psid: str):
    pass  # Telegram has no "seen" receipt for bots


def download_photo(file_id: str, code: str) -> str:
    """Save a guest's photo (payment screenshot) locally. Returns the file name or '' on failure."""
    info = _call("getFile", file_id=file_id).get("result") or {}
    file_path = info.get("file_path")
    if not file_path or (info.get("file_size") or 0) > 10 * 1024 * 1024:
        return ""
    try:
        r = httpx.get(f"{API}/file/bot{config.TELEGRAM_BOT_TOKEN}/{file_path}", timeout=20)
    except httpx.HTTPError as exc:
        log.warning("Could not download photo: %s", type(exc).__name__)
        return ""
    if r.status_code != 200 or len(r.content) > 10 * 1024 * 1024:
        return ""
    filename = f"proof_{code}_{uuid.uuid4().hex[:6]}{'.png' if file_path.lower().endswith('.png') else '.jpg'}"
    (config.UPLOAD_DIR / filename).write_bytes(r.content)
    return filename


async def poll_loop(handle) -> None:
    """Fetch new messages forever and run handle(update) for each one in a worker thread."""
    url = f"{API}/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    offset, tasks = None, set()
    log.info("Telegram guest bot: polling for messages")
    async with httpx.AsyncClient(timeout=POLL_SECONDS + 10) as client:
        while True:
            try:
                r = await client.post(url, json={"timeout": POLL_SECONDS, "offset": offset,
                                                 "allowed_updates": ["message"]})
                data = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Telegram polling failed: %s", type(exc).__name__)
                await asyncio.sleep(5)
                continue
            if not data.get("ok"):
                # 409 = another copy of this bot is polling, or a webhook is set for this token
                log.error("Telegram polling error: %s", data.get("description"))
                await asyncio.sleep(15)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                task = asyncio.create_task(asyncio.to_thread(handle, update))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
