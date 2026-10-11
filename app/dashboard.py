"""Owner dashboard (mobile friendly). Password protected with ADMIN_PASSWORD."""
import hmac
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import agent, booking, bot, config, db

router = APIRouter()
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
TEST_PSID = "TEST-dashboard"
ALLOWED_IMG = {".jpg", ".jpeg", ".png", ".webp"}


def _localtime(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone(booking.tz()).strftime("%d %b %Y %H:%M")
    except ValueError:
        return value


def _nice_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%a %d %b %Y")
    except (TypeError, ValueError):
        return value or ""


templates.env.filters["localtime"] = _localtime
templates.env.filters["nicedate"] = _nice_date
templates.env.globals["STATUS_LABELS"] = booking.STATUS_LABELS


# ---------- helpers ----------
def require_login(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": msg, "kind": kind}


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    ctx = dict(ctx or {})
    ctx.setdefault("s", db.get_settings())
    ctx["flash"] = request.session.pop("flash", None)
    with db.get_conn() as conn:
        ctx["attention_count"] = conn.execute("SELECT COUNT(*) FROM guests WHERE needs_attention = 1").fetchone()[0]
        ctx["pending_count"] = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE status='pending_deposit'").fetchone()[0]
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def back(url: str):
    return RedirectResponse(url, status_code=303)


async def save_upload(file: UploadFile, prefix: str) -> str:
    ext = "." + (file.filename or "x.jpg").rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_IMG:
        raise booking.BookingError("Please upload a JPG, PNG or WEBP image.")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise booking.BookingError("Image is too large (max 8 MB).")
    name = f"{prefix}_{uuid.uuid4().hex[:10]}{ext}"
    (config.UPLOAD_DIR / name).write_bytes(data)
    return name


# ---------- auth ----------
@router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"s": db.get_settings(), "error": None})


@router.post("/admin/login")
def login(request: Request, password: str = Form(...)):
    if hmac.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode()):
        request.session["admin"] = True
        return back("/admin")
    return templates.TemplateResponse(request, "login.html", {"s": db.get_settings(), "error": "Wrong password"},
                                      status_code=401)


@router.get("/admin/logout")
def logout(request: Request):
    request.session.clear()
    return back("/admin/login")


# ---------- overview ----------
@router.get("/admin", response_class=HTMLResponse)
def overview(request: Request):
    require_login(request)
    today = booking.today_local().isoformat()
    with db.get_conn() as conn:
        q = ("SELECT b.*, r.name AS room_name FROM bookings b JOIN room_types r ON r.id=b.room_type_id ")
        arrivals = conn.execute(q + "WHERE b.check_in=? AND b.status IN ('confirmed','pending_deposit','checked_in')"
                                " ORDER BY b.id", (today,)).fetchall()
        departures = conn.execute(q + "WHERE b.check_out=? AND b.status IN ('checked_in','confirmed')"
                                  " ORDER BY b.id", (today,)).fetchall()
        in_house = conn.execute(q + "WHERE b.status='checked_in' ORDER BY b.check_out").fetchall()
        pending = conn.execute(q + "WHERE b.status='pending_deposit' ORDER BY b.payment_proof DESC, b.created_at"
                               ).fetchall()
        upcoming = conn.execute(q + "WHERE b.check_in>? AND b.status IN ('confirmed','pending_deposit')"
                                " ORDER BY b.check_in LIMIT 15", (today,)).fetchall()
        attention = conn.execute("SELECT * FROM guests WHERE needs_attention=1 ORDER BY last_user_msg_at DESC"
                                 ).fetchall()
    dates, grid = booking.calendar_grid(booking.today_local(), 7)
    return render(request, "overview.html", {
        "arrivals": arrivals, "departures": departures, "in_house": in_house, "pending": pending,
        "upcoming": upcoming, "attention": attention, "dates": dates, "grid": grid, "today": today})


# ---------- bookings ----------
@router.get("/admin/bookings", response_class=HTMLResponse)
def bookings_list(request: Request, status: str = "", q: str = "", upcoming: str = "1"):
    require_login(request)
    rows = booking.list_bookings(status=status or None, search=q or None, upcoming_only=(upcoming == "1"))
    return render(request, "bookings.html", {"rows": rows, "status": status, "q": q, "upcoming": upcoming})


@router.get("/admin/bookings/new", response_class=HTMLResponse)
def booking_new_page(request: Request, check_in: str = "", check_out: str = ""):
    require_login(request)
    today = booking.today_local()
    return render(request, "booking_new.html", {
        "rooms": booking.list_room_types(),
        "check_in": check_in or today.isoformat(),
        "check_out": check_out or (today + timedelta(days=1)).isoformat()})


@router.post("/admin/bookings/new")
def booking_new(request: Request, room_type_id: int = Form(...), check_in: str = Form(...),
                check_out: str = Form(...), num_guests: int = Form(1), guest_name: str = Form(...),
                phone: str = Form(""), payment_method: str = Form("arrival"), deposit_paid: str = Form(""),
                special_requests: str = Form(""), notes: str = Form("")):
    require_login(request)
    try:
        b = booking.create_booking(room_type_id=room_type_id, check_in=check_in, check_out=check_out,
                                   num_guests=num_guests, guest_name=guest_name, phone=phone,
                                   payment_method=payment_method, special_requests=special_requests,
                                   notes=notes, source="manual", skip_rules=True)
        if payment_method == "deposit" and deposit_paid:
            b = booking.confirm_deposit(b["id"])
        elif payment_method == "deposit":
            # manual deposit bookings don't auto-expire; the owner tracks them
            booking.attach_payment_proof(b["id"], "")
    except booking.BookingError as exc:
        flash(request, str(exc), "err")
        return back(f"/admin/bookings/new?check_in={check_in}&check_out={check_out}")
    flash(request, f"Booking {b['code']} created.")
    return back(f"/admin/bookings/{b['id']}")


@router.get("/admin/bookings/{booking_id}", response_class=HTMLResponse)
def booking_detail(request: Request, booking_id: int):
    require_login(request)
    b = booking.get_booking(booking_id=booking_id)
    if not b:
        raise HTTPException(404)
    guest = agent.get_guest(b["psid"]) if b["psid"] else {}
    return render(request, "booking_detail.html", {"b": b, "guest": guest})


def _told(fn, *args) -> str:
    """Run a guest notification and describe the result for the owner."""
    try:
        ok = fn(*args)
    except Exception:  # the database change is already saved; never show a 500 here
        ok = False
    if ok is None:
        return ""
    return ("The guest was told on Messenger." if ok else
            "Could NOT message the guest on Messenger (24-hour limit or error). Please call them.")


@router.post("/admin/bookings/{booking_id}/action")
def booking_action(request: Request, booking_id: int, action: str = Form(...), reason: str = Form("")):
    require_login(request)
    try:
        if action == "confirm_deposit":
            b = booking.confirm_deposit(booking_id)
            flash(request, "Deposit confirmed. " + _told(bot.notify_guest_confirmed, b))
        elif action == "reject_proof":
            b = booking.reject_payment_proof(booking_id)
            flash(request, "Screenshot rejected; the room is held again for "
                  f"{db.get_setting('hold_hours')}h. " + _told(bot.notify_guest_proof_rejected, b, reason))
        elif action == "cancel":
            b = booking.update_status(booking_id, "cancelled")
            flash(request, "Booking cancelled. " + _told(bot.notify_guest_cancelled, b, reason))
        elif action in ("check_in", "check_out", "no_show"):
            status = {"check_in": "checked_in", "check_out": "checked_out", "no_show": "no_show"}[action]
            booking.update_status(booking_id, status)
            flash(request, f"Marked as {booking.STATUS_LABELS[status]}.")
        elif action == "reactivate":
            booking.update_status(booking_id, "confirmed")
            flash(request, "Booking re-activated as confirmed.")
        else:
            flash(request, "Unknown action", "err")
    except booking.BookingError as exc:
        flash(request, str(exc), "err")
    return back(f"/admin/bookings/{booking_id}")


@router.post("/admin/bookings/{booking_id}/edit")
def booking_edit(request: Request, booking_id: int, guest_name: str = Form(...), phone: str = Form(""),
                 num_guests: int = Form(1), special_requests: str = Form(""), notes: str = Form("")):
    require_login(request)
    booking.update_booking_fields(booking_id, guest_name=guest_name, phone=phone, num_guests=num_guests,
                                  special_requests=special_requests, notes=notes)
    flash(request, "Saved.")
    return back(f"/admin/bookings/{booking_id}")


@router.get("/admin/proof/{filename}")
def proof_file(request: Request, filename: str):
    require_login(request)
    if "/" in filename or ".." in filename:
        raise HTTPException(404)
    path = config.UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


# ---------- calendar & blocks ----------
@router.get("/admin/calendar", response_class=HTMLResponse)
def calendar(request: Request, start: str = ""):
    require_login(request)
    try:
        start_d = booking.parse_date(start) if start else booking.today_local()
    except booking.BookingError:
        start_d = booking.today_local()
    dates, grid = booking.calendar_grid(start_d, 14)
    with db.get_conn() as conn:
        blocks = conn.execute("SELECT b.*, r.name AS room_name FROM blocks b JOIN room_types r ON r.id=b.room_type_id"
                              " WHERE b.date_to >= ? ORDER BY b.date_from", (booking.today_local().isoformat(),)
                              ).fetchall()
    return render(request, "calendar.html", {
        "dates": dates, "grid": grid, "blocks": blocks, "rooms": booking.list_room_types(),
        "prev": (start_d - timedelta(days=14)).isoformat(), "next": (start_d + timedelta(days=14)).isoformat(),
        "start": start_d.isoformat()})


@router.post("/admin/blocks")
def add_block(request: Request, room_type_id: int = Form(...), date_from: str = Form(...),
              date_to: str = Form(...), units: int = Form(1), reason: str = Form("")):
    require_login(request)
    try:
        if booking.parse_date(date_to) < booking.parse_date(date_from):
            raise booking.BookingError("'Last night' must be on or after 'From'.")
        with db.get_conn() as conn:
            conn.execute("INSERT INTO blocks(room_type_id, date_from, date_to, units, reason) VALUES (?,?,?,?,?)",
                         (room_type_id, date_from, date_to, max(units, 1), reason))
        flash(request, "Rooms blocked.")
    except booking.BookingError as exc:
        flash(request, str(exc), "err")
    return back("/admin/calendar")


@router.post("/admin/blocks/{block_id}/delete")
def delete_block(request: Request, block_id: int):
    require_login(request)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM blocks WHERE id=?", (block_id,))
    flash(request, "Block removed.")
    return back("/admin/calendar")


# ---------- rooms & rates ----------
@router.get("/admin/rooms", response_class=HTMLResponse)
def rooms_page(request: Request):
    require_login(request)
    with db.get_conn() as conn:
        rates = conn.execute("SELECT o.*, r.name AS room_name FROM rate_overrides o JOIN room_types r"
                             " ON r.id=o.room_type_id WHERE o.date_to >= ? ORDER BY o.date_from",
                             (booking.today_local().isoformat(),)).fetchall()
    return render(request, "rooms.html", {"rooms": booking.list_room_types(active_only=False), "rates": rates})


@router.post("/admin/rooms")
def room_save(request: Request, room_id: int = Form(0), name: str = Form(...), name_km: str = Form(""),
              description: str = Form(""), description_km: str = Form(""), capacity: int = Form(2),
              quantity: int = Form(1), price: float = Form(...), active: str = Form(""), sort: int = Form(0)):
    require_login(request)
    vals = (name.strip(), name_km.strip(), description.strip(), description_km.strip(), max(capacity, 1),
            max(quantity, 0), max(price, 0), 1 if active else 0, sort)
    with db.get_conn() as conn:
        if room_id:
            conn.execute("UPDATE room_types SET name=?, name_km=?, description=?, description_km=?, capacity=?,"
                         " quantity=?, price=?, active=?, sort=? WHERE id=?", (*vals, room_id))
        else:
            conn.execute("INSERT INTO room_types(name, name_km, description, description_km, capacity, quantity,"
                         " price, active, sort) VALUES (?,?,?,?,?,?,?,?,?)", vals)
    flash(request, "Room type saved.")
    return back("/admin/rooms")


@router.post("/admin/rooms/{room_id}/photos")
async def room_photo_upload(request: Request, room_id: int, photo: UploadFile = File(...)):
    require_login(request)
    try:
        name = await save_upload(photo, f"room{room_id}")
        with db.get_conn() as conn:
            rt = booking.get_room_type(conn, room_id)
            if not rt:
                raise HTTPException(404)
            photos = rt["photos"] + [name]
            conn.execute("UPDATE room_types SET photos=? WHERE id=?", (json.dumps(photos), room_id))
        flash(request, "Photo added.")
    except booking.BookingError as exc:
        flash(request, str(exc), "err")
    return back("/admin/rooms")


@router.post("/admin/rooms/{room_id}/photos/delete")
def room_photo_delete(request: Request, room_id: int, photo: str = Form(...)):
    require_login(request)
    with db.get_conn() as conn:
        rt = booking.get_room_type(conn, room_id)
        if not rt or photo not in rt["photos"] or "/" in photo or "\\" in photo or ".." in photo:
            raise HTTPException(404)
        photos = [p for p in rt["photos"] if p != photo]
        conn.execute("UPDATE room_types SET photos=? WHERE id=?", (json.dumps(photos), room_id))
    (config.UPLOAD_DIR / photo).unlink(missing_ok=True)
    flash(request, "Photo removed.")
    return back("/admin/rooms")


@router.post("/admin/rates")
def rate_add(request: Request, room_type_id: int = Form(...), date_from: str = Form(...),
             date_to: str = Form(...), price: float = Form(...), note: str = Form("")):
    require_login(request)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO rate_overrides(room_type_id, date_from, date_to, price, note) VALUES (?,?,?,?,?)",
                     (room_type_id, date_from, date_to, price, note))
    flash(request, "Special price added.")
    return back("/admin/rooms")


@router.post("/admin/rates/{rate_id}/delete")
def rate_delete(request: Request, rate_id: int):
    require_login(request)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rate_overrides WHERE id=?", (rate_id,))
    flash(request, "Special price removed.")
    return back("/admin/rooms")


# ---------- hotel info & settings ----------
INFO_KEYS = ["hotel_name", "address", "map_link", "phone", "check_in_time", "check_out_time", "hotel_info"]
SETTING_KEYS = ["currency", "deposit_percent", "hold_hours", "payment_instructions", "auto_resume_hours",
                "max_nights", "max_advance_days"]
SETTING_FLAGS = ["allow_pay_on_arrival", "allow_deposit", "bot_enabled"]


@router.get("/admin/info", response_class=HTMLResponse)
def info_page(request: Request):
    require_login(request)
    return render(request, "info.html")


@router.post("/admin/info")
async def info_save(request: Request):
    require_login(request)
    form = await request.form()
    db.set_settings({k: (form.get(k) or "").strip() for k in INFO_KEYS})
    flash(request, "Hotel information saved. The bot uses it immediately.")
    return back("/admin/info")


@router.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    require_login(request)
    return render(request, "settings.html", {"webhook_url": f"{config.BASE_URL}/webhook",
                                              "privacy_url": f"{config.BASE_URL}/privacy",
                                              "model": config.AI_MODEL,
                                              "fb_ready": bool(config.FB_PAGE_ACCESS_TOKEN),
                                              "ai_ready": bool(config.ANTHROPIC_API_KEY),
                                              "tg_ready": bool(config.TELEGRAM_BOT_TOKEN)})


@router.post("/admin/settings")
async def settings_save(request: Request):
    require_login(request)
    form = await request.form()
    vals = {k: (form.get(k) or "").strip() for k in SETTING_KEYS}
    vals.update({k: "1" if form.get(k) else "0" for k in SETTING_FLAGS})
    if vals["allow_pay_on_arrival"] == "0" and vals["allow_deposit"] == "0":
        flash(request, "Enable at least one payment option.", "err")
        return back("/admin/settings")
    limits = {"deposit_percent": (1, 100), "hold_hours": (0.5, 168), "auto_resume_hours": (0, 720),
              "max_nights": (1, 365), "max_advance_days": (1, 1000)}
    for key, (lo, hi) in limits.items():
        try:
            num = float(vals[key])
        except ValueError:
            num = None
        if num is None or not lo <= num <= hi:
            flash(request, f"'{key.replace('_', ' ')}' must be a number between {lo} and {hi}.", "err")
            return back("/admin/settings")
        vals[key] = str(int(num)) if num == int(num) else str(num)
    if not vals["currency"]:
        vals["currency"] = "USD"
    db.set_settings(vals)
    flash(request, "Settings saved.")
    return back("/admin/settings")


@router.post("/admin/settings/khqr")
async def khqr_upload(request: Request, qr: UploadFile = File(...)):
    require_login(request)
    try:
        name = await save_upload(qr, "khqr")
        db.set_settings({"khqr_image": name})
        flash(request, "KHQR image saved.")
    except booking.BookingError as exc:
        flash(request, str(exc), "err")
    return back("/admin/settings")


# ---------- conversations ----------
@router.get("/admin/conversations", response_class=HTMLResponse)
def conversations(request: Request):
    require_login(request)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT g.*, (SELECT content FROM messages m WHERE m.psid=g.psid ORDER BY id DESC LIMIT 1) AS last_msg"
            " FROM guests g WHERE g.psid != ? ORDER BY g.needs_attention DESC, g.last_user_msg_at DESC LIMIT 200",
            (TEST_PSID,)).fetchall()
    return render(request, "conversations.html", {"rows": rows})


@router.get("/admin/conversations/{psid}", response_class=HTMLResponse)
def conversation(request: Request, psid: str):
    require_login(request)
    guest = agent.get_guest(psid)
    if not guest:
        raise HTTPException(404)
    with db.get_conn() as conn:
        msgs = conn.execute("SELECT * FROM messages WHERE psid=? ORDER BY id DESC LIMIT 200", (psid,)).fetchall()
    return render(request, "conversation.html", {
        "g": guest, "msgs": list(reversed(msgs)), "bookings": booking.list_bookings(psid=psid, limit=20)})


@router.post("/admin/conversations/{psid}/send")
def conversation_send(request: Request, psid: str, text: str = Form(...)):
    require_login(request)
    if text.strip():
        err = bot.staff_send(psid, text.strip())
        if err:
            flash(request, err, "err")
        else:
            flash(request, "Sent. The bot is paused for this guest until you resume it.")
    return back(f"/admin/conversations/{psid}")


@router.post("/admin/conversations/{psid}/pause")
def conversation_pause(request: Request, psid: str):
    require_login(request)
    agent.set_paused(psid, True, "Paused by owner")
    with db.get_conn() as conn:
        conn.execute("UPDATE guests SET needs_attention=0 WHERE psid=?", (psid,))
    flash(request, "Bot paused for this guest. You reply yourself.")
    return back(f"/admin/conversations/{psid}")


@router.post("/admin/conversations/{psid}/resume")
def conversation_resume(request: Request, psid: str):
    require_login(request)
    agent.set_paused(psid, False)
    flash(request, "Bot resumed for this guest.")
    return back(f"/admin/conversations/{psid}")


@router.post("/admin/conversations/{psid}/done")
def conversation_done(request: Request, psid: str):
    require_login(request)
    with db.get_conn() as conn:
        conn.execute("UPDATE guests SET needs_attention=0 WHERE psid=?", (psid,))
    return back("/admin/conversations")


# ---------- test chat (try the bot without Messenger) ----------
@router.get("/admin/test-chat", response_class=HTMLResponse)
def test_chat_page(request: Request):
    require_login(request)
    with db.get_conn() as conn:
        msgs = conn.execute("SELECT * FROM messages WHERE psid=? ORDER BY id", (TEST_PSID,)).fetchall()
    return render(request, "test_chat.html", {"msgs": msgs, "ai_ready": bool(config.ANTHROPIC_API_KEY)})


@router.post("/admin/test-chat")
async def test_chat_send(request: Request):
    require_login(request)
    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        return JSONResponse({"reply": "", "images": []})
    with db.get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO guests(psid, name, first_seen) VALUES (?,?,?)",
                     (TEST_PSID, "Test Guest", db.utcnow()))
    agent.log_message(TEST_PSID, "user", text)
    try:
        import asyncio
        reply, runner = await asyncio.to_thread(agent.generate_reply, TEST_PSID)
    except Exception as exc:  # show the real error to the owner
        return JSONResponse({"reply": f"[Error: {exc}]", "images": []}, status_code=200)
    if reply:
        agent.log_message(TEST_PSID, "assistant", reply)
    if runner.handed_off:
        agent.set_paused(TEST_PSID, False)  # don't leave the test guest paused
        reply += "\n\n[Test note: in real chats the bot would now pause and alert you.]"
    return JSONResponse({"reply": reply, "images": runner.outbox_images})


@router.post("/admin/test-chat/reset")
def test_chat_reset(request: Request):
    require_login(request)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE psid=?", (TEST_PSID,))
        conn.execute("UPDATE bookings SET status='cancelled' WHERE psid=? AND status IN"
                     " ('pending_deposit','confirmed')", (TEST_PSID,))
    flash(request, "Test chat cleared and test bookings cancelled.")
    return back("/admin/test-chat")
