"""Handles incoming Messenger / Telegram events and owner-triggered guest notifications."""
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from . import agent, booking, config, db, messenger, notify, telegram

log = logging.getLogger("bot")
KHMER_RE = re.compile(r"[ក-៿]")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
DEBOUNCE_SECONDS = 1.5


def _lock_for(psid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(psid, threading.Lock())


def guest_prefers_khmer(psid: str) -> bool:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT content FROM messages WHERE psid=? AND role='user' ORDER BY id DESC LIMIT 3",
                            (psid,)).fetchall()
    return any(KHMER_RE.search(r["content"] or "") for r in rows)


def t(psid: str, en: str, km: str) -> str:
    return km if guest_prefers_khmer(psid) else en


def _already_processed(mid: str) -> bool:
    if not mid:
        return False
    with db.get_conn() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO processed_mids(mid, created_at) VALUES (?,?)", (mid, db.utcnow()))
        return cur.rowcount == 0


def _channel(psid: str):
    """The adapter module (same send functions) for the app this guest writes from."""
    return telegram if telegram.is_telegram(psid) else messenger


def reply(psid: str, text: str, images=()) -> bool:
    """Send to the guest. Returns False if the app refused or Messenger's 24h window is closed."""
    ch = _channel(psid)
    try:
        ok = ch.send_text(psid, text) if text else True
        for url in images:
            ok = ch.send_image(psid, url) and ok
    except messenger.OutsideWindow:
        return False
    if text:
        agent.log_message(psid, "assistant", text if ok else f"[NOT DELIVERED] {text}")
    return ok


def _owner_must_contact(b: dict, what: str) -> None:
    if telegram.is_telegram(b["psid"]):
        where, why = "Telegram", "They may have blocked the bot."
    else:
        where, why = "Messenger", "Their last message is probably older than 24h."
    notify.owner_alert(f"Could not message {b['guest_name']} on {where} about {b['code']} ({what}). {why} "
                       f"Please call: {b.get('phone') or 'no phone given'}")


# ---------- incoming ----------
def handle_payload(payload: dict) -> None:
    if payload.get("object") != "page":
        return
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            try:
                handle_event(event)
            except Exception:  # never let one bad event break the rest
                log.exception("Failed to handle event: %s", event)


def handle_event(event: dict) -> None:
    message = event.get("message") or {}

    # Echo of a message sent by the Page. If it wasn't sent by this bot, the owner replied by hand.
    if message.get("is_echo"):
        if _already_processed(message.get("mid")):
            return
        if message.get("metadata") != messenger.BOT_METADATA and message.get("text"):
            psid = event["recipient"]["id"]
            agent.ensure_guest(psid)
            agent.log_message(psid, "staff", message["text"])
            agent.set_paused(psid, True, "Owner replied from the Page inbox")
            with db.get_conn() as conn:
                conn.execute("UPDATE guests SET needs_attention = 0 WHERE psid = ?", (psid,))
        return

    psid = (event.get("sender") or {}).get("id")
    if not psid:
        return
    mid = message.get("mid") or (event.get("postback") or {}).get("mid")
    if _already_processed(mid):
        return
    agent.ensure_guest(psid)
    messenger.mark_seen(psid)

    # --- postbacks (Get Started / menu) ---
    if "postback" in event:
        pb = event["postback"].get("payload", "")
        if pb == "MENU_HUMAN":
            agent.log_message(psid, "user", "[Tapped: Talk to staff]")
            agent.set_paused(psid, True, "Guest asked for staff (menu)")
            notify.owner_alert(f"Guest {agent.get_guest(psid).get('name') or psid} asked to talk to staff.\n"
                               f"{config.BASE_URL}/admin/conversations/{psid}")
            reply(psid, "A team member will reply to you here soon. Thank you for waiting!\n"
                        "បុគ្គលិករបស់យើងនឹងឆ្លើយតបអ្នកក្នុងពេលឆាប់ៗនេះ។ សូមអរគុណ!")
            return
        text = {
            "GET_STARTED": "Hello",
            "MENU_BOOK": "I would like to book a room.",
            "MENU_MY_BOOKING": "Please show my booking.",
        }.get(pb, event["postback"].get("title") or "Hello")
        return _respond(psid, text)

    # --- text / attachments ---
    text = message.get("text") or ""
    if message.get("quick_reply"):
        text = message["quick_reply"].get("payload") or text
    attachments = message.get("attachments") or []
    images = [a for a in attachments if a.get("type") == "image" and not (a.get("payload") or {}).get("sticker_id")]
    if images:
        pending = _awaiting_deposit(psid)
        if pending:
            url = (images[0].get("payload") or {}).get("url", "")
            return _handle_payment_proof(psid, pending, _download_proof(url, pending["code"]), caption=text)
        text = (text + "\n[Guest sent a photo]").strip()
    elif attachments and not text:
        kinds = ", ".join(sorted({a.get("type", "file") for a in attachments}))
        text = f"[Guest sent: {kinds}]"
    if not text:
        return
    _respond(psid, text)


def handle_telegram_update(update: dict) -> None:
    try:
        _handle_telegram_message(update)
    except Exception:
        log.exception("Failed to handle Telegram update %s", update.get("update_id"))


def _handle_telegram_message(update: dict) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private":   # guests book in a one-to-one chat; ignore groups
        return
    if _already_processed(f"tg{update.get('update_id')}"):
        return
    psid = telegram.psid_for(chat["id"])
    text = (message.get("text") or message.get("caption") or "").strip()
    if text == "/id":   # the owner needs this number for TELEGRAM_CHAT_ID
        telegram.send_text(psid, f"Your Telegram chat ID is {chat['id']}")
        return
    sender = message.get("from") or {}
    agent.ensure_guest(psid, name=f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip())
    if text == "/start":
        text = "Hello"

    photo = message.get("photo") or []
    if photo:
        pending = _awaiting_deposit(psid)
        if pending:
            filename = telegram.download_photo(photo[-1]["file_id"], pending["code"])   # last = largest size
            return _handle_payment_proof(psid, pending, filename, caption=text)
        text = (text + "\n[Guest sent a photo]").strip()
    elif not text:
        kinds = [k for k in ("voice", "video", "audio", "document", "sticker", "location", "contact") if k in message]
        if not kinds:
            return
        text = f"[Guest sent: {', '.join(kinds)}]"
    _respond(psid, text)


def _awaiting_deposit(psid: str) -> dict | None:
    """The booking a payment screenshot belongs to: one waiting for a deposit, or one whose hold just expired."""
    today = booking.today_local().isoformat()
    pending = booking.list_bookings(psid=psid, status="pending_deposit") or [
        b for b in booking.list_bookings(psid=psid, status="expired") if b["check_in"] >= today]
    return pending[0] if pending else None


def _download_proof(url: str, code: str) -> str:
    """Save the screenshot locally (Facebook CDN only). Returns the file name or '' on failure."""
    if not messenger.is_facebook_media_url(url):
        log.warning("Ignoring payment proof from untrusted URL: %s", (url or "")[:200])
        return ""
    try:
        r = httpx.get(url, timeout=20, follow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            nxt = r.headers.get("location", "")
            if not messenger.is_facebook_media_url(nxt):
                return ""
            r = httpx.get(nxt, timeout=20, follow_redirects=False)
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) > 10 * 1024 * 1024:
            return ""
        filename = f"proof_{code}_{uuid.uuid4().hex[:6]}{'.png' if 'png' in ctype else '.jpg'}"
        (config.UPLOAD_DIR / filename).write_bytes(r.content)
        return filename
    except httpx.HTTPError as exc:
        log.warning("Could not download proof: %s", exc)
        return ""


def _handle_payment_proof(psid: str, b: dict, filename: str, caption: str = "") -> None:
    filename = filename or "not_downloaded"
    with db.get_conn() as conn:
        conn.execute("UPDATE guests SET last_user_msg_at = ? WHERE psid = ?", (db.utcnow(), psid))
    agent.log_message(psid, "system", f"Guest sent a payment screenshot for {b['code']}.")
    try:
        booking.attach_payment_proof(b["id"], filename)
    except booking.BookingError:
        agent.set_paused(psid, True, f"Paid after hold expired, room taken ({b['code']})")
        notify.owner_alert(f"{b['guest_name']} sent a payment screenshot for {b['code']} AFTER the hold expired, "
                           f"and the room is no longer free. Please contact them (offer another room or a refund). "
                           f"Phone: {b['phone']}\n{config.BASE_URL}/admin/bookings/{b['id']}")
        reply(psid, t(psid,
                      f"Thank you for the payment for {b['code']}. Unfortunately the room hold had already expired. "
                      "A team member will contact you shortly to sort this out.",
                      f"សូមអរគុណសម្រាប់ការបង់ប្រាក់សម្រាប់ {b['code']}។ ជាអកុសល ការរក្សាបន្ទប់បានផុតកំណត់ហើយ។ "
                      "បុគ្គលិករបស់យើងនឹងទាក់ទងអ្នកក្នុងពេលឆាប់ៗ ដើម្បីដោះស្រាយ។"))
        return
    cur = db.get_setting("currency")
    notify.owner_alert(f"Payment screenshot received for {b['code']} ({b['guest_name']}), deposit "
                       f"{b['deposit_amount']:.2f} {cur}. Please verify and confirm:\n"
                       f"{config.BASE_URL}/admin/bookings/{b['id']}")
    reply(psid, t(psid,
                  f"Thank you! We received your payment screenshot for booking {b['code']}. "
                  "Your room is held while we verify it, and we'll message you as soon as it's confirmed.",
                  f"សូមអរគុណ! យើងបានទទួលរូបភាពការបង់ប្រាក់សម្រាប់ការកក់ {b['code']} ហើយ។ "
                  "បន្ទប់របស់អ្នកត្រូវបានរក្សាទុក ខណៈយើងកំពុងពិនិត្យ ហើយយើងនឹងផ្ញើសារជូនភ្លាមៗពេលបញ្ជាក់រួច។"))
    if caption:  # text sent together with the screenshot: let the AI answer it
        _respond(psid, caption)


def _maybe_auto_resume(guest: dict) -> bool:
    """Resume the bot if staff has been silent for auto_resume_hours. Returns True if bot is active."""
    if not guest.get("bot_paused"):
        return True
    try:
        hours = float(db.get_setting("auto_resume_hours") or 0)
    except ValueError:
        hours = 12
    if hours <= 0:
        return False
    with db.get_conn() as conn:
        last_staff = conn.execute("SELECT MAX(created_at) FROM messages WHERE psid=? AND role='staff'",
                                  (guest["psid"],)).fetchone()[0]
    ref = max(filter(None, [guest.get("paused_at"), last_staff]), default=None)
    if ref and datetime.now(timezone.utc) - datetime.fromisoformat(ref) > timedelta(hours=hours):
        agent.set_paused(guest["psid"], False)
        return True
    return False


def _respond(psid: str, text: str, already_logged: bool = False) -> None:
    if not already_logged:
        agent.log_message(psid, "user", text)
    with db.get_conn() as conn:
        my_id = conn.execute("SELECT MAX(id) FROM messages WHERE psid=? AND role='user'", (psid,)).fetchone()[0]

    guest = agent.get_guest(psid)
    if db.get_setting("bot_enabled") != "1" or not _maybe_auto_resume(guest):
        with db.get_conn() as conn:
            conn.execute("UPDATE guests SET needs_attention = 1 WHERE psid = ?", (psid,))
        return

    # Guests often send several short messages in a row: wait briefly and answer them together.
    time.sleep(DEBOUNCE_SECONDS)
    with _lock_for(psid):
        with db.get_conn() as conn:
            latest = conn.execute("SELECT MAX(id) FROM messages WHERE psid=? AND role='user'", (psid,)).fetchone()[0]
        if latest != my_id:
            return  # a newer message will trigger the reply
        _channel(psid).typing_on(psid)
        try:
            text_out, runner = agent.generate_reply(psid)
        except Exception as exc:
            log.exception("AI error")
            notify.owner_alert(f"Bot error for guest {psid}: {exc}. Please reply manually.")
            agent.set_paused(psid, True, "Bot error")
            reply(psid, "Sorry, I'm having a small technical problem. A team member will reply to you shortly.\n"
                        "សូមអភ័យទោស មានបញ្ហាបច្ចេកទេសបន្តិច។ បុគ្គលិករបស់យើងនឹងឆ្លើយតបអ្នកក្នុងពេលឆាប់ៗនេះ។")
            return
        if not text_out:
            text_out = t(psid, "Sorry, could you say that again?", "សូមអភ័យទោស តើអ្នកអាចនិយាយម្តងទៀតបានទេ?")
        reply(psid, text_out, runner.outbox_images)


# ---------- owner-triggered messages ----------
def _notify(b: dict, what: str, en: str, km: str):
    """Message the guest about their booking. Returns None if it wasn't made through a chat."""
    if not b.get("psid") or b["psid"].startswith("TEST-"):
        return None
    ok = reply(b["psid"], t(b["psid"], en, km))
    if not ok:
        _owner_must_contact(b, what)
    return ok


def notify_guest_confirmed(b: dict):
    s = booking.booking_summary_text(b, db.get_setting("currency"))
    return _notify(b, "deposit confirmed",
                   f"Your deposit is received and your booking is CONFIRMED. See you soon!\n\n{s}",
                   f"យើងបានទទួលប្រាក់កក់ ហើយការកក់របស់អ្នកត្រូវបានបញ្ជាក់។ រង់ចាំស្វាគមន៍អ្នក!\n\n{s}")


def notify_guest_cancelled(b: dict, reason: str = ""):
    extra = f"\n{reason}" if reason else ""
    return _notify(b, "booking cancelled",
                   f"Your booking {b['code']} ({b['check_in']} to {b['check_out']}) has been cancelled.{extra}",
                   f"ការកក់ {b['code']} ({b['check_in']} ដល់ {b['check_out']}) ត្រូវបានលុបចោល។{extra}")


def notify_guest_proof_rejected(b: dict, reason: str = ""):
    return _notify(b, "payment not verified",
                   f"We could not verify the payment for booking {b['code']}. {reason}".strip()
                   + "\nPlease check and send the screenshot again.",
                   f"យើងមិនអាចផ្ទៀងផ្ទាត់ការបង់ប្រាក់សម្រាប់ការកក់ {b['code']} បានទេ។ {reason}".strip()
                   + "\nសូមពិនិត្យ ហើយផ្ញើរូបភាពម្តងទៀត។")


def notify_guest_expired(b: dict):
    return _notify(b, "deposit hold expired",
                   f"We didn't receive the deposit for booking {b['code']} in time, so the room hold was released. "
                   "Message us anytime if you'd like to book again.",
                   f"យើងមិនបានទទួលប្រាក់កក់សម្រាប់ការកក់ {b['code']} ទាន់ពេលទេ ដូច្នេះបន្ទប់ត្រូវបានដោះលែង។ "
                   "សូមផ្ញើសារមកយើងគ្រប់ពេល ប្រសិនបើអ្នកចង់កក់ម្តងទៀត។")


def staff_send(psid: str, text: str) -> str:
    """Owner replies from the dashboard. Returns '' on success, otherwise an error message."""
    try:
        ok = _channel(psid).send_text(psid, text, human=True)
    except messenger.OutsideWindow:
        ok = False
    if not ok and telegram.is_telegram(psid):
        return "Telegram did not accept the message. The guest may have blocked the bot. Please contact them by phone."
    if not ok:
        return ("Meta did not accept the message. The guest's last message is probably older than 24 hours "
                "(7 days if your app has the Human Agent permission). Please contact them by phone.")
    agent.log_message(psid, "staff", text)
    agent.set_paused(psid, True, "Owner replying from dashboard")
    with db.get_conn() as conn:
        conn.execute("UPDATE guests SET needs_attention = 0 WHERE psid = ?", (psid,))
    return ""


def run_expiry_job() -> int:
    expired = booking.expire_holds()
    for b in expired:
        notify.owner_alert(f"Deposit hold expired for {b['code']} ({b['guest_name']}). Room released.")
        try:
            notify_guest_expired(b)
        except Exception:
            log.exception("Could not notify guest about expiry")
    return len(expired)
