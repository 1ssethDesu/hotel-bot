"""SQLite storage. One file, no external database needed."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS room_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_km TEXT DEFAULT '',
    description TEXT DEFAULT '',
    description_km TEXT DEFAULT '',
    capacity INTEGER NOT NULL DEFAULT 2,
    quantity INTEGER NOT NULL DEFAULT 1,
    price REAL NOT NULL DEFAULT 0,
    photos TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    sort INTEGER NOT NULL DEFAULT 0
);

-- Special prices for specific nights (e.g. Khmer New Year). date_to is the LAST night (inclusive).
CREATE TABLE IF NOT EXISTS rate_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_type_id INTEGER NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    price REAL NOT NULL,
    note TEXT DEFAULT ''
);

-- Rooms taken out of sale (maintenance, private use). date_to is the LAST night (inclusive).
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_type_id INTEGER NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    units INTEGER NOT NULL DEFAULT 1,
    reason TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS guests (
    psid TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    last_user_msg_at TEXT,
    bot_paused INTEGER NOT NULL DEFAULT 0,
    paused_at TEXT,
    paused_reason TEXT DEFAULT '',
    needs_attention INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    psid TEXT NOT NULL,
    role TEXT NOT NULL,          -- user | assistant | staff | system
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_psid ON messages(psid, id);

-- check_out is the departure date (not a night). Nights = check_in .. check_out-1
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    psid TEXT,
    source TEXT NOT NULL DEFAULT 'messenger',   -- messenger | manual
    room_type_id INTEGER NOT NULL REFERENCES room_types(id),
    check_in TEXT NOT NULL,
    check_out TEXT NOT NULL,
    nights INTEGER NOT NULL,
    num_guests INTEGER NOT NULL DEFAULT 1,
    guest_name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    total REAL NOT NULL,
    deposit_amount REAL NOT NULL DEFAULT 0,
    payment_method TEXT NOT NULL DEFAULT 'arrival', -- arrival | deposit
    status TEXT NOT NULL,          -- pending_deposit | confirmed | checked_in | checked_out | cancelled | expired | no_show
    deposit_paid INTEGER NOT NULL DEFAULT 0,
    payment_proof TEXT DEFAULT '',
    special_requests TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    hold_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_dates ON bookings(room_type_id, check_in, check_out);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_mids (
    mid TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "hotel_name": "My Hotel",
    "currency": "USD",
    "check_in_time": "14:00",
    "check_out_time": "12:00",
    "address": "",
    "map_link": "",
    "phone": "",
    "hotel_info": (
        "About us: A small, friendly hotel.\n"
        "Amenities: Free Wi-Fi, air conditioning, hot water.\n"
        "Breakfast: Not included (edit this).\n"
        "Airport transfer: Ask staff (edit this).\n"
        "Cancellation policy: Free cancellation up to 24 hours before check-in (edit this).\n"
        "Children/extra bed: (edit this)\n"
        "Pets: Not allowed (edit this)."
    ),
    "allow_pay_on_arrival": "1",
    "allow_deposit": "1",
    "deposit_percent": "30",
    "hold_hours": "12",
    "payment_instructions": (
        "Scan the KHQR code with any Cambodian banking app (ABA, ACLEDA, Wing...). "
        "Please put your booking code in the payment remark and send us a screenshot here."
    ),
    "khqr_image": "",
    "auto_resume_hours": "12",
    "max_nights": "30",
    "max_advance_days": "365",
    "bot_enabled": "1",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction():
    """Write transaction that locks the DB immediately (prevents double booking)."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(seed: bool = True) -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if seed and conn.execute("SELECT COUNT(*) FROM room_types").fetchone()[0] == 0:
            samples = [
                ("Standard Double", "បន្ទប់ធម្មតា គ្រែពីរនាក់", "Double bed, air-con, private bathroom, city view.",
                 "គ្រែធំមួយ ម៉ាស៊ីនត្រជាក់ បន្ទប់ទឹកផ្ទាល់ខ្លួន", 2, 4, 25),
                ("Deluxe Twin", "បន្ទប់ពិសេស គ្រែពីរ", "Two single beds, balcony, fridge.",
                 "គ្រែតូចពីរ មានរានហាល និងទូទឹកកក", 2, 3, 35),
                ("Family Room", "បន្ទប់គ្រួសារ", "One double + one single bed, sofa, fits up to 4.",
                 "គ្រែធំមួយ និងគ្រែតូចមួយ ស្នាក់នៅបាន ៤នាក់", 4, 2, 50),
            ]
            for i, s in enumerate(samples):
                conn.execute(
                    "INSERT INTO room_types(name, name_km, description, description_km, capacity, quantity, price, sort)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (*s, i),
                )


# ---------- settings helpers ----------
def get_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def get_setting(key: str) -> str:
    return get_settings().get(key, "")


def set_settings(values: dict) -> None:
    with get_conn() as conn:
        for k, v in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)),
            )


def row_to_dict(row) -> dict:
    if row is None:
        return None
    d = dict(row)
    if "photos" in d and isinstance(d["photos"], str):
        try:
            d["photos"] = json.loads(d["photos"])
        except ValueError:
            d["photos"] = []
    return d
