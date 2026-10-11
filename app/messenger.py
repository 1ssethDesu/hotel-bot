"""Facebook Messenger Send API helpers + webhook signature check."""
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db

log = logging.getLogger("messenger")
GRAPH = "https://graph.facebook.com"
BOT_METADATA = "hotel-bot"   # marks our own messages so echoes from staff can be told apart
MAX_TEXT = 1900              # Messenger limit is 2000 chars


def verify_signature(raw_body: bytes, header: str) -> bool:
    if not config.FB_APP_SECRET:
        if config.BASE_URL.startswith("https://"):
            log.error("FB_APP_SECRET is not set: rejecting webhook calls. Add it to .env.")
            return False
        log.warning("FB_APP_SECRET not set - skipping signature check (local testing only)")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(config.FB_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def _within_24h(psid: str) -> bool:
    with db.get_conn() as conn:
        row = conn.execute("SELECT last_user_msg_at FROM guests WHERE psid = ?", (psid,)).fetchone()
    if not row or not row["last_user_msg_at"]:
        return False
    last = datetime.fromisoformat(row["last_user_msg_at"])
    return datetime.now(timezone.utc) - last < timedelta(hours=23, minutes=50)


def _post(payload: dict) -> dict:
    """Call the Send API. Never raises; returns {"ok": bool, ...}."""
    if not config.FB_PAGE_ACCESS_TOKEN:
        log.info("[DRY RUN - no page token] would send: %s", payload)
        return {"ok": True, "dry_run": True}
    url = f"{GRAPH}/{config.FB_GRAPH_VERSION}/me/messages"
    try:
        r = httpx.post(url, params={"access_token": config.FB_PAGE_ACCESS_TOKEN}, json=payload, timeout=20)
        data = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        log.error("Send API request failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    if r.status_code >= 400:
        log.error("Send API error %s: %s", r.status_code, r.text)
        return {"ok": False, "error": (data.get("error") or {}).get("message", r.text)}
    data["ok"] = True
    return data


class OutsideWindow(Exception):
    """The guest's last message is older than 24h: Meta does not allow a normal reply."""


def _envelope(psid: str, message: dict | None = None, human: bool = False) -> dict:
    """RESPONSE inside the 24h window. Outside it only staff replies (HUMAN_AGENT tag, 7 days) are allowed.

    Meta retired the POST_PURCHASE_UPDATE / ACCOUNT_UPDATE / CONFIRMED_EVENT_UPDATE tags in 2026, so automatic
    updates outside the window raise OutsideWindow and the owner is asked to contact the guest instead.
    """
    env = {"recipient": {"id": psid}}
    if message is not None:
        env["message"] = message
    if _within_24h(psid):
        env["messaging_type"] = "RESPONSE"
    elif human:
        env["messaging_type"] = "MESSAGE_TAG"
        env["tag"] = "HUMAN_AGENT"           # needs the Human Agent permission (App Review)
    else:
        raise OutsideWindow(psid)
    return env


def split_text(text: str):
    text = (text or "").strip()
    while len(text) > MAX_TEXT:
        cut = text.rfind("\n", 0, MAX_TEXT)
        if cut < MAX_TEXT // 2:
            cut = text.rfind(" ", 0, MAX_TEXT)
        if cut <= 0:
            cut = MAX_TEXT
        yield text[:cut].strip()
        text = text[cut:].strip()
    if text:
        yield text


def send_text(psid: str, text: str, human: bool = False, quick_replies=None) -> bool:
    """Returns True if every part was accepted by Meta. Raises OutsideWindow (see _envelope)."""
    ok = True
    parts = list(split_text(text))
    for i, part in enumerate(parts):
        msg = {"text": part, "metadata": BOT_METADATA}
        if quick_replies and i == len(parts) - 1:
            msg["quick_replies"] = [
                {"content_type": "text", "title": q[:20], "payload": q[:1000]} for q in quick_replies[:13]
            ]
        ok = _post(_envelope(psid, msg, human=human)).get("ok", False) and ok
    return ok


def send_image(psid: str, image_url: str) -> bool:
    msg = {"attachment": {"type": "image", "payload": {"url": image_url, "is_reusable": True}},
           "metadata": BOT_METADATA}
    return _post(_envelope(psid, msg)).get("ok", False)


def typing_on(psid: str):
    if config.FB_PAGE_ACCESS_TOKEN:
        _post({"recipient": {"id": psid}, "sender_action": "typing_on"})


def mark_seen(psid: str):
    if config.FB_PAGE_ACCESS_TOKEN:
        _post({"recipient": {"id": psid}, "sender_action": "mark_seen"})


def fetch_name(psid: str) -> str:
    if not config.FB_PAGE_ACCESS_TOKEN:
        return ""
    try:
        r = httpx.get(f"{GRAPH}/{config.FB_GRAPH_VERSION}/{psid}",
                      params={"fields": "first_name,last_name", "access_token": config.FB_PAGE_ACCESS_TOKEN},
                      timeout=10)
        if r.status_code == 200:
            d = r.json()
            return f"{d.get('first_name', '')} {d.get('last_name', '')}".strip()
    except httpx.HTTPError:
        pass
    return ""


TRUSTED_MEDIA_HOSTS = (".fbcdn.net", ".fbsbx.com", ".facebook.com")


def is_facebook_media_url(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host.endswith(h) for h in TRUSTED_MEDIA_HOSTS)


def public_url(filename: str) -> str:
    return f"{config.BASE_URL}/media/{filename}"


def set_messenger_profile(greeting_en: str, greeting_km: str) -> dict:
    """Get Started button, greeting and persistent menu."""
    payload = {
        "get_started": {"payload": "GET_STARTED"},
        "greeting": [
            {"locale": "default", "text": greeting_en[:160]},
            {"locale": "km_KH", "text": greeting_km[:160]},
        ],
        "persistent_menu": [{
            "locale": "default",
            "composer_input_disabled": False,
            "call_to_actions": [
                {"type": "postback", "title": "Book a room / កក់បន្ទប់", "payload": "MENU_BOOK"},
                {"type": "postback", "title": "My booking / ការកក់របស់ខ្ញុំ", "payload": "MENU_MY_BOOKING"},
                {"type": "postback", "title": "Talk to staff / បុគ្គលិក", "payload": "MENU_HUMAN"},
            ],
        }],
    }
    r = httpx.post(f"{GRAPH}/{config.FB_GRAPH_VERSION}/me/messenger_profile",
                   params={"access_token": config.FB_PAGE_ACCESS_TOKEN}, json=payload, timeout=20)
    return r.json()
