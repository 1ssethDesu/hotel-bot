"""Booking engine: availability, pricing, holds and booking lifecycle."""
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db

ACTIVE_STATUSES = ("pending_deposit", "confirmed", "checked_in")
STATUS_LABELS = {
    "pending_deposit": "Waiting for deposit",
    "confirmed": "Confirmed",
    "checked_in": "Checked in",
    "checked_out": "Checked out",
    "cancelled": "Cancelled",
    "expired": "Expired (deposit not paid)",
    "no_show": "No-show",
}


class BookingError(Exception):
    pass


# ---------- dates ----------
def tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def today_local() -> date:
    return datetime.now(tz()).date()


def parse_date(value) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as exc:
        raise BookingError(f"Invalid date '{value}'. Use YYYY-MM-DD.") from exc


def nights_between(check_in: date, check_out: date):
    d = check_in
    while d < check_out:
        yield d
        d += timedelta(days=1)


def validate_stay(check_in: date, check_out: date, settings: dict) -> None:
    today = today_local()
    if check_in < today:
        raise BookingError("Check-in date is in the past.")
    if check_out <= check_in:
        raise BookingError("Check-out must be after check-in.")
    nights = (check_out - check_in).days
    if nights > int(settings.get("max_nights") or 30):
        raise BookingError(f"Stays longer than {settings.get('max_nights')} nights must be arranged with staff.")
    if (check_in - today).days > int(settings.get("max_advance_days") or 365):
        raise BookingError("That date is too far in the future to book online.")


# ---------- room types ----------
def list_room_types(conn=None, active_only=True):
    own = conn is None
    conn = conn or db.connect()
    try:
        q = "SELECT * FROM room_types"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY sort, id"
        return [db.row_to_dict(r) for r in conn.execute(q).fetchall()]
    finally:
        if own:
            conn.close()


def get_room_type(conn, room_type_id: int):
    return db.row_to_dict(conn.execute("SELECT * FROM room_types WHERE id = ?", (room_type_id,)).fetchone())


# ---------- pricing ----------
def price_for_night(conn, room_type: dict, night: date) -> float:
    row = conn.execute(
        "SELECT price FROM rate_overrides WHERE room_type_id = ? AND date_from <= ? AND date_to >= ?"
        " ORDER BY id DESC LIMIT 1",
        (room_type["id"], night.isoformat(), night.isoformat()),
    ).fetchone()
    return float(row["price"]) if row else float(room_type["price"])


def quote(conn, room_type: dict, check_in: date, check_out: date) -> dict:
    nightly = [(n.isoformat(), price_for_night(conn, room_type, n)) for n in nights_between(check_in, check_out)]
    total = round(sum(p for _, p in nightly), 2)
    return {"nightly": nightly, "total": total, "nights": len(nightly)}


# ---------- availability ----------
def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def units_used(conn, room_type_id: int, night: date, exclude_booking_id=None) -> int:
    n = night.isoformat()
    params = [room_type_id, n, n, _now_iso()]
    q = (
        "SELECT COUNT(*) FROM bookings WHERE room_type_id = ? AND check_in <= ? AND check_out > ?"
        " AND (status IN ('confirmed','checked_in')"
        "      OR (status = 'pending_deposit' AND (hold_expires_at IS NULL OR hold_expires_at > ?)))"
    )
    if exclude_booking_id:
        q += " AND id != ?"
        params.append(exclude_booking_id)
    booked = conn.execute(q, params).fetchone()[0]
    blocked = conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM blocks WHERE room_type_id = ? AND date_from <= ? AND date_to >= ?",
        (room_type_id, n, n),
    ).fetchone()[0]
    return int(booked) + int(blocked)


def available_units(conn, room_type: dict, check_in: date, check_out: date, exclude_booking_id=None) -> int:
    free = room_type["quantity"]
    for night in nights_between(check_in, check_out):
        free = min(free, room_type["quantity"] - units_used(conn, room_type["id"], night, exclude_booking_id))
    return max(free, 0)


def check_availability(check_in, check_out, num_guests=None) -> dict:
    settings = db.get_settings()
    ci, co = parse_date(check_in), parse_date(check_out)
    validate_stay(ci, co, settings)
    results = []
    with db.get_conn() as conn:
        for rt in list_room_types(conn):
            if num_guests and int(num_guests) > rt["capacity"]:
                fits = False
            else:
                fits = True
            free = available_units(conn, rt, ci, co)
            q = quote(conn, rt, ci, co)
            results.append({
                "room_type_id": rt["id"],
                "name": rt["name"],
                "name_km": rt["name_km"],
                "capacity": rt["capacity"],
                "rooms_available": free,
                "fits_party_size": fits,
                "nights": q["nights"],
                "total_price": q["total"],
                "price_per_night": q["nightly"],
                "has_photos": bool(rt["photos"]),
            })
    return {
        "check_in": ci.isoformat(),
        "check_out": co.isoformat(),
        "currency": settings["currency"],
        "room_types": results,
    }


def calendar_grid(start: date, days: int = 14):
    """Availability per room type per night, for the dashboard calendar."""
    dates = [start + timedelta(days=i) for i in range(days)]
    rows = []
    with db.get_conn() as conn:
        for rt in list_room_types(conn):
            cells = []
            for d in dates:
                used = units_used(conn, rt["id"], d)
                cells.append({"date": d.isoformat(), "free": max(rt["quantity"] - used, 0), "total": rt["quantity"]})
            rows.append({"room_type": rt, "cells": cells})
    return dates, rows


# ---------- bookings ----------
def _new_code(conn) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "BK" + "".join(secrets.choice(alphabet) for _ in range(5))
        if not conn.execute("SELECT 1 FROM bookings WHERE code = ?", (code,)).fetchone():
            return code


def _as_int(value, what: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise BookingError(f"Invalid {what}: {value!r}") from exc


def _setting_num(settings: dict, key: str, default: float) -> float:
    try:
        return float(str(settings.get(key) or default).strip().rstrip("%"))
    except ValueError:
        return default


def new_hold_expiry(settings: dict) -> str:
    hours = _setting_num(settings, "hold_hours", 12)
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _hold_expired(b) -> bool:
    return b["status"] == "pending_deposit" and bool(b["hold_expires_at"]) and b["hold_expires_at"] <= _now_iso()


def create_booking(*, room_type_id, check_in, check_out, num_guests, guest_name, phone="",
                   payment_method="arrival", special_requests="", psid=None, source="messenger",
                   notes="", skip_rules=False) -> dict:
    settings = db.get_settings()
    ci, co = parse_date(check_in), parse_date(check_out)
    if not skip_rules:
        validate_stay(ci, co, settings)
    elif co <= ci:
        raise BookingError("Check-out must be after check-in.")
    guest_name = str(guest_name or "").strip()
    if not guest_name:
        raise BookingError("Guest name is required.")
    num_guests = _as_int(num_guests, "number of guests")
    if num_guests < 1:
        raise BookingError("Number of guests must be at least 1.")
    phone, special_requests = str(phone or "").strip(), str(special_requests or "").strip()
    payment_method = "deposit" if payment_method == "deposit" else "arrival"
    if source == "messenger":
        if payment_method == "arrival" and settings["allow_pay_on_arrival"] != "1":
            raise BookingError("Pay on arrival is not available. A deposit is required.")
        if payment_method == "deposit" and settings["allow_deposit"] != "1":
            raise BookingError("Online deposit is not available. Guests pay on arrival.")

    with db.transaction() as conn:
        rt = get_room_type(conn, _as_int(room_type_id, "room type"))
        if not rt or not rt["active"]:
            raise BookingError("Unknown room type.")
        if int(num_guests) > rt["capacity"]:
            raise BookingError(f"{rt['name']} fits at most {rt['capacity']} guests.")
        if available_units(conn, rt, ci, co) < 1:
            raise BookingError(f"Sorry, {rt['name']} is no longer available for those dates.")
        q = quote(conn, rt, ci, co)
        now = db.utcnow()
        if payment_method == "deposit":
            pct = _setting_num(settings, "deposit_percent", 30)
            deposit = round(q["total"] * pct / 100, 2)
            status = "pending_deposit"
            hold = new_hold_expiry(settings)
        else:
            deposit, status, hold = 0.0, "confirmed", None
        code = _new_code(conn)
        conn.execute(
            "INSERT INTO bookings(code, psid, source, room_type_id, check_in, check_out, nights, num_guests,"
            " guest_name, phone, total, deposit_amount, payment_method, status, special_requests, notes,"
            " hold_expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, psid, source, rt["id"], ci.isoformat(), co.isoformat(), q["nights"], int(num_guests),
             guest_name, phone or "", q["total"], deposit, payment_method, status, special_requests or "",
             notes or "", hold, now, now),
        )
    return get_booking(code=code)


def get_booking(booking_id=None, code=None) -> dict:
    with db.get_conn() as conn:
        if booking_id is not None:
            row = conn.execute(
                "SELECT b.*, r.name AS room_name, r.name_km AS room_name_km FROM bookings b"
                " JOIN room_types r ON r.id = b.room_type_id WHERE b.id = ?", (booking_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT b.*, r.name AS room_name, r.name_km AS room_name_km FROM bookings b"
                " JOIN room_types r ON r.id = b.room_type_id WHERE b.code = ?", ((code or "").upper().strip(),)
            ).fetchone()
    return dict(row) if row else None


def list_bookings(status=None, psid=None, upcoming_only=False, search=None, limit=200):
    q = ("SELECT b.*, r.name AS room_name FROM bookings b JOIN room_types r ON r.id = b.room_type_id WHERE 1=1")
    params = []
    if status:
        q += " AND b.status = ?"
        params.append(status)
    if psid:
        q += " AND b.psid = ?"
        params.append(psid)
    if upcoming_only:
        q += " AND b.check_out >= ?"
        params.append(today_local().isoformat())
    if search:
        q += " AND (b.code LIKE ? OR b.guest_name LIKE ? OR b.phone LIKE ?)"
        s = f"%{search}%"
        params += [s, s, s]
    q += " ORDER BY b.check_in DESC, b.id DESC LIMIT ?"
    params.append(limit)
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def update_status(booking_id: int, status: str, deposit_paid=None) -> dict:
    if status not in STATUS_LABELS:
        raise BookingError("Unknown status")
    with db.transaction() as conn:
        b = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if not b:
            raise BookingError("Booking not found")
        # Re-activating a cancelled/expired booking must not overbook.
        if status in ACTIVE_STATUSES and (b["status"] not in ACTIVE_STATUSES or _hold_expired(b)):
            rt = get_room_type(conn, b["room_type_id"])
            if available_units(conn, rt, parse_date(b["check_in"]), parse_date(b["check_out"]), booking_id) < 1:
                raise BookingError("No room of this type is left for those dates (the hold had expired or the "
                                   "booking was inactive). Contact the guest to offer another room or a refund.")
        fields = {"status": status, "updated_at": db.utcnow()}
        if status != "pending_deposit":
            fields["hold_expires_at"] = None
        if deposit_paid is not None:
            fields["deposit_paid"] = 1 if deposit_paid else 0
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE bookings SET {sets} WHERE id = ?", (*fields.values(), booking_id))
    return get_booking(booking_id=booking_id)


def confirm_deposit(booking_id: int) -> dict:
    b = get_booking(booking_id=booking_id)
    if not b:
        raise BookingError("Booking not found")
    if b["status"] != "pending_deposit":
        raise BookingError(f"This booking is no longer waiting for a deposit (status: {STATUS_LABELS[b['status']]}).")
    return update_status(booking_id, "confirmed", deposit_paid=True)


def cancel_booking(code: str, psid=None) -> dict:
    b = get_booking(code=code)
    if not b or (psid is not None and b["psid"] != psid):
        raise BookingError("Booking not found for this conversation.")
    if b["status"] not in ("pending_deposit", "confirmed"):
        raise BookingError(f"This booking cannot be cancelled (status: {STATUS_LABELS[b['status']]}).")
    return update_status(b["id"], "cancelled")


def attach_payment_proof(booking_id: int, proof: str) -> dict:
    """Guest sent a payment screenshot: keep the room held until the owner verifies.

    If the hold had already run out, the room may have been sold meanwhile: re-check first.
    Raises BookingError when the room is gone.
    """
    with db.transaction() as conn:
        b = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if not b or b["status"] not in ("pending_deposit", "expired"):
            raise BookingError("Booking is not waiting for a deposit.")
        if b["status"] == "expired" or _hold_expired(b):
            rt = get_room_type(conn, b["room_type_id"])
            if available_units(conn, rt, parse_date(b["check_in"]), parse_date(b["check_out"]), booking_id) < 1:
                conn.execute("UPDATE bookings SET status='expired', payment_proof=?, updated_at=? WHERE id=?",
                             (proof, db.utcnow(), booking_id))
                raise BookingError("Hold expired and the room was taken.")
        conn.execute(
            "UPDATE bookings SET status='pending_deposit', payment_proof = ?, hold_expires_at = NULL, updated_at = ?"
            " WHERE id = ?", (proof, db.utcnow(), booking_id))
    return get_booking(booking_id=booking_id)


def reject_payment_proof(booking_id: int) -> dict:
    """Owner could not verify the payment: clear the proof and restart the hold timer."""
    b = get_booking(booking_id=booking_id)
    if not b or b["status"] != "pending_deposit":
        raise BookingError("Booking is not waiting for a deposit.")
    with db.get_conn() as conn:
        conn.execute("UPDATE bookings SET payment_proof='', hold_expires_at=?, updated_at=? WHERE id=?",
                     (new_hold_expiry(db.get_settings()), db.utcnow(), booking_id))
    return get_booking(booking_id=booking_id)


def update_booking_fields(booking_id: int, **fields) -> dict:
    allowed = {"guest_name", "phone", "num_guests", "special_requests", "notes"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields:
        fields["updated_at"] = db.utcnow()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with db.get_conn() as conn:
            conn.execute(f"UPDATE bookings SET {sets} WHERE id = ?", (*fields.values(), booking_id))
    return get_booking(booking_id=booking_id)


def expire_holds() -> list:
    """Expire deposit holds that ran out without payment proof. Returns the expired bookings."""
    now = _now_iso()
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id FROM bookings WHERE status = 'pending_deposit' AND hold_expires_at IS NOT NULL"
            " AND hold_expires_at <= ?", (now,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        for i in ids:
            conn.execute("UPDATE bookings SET status='expired', updated_at=? WHERE id=?", (now, i))
    return [get_booking(booking_id=i) for i in ids]


def booking_summary_text(b: dict, currency: str) -> str:
    """Plain text summary used in Messenger confirmations."""
    lines = [
        f"Booking code: {b['code']}",
        f"Room: {b['room_name']}" + (f" ({b['room_name_km']})" if b.get("room_name_km") else ""),
        f"Check-in: {b['check_in']}  Check-out: {b['check_out']} ({b['nights']} night{'s' if b['nights'] != 1 else ''})",
        f"Guests: {b['num_guests']}  Name: {b['guest_name']}",
        f"Total: {b['total']:.2f} {currency}",
    ]
    if b["payment_method"] == "deposit":
        lines.append(f"Deposit: {b['deposit_amount']:.2f} {currency} ({'paid' if b['deposit_paid'] else 'not paid yet'})")
    else:
        lines.append("Payment: pay at the hotel on arrival")
    return "\n".join(lines)
