"""Optional owner alerts via Telegram (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)."""
import logging

import httpx

from . import config

log = logging.getLogger("notify")


def owner_alert(text: str) -> None:
    log.info("OWNER ALERT: %s", text)
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:4000], "disable_web_page_preview": True},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        log.warning("Telegram alert failed: %s", exc)
