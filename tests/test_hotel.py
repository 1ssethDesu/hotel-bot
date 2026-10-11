import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent, booking, bot, config, db, messenger, telegram
from app.main import app

bot.DEBOUNCE_SECONDS = 0


@pytest.fixture(autouse=True)
def fresh_db():
    for suffix in ("", "-wal", "-shm"):
        config.DB_PATH.with_name(config.DB_PATH.name + suffix).unlink(missing_ok=True)
    db.init_db()
    yield


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(messenger, "_post", lambda payload: out.append(payload) or {"message_id": "m"})
    return out


def d(days):
    return (booking.today_local() + timedelta(days=days)).isoformat()


def family_id():
    return [r for r in booking.list_room_types() if r["name"] == "Family Room"][0]["id"]


def fam(av):
    return [r for r in av["room_types"] if r["room_type_id"] == family_id()][0]


# ---------------- booking engine ----------------
def test_no_double_booking_and_adjacent_stays():
    fid = family_id()  # 2 family rooms in the seed data
    for i in range(2):
        booking.create_booking(room_type_id=fid, check_in=d(5), check_out=d(8), num_guests=3, guest_name=f"G{i}")
    with pytest.raises(booking.BookingError):
        booking.create_booking(room_type_id=fid, check_in=d(7), check_out=d(9), num_guests=2, guest_name="X")
    # leaving on day 8 frees the room for a day-8 arrival
    booking.create_booking(room_type_id=fid, check_in=d(8), check_out=d(10), num_guests=2, guest_name="Y")
    assert fam(booking.check_availability(d(5), d(6)))["rooms_available"] == 0


def test_capacity_and_date_rules():
    fid = family_id()
    with pytest.raises(booking.BookingError):
        booking.create_booking(room_type_id=fid, check_in=d(1), check_out=d(2), num_guests=9, guest_name="Big")
    with pytest.raises(booking.BookingError):
        booking.create_booking(room_type_id=fid, check_in=d(-1), check_out=d(2), num_guests=1, guest_name="Past")
    with pytest.raises(booking.BookingError):
        booking.check_availability(d(3), d(3))


def test_special_price_and_blocks():
    fid = family_id()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO rate_overrides(room_type_id, date_from, date_to, price) VALUES (?,?,?,?)",
                     (fid, d(10), d(10), 80))
        conn.execute("INSERT INTO blocks(room_type_id, date_from, date_to, units) VALUES (?,?,?,?)",
                     (fid, d(20), d(21), 2))
    assert fam(booking.check_availability(d(9), d(12)))["total_price"] == 50 + 80 + 50
    assert fam(booking.check_availability(d(21), d(23)))["rooms_available"] == 0   # last night inclusive
    assert fam(booking.check_availability(d(22), d(23)))["rooms_available"] == 2


def test_deposit_hold_expires_and_releases_room(sent):
    fid = family_id()
    b1 = booking.create_booking(room_type_id=fid, check_in=d(3), check_out=d(4), num_guests=2, guest_name="A",
                                payment_method="deposit", psid="p1")
    b2 = booking.create_booking(room_type_id=fid, check_in=d(3), check_out=d(4), num_guests=2, guest_name="B",
                                payment_method="deposit", psid="p2")
    assert b1["status"] == "pending_deposit" and b1["deposit_amount"] == 15.0
    booking.attach_payment_proof(b2["id"], "proof_x.jpg")       # B sent a screenshot -> no expiry
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with db.get_conn() as conn:
        conn.execute("UPDATE bookings SET hold_expires_at=? WHERE id=?", (past, b1["id"]))
    agent.ensure_guest("p1")
    agent.log_message("p1", "user", "hi")   # guest chatted recently -> inside the 24h window
    assert bot.run_expiry_job() == 1
    assert booking.get_booking(booking_id=b1["id"])["status"] == "expired"
    assert booking.get_booking(booking_id=b2["id"])["status"] == "pending_deposit"
    assert fam(booking.check_availability(d(3), d(4)))["rooms_available"] == 1
    assert "didn't receive the deposit" in sent[-1]["message"]["text"]
    booking.confirm_deposit(b2["id"])
    assert booking.get_booking(booking_id=b2["id"])["deposit_paid"] == 1


# ---------------- fake Claude ----------------
class Block:
    def __init__(self, **kw):
        self.kw = kw

    def model_dump(self, exclude_none=True):
        return dict(self.kw)


class Resp:
    def __init__(self, blocks):
        self.content = blocks


class FakeClaude:
    """Scripted model: each call pops the next response. Records the requests."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def tool(name, inp, id_="t1"):
    return Block(type="tool_use", id=id_, name=name, input=inp)


def text(t):
    return Block(type="text", text=t)


def webhook(client, psid, msg=None, postback=None, mid="m1"):
    ev = {"sender": {"id": psid}, "recipient": {"id": "PAGE"}, "timestamp": 1}
    if postback:
        ev["postback"] = {"payload": postback, "mid": mid}
    else:
        ev["message"] = {"mid": mid, **(msg or {})}
    return client.post("/webhook", json={"object": "page", "entry": [{"id": "PAGE", "messaging": [ev]}]})


def texts(sent):
    return [p["message"]["text"] for p in sent if "message" in p and "text" in p["message"]]


def images(sent):
    return [p["message"]["attachment"]["payload"]["url"] for p in sent
            if "message" in p and "attachment" in p["message"]]


def test_full_messenger_booking_flow(sent, monkeypatch):
    db.set_settings({"khqr_image": "khqr_abc.png"})
    fid = family_id()
    fake = FakeClaude([
        Resp([tool("check_availability", {"check_in": d(2), "check_out": d(4), "num_guests": 3})]),
        Resp([text("Yes! **Family Room** is free, 100.00 USD for 2 nights. May I have your name and phone?")]),
        Resp([tool("create_booking", {"room_type_id": fid, "check_in": d(2), "check_out": d(4), "num_guests": 3,
                                      "guest_name": "Sok Dara", "phone": "012345678",
                                      "payment_method": "deposit"})]),
        Resp([text("Booked! Please pay the 30.00 USD deposit with the QR below.")]),
    ])
    monkeypatch.setattr(agent, "_client", fake)
    with TestClient(app) as client:
        assert webhook(client, "guest1", {"text": "Room for 3 people, 2 nights?"}, mid="a1").status_code == 200
        webhook(client, "guest1", {"text": "Sok Dara 012345678, deposit please. Yes confirm"}, mid="a2")
        webhook(client, "guest1", {"text": "Sok Dara 012345678, deposit please. Yes confirm"}, mid="a2")  # retry
    assert texts(sent)[0].startswith("Yes! Family Room")       # markdown stripped
    assert "deposit" in texts(sent)[1]
    assert images(sent) == [f"{config.BASE_URL}/media/khqr_abc.png"]
    assert len(fake.calls) == 4                                  # duplicate delivery ignored
    tool_result = fake.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and '"rooms_available": 2' in tool_result["content"]
    assert "TODAY" in fake.calls[0]["system"][0]["text"]
    # history passed on the 2nd turn alternates user/assistant correctly
    roles = [m["role"] for m in fake.calls[2]["messages"]]
    assert roles[:3] == ["user", "assistant", "user"]
    bks = booking.list_bookings(psid="guest1")
    assert len(bks) == 1 and bks[0]["status"] == "pending_deposit" and bks[0]["deposit_amount"] == 30.0

    class R:
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        content = b"\xff\xd8jpeg"
    monkeypatch.setattr(bot.httpx, "get", lambda *a, **k: R())
    with TestClient(app) as client:
        webhook(client, "guest1", {"attachments": [{"type": "image", "payload": {"url": "https://scontent.xx.fbcdn.net/x.jpg"}}]},
                mid="a3")
    b = booking.list_bookings(psid="guest1")[0]
    assert b["payment_proof"].startswith("proof_") and b["hold_expires_at"] is None
    assert "received your payment screenshot" in texts(sent)[-1]
    assert len(fake.calls) == 4                                  # handled without AI


def test_staff_echo_pauses_bot(sent, monkeypatch):
    fake = FakeClaude([Resp([text("សួស្តី! តើខ្ញុំអាចជួយអ្វីបាន?")])])
    monkeypatch.setattr(agent, "_client", fake)
    with TestClient(app) as client:
        webhook(client, "g2", {"text": "សួស្តី"}, mid="k1")
        client.post("/webhook", json={"object": "page", "entry": [{"id": "PAGE", "messaging": [
            {"sender": {"id": "PAGE"}, "recipient": {"id": "g2"},
             "message": {"is_echo": True, "mid": "e1", "text": "I'll handle this, thanks"}}]}]})
        # our own bot echo must NOT pause anything
        client.post("/webhook", json={"object": "page", "entry": [{"id": "PAGE", "messaging": [
            {"sender": {"id": "PAGE"}, "recipient": {"id": "g9"},
             "message": {"is_echo": True, "mid": "e2", "text": "bot msg", "metadata": messenger.BOT_METADATA}}]}]})
        webhook(client, "g2", {"text": "Thanks!"}, mid="k2")
    assert len(fake.calls) == 1
    g = agent.get_guest("g2")
    assert g["bot_paused"] == 1 and g["needs_attention"] == 1
    assert agent.get_guest("g9") == {}
    assert bot.guest_prefers_khmer("g2")


def test_auto_resume_after_staff_silence(sent, monkeypatch):
    fake = FakeClaude([Resp([text("Hi again!")])])
    monkeypatch.setattr(agent, "_client", fake)
    agent.ensure_guest("g6")
    agent.set_paused("g6", True, "test")
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    with db.get_conn() as conn:
        conn.execute("UPDATE guests SET paused_at=? WHERE psid='g6'", (old,))
    with TestClient(app) as client:
        webhook(client, "g6", {"text": "hello?"}, mid="r1")
    assert texts(sent)[-1] == "Hi again!" and agent.get_guest("g6")["bot_paused"] == 0


def test_handoff_tool_and_ai_error(sent, monkeypatch):
    fake = FakeClaude([
        Resp([tool("handoff_to_staff", {"reason": "Wants 50% discount"})]),
        Resp([text("I'll ask our manager, someone will reply soon.")]),
    ])
    monkeypatch.setattr(agent, "_client", fake)
    with TestClient(app) as client:
        webhook(client, "g3", {"text": "Give me 50% off"}, mid="h1")
    assert agent.get_guest("g3")["bot_paused"] == 1
    assert "manager" in texts(sent)[-1]

    class Boom:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("API down")
    monkeypatch.setattr(agent, "_client", Boom())
    with TestClient(app) as client:
        webhook(client, "g4", {"text": "hello"}, mid="h2")
    assert "technical problem" in texts(sent)[-1]


def test_menu_talk_to_staff(sent):
    with TestClient(app) as client:
        webhook(client, "g7", postback="MENU_HUMAN", mid="pb1")
    assert agent.get_guest("g7")["bot_paused"] == 1 and "team member" in texts(sent)[-1]


def test_24h_window_closed_alerts_owner_instead(sent, monkeypatch):
    alerts = []
    monkeypatch.setattr(bot.notify, "owner_alert", alerts.append)
    fid = family_id()
    agent.ensure_guest("g5")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with db.get_conn() as conn:
        conn.execute("UPDATE guests SET last_user_msg_at=? WHERE psid='g5'", (old,))
    b = booking.create_booking(room_type_id=fid, check_in=d(3), check_out=d(4), num_guests=2, guest_name="Z",
                               phone="011", payment_method="deposit", psid="g5")
    assert bot.notify_guest_confirmed(booking.confirm_deposit(b["id"])) is False
    assert sent == [] and "Please call: 011" in alerts[-1]
    # staff reply outside 24h uses the HUMAN_AGENT tag
    bot.staff_send("g5", "Hello from the owner")
    assert sent[-1]["messaging_type"] == "MESSAGE_TAG" and sent[-1]["tag"] == "HUMAN_AGENT"


def test_signature_and_media_privacy(monkeypatch):
    monkeypatch.setattr(config, "FB_APP_SECRET", "s3cret")
    body = b'{"object":"page","entry":[]}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    (config.UPLOAD_DIR / "proof_secret.jpg").write_bytes(b"x")
    with TestClient(app) as client:
        assert client.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
        assert client.post("/webhook", content=body, headers={"x-hub-signature-256": good}).status_code == 200
        assert client.get("/media/proof_secret.jpg").status_code == 404
        assert client.get("/admin/proof/proof_secret.jpg", follow_redirects=False).status_code == 303


def test_dashboard_pages_and_manual_booking(sent):
    with TestClient(app) as client:
        assert client.post("/admin/login", data={"password": "wrong"}).status_code == 401
        client.post("/admin/login", data={"password": config.ADMIN_PASSWORD})
        for p in ["/admin", "/admin/bookings", "/admin/calendar", "/admin/rooms", "/admin/info",
                  "/admin/settings", "/admin/conversations", "/admin/test-chat", "/admin/bookings/new"]:
            assert client.get(p).status_code == 200, p
        r = client.post("/admin/bookings/new", data={
            "room_type_id": family_id(), "check_in": d(1), "check_out": d(3), "num_guests": 2,
            "guest_name": "Walk In", "phone": "099", "payment_method": "arrival"}, follow_redirects=True)
        assert r.status_code == 200 and "Walk In" in r.text
        b = booking.list_bookings(search="Walk In")[0]
        assert b["source"] == "manual" and b["status"] == "confirmed"
        client.post(f"/admin/bookings/{b['id']}/action", data={"action": "check_in"})
        assert booking.get_booking(booking_id=b["id"])["status"] == "checked_in"
        assert client.get(f"/admin/bookings/{b['id']}").status_code == 200


# ---------------- regression tests from code review ----------------
def test_paying_after_hold_expired_when_room_taken(sent, monkeypatch):
    alerts = []
    monkeypatch.setattr(bot.notify, "owner_alert", alerts.append)
    fid = family_id()
    with db.get_conn() as conn:
        conn.execute("UPDATE room_types SET quantity=1 WHERE id=?", (fid,))
    agent.ensure_guest("late")
    agent.log_message("late", "user", "hi")
    b1 = booking.create_booking(room_type_id=fid, check_in=d(3), check_out=d(4), num_guests=2, guest_name="Late",
                                payment_method="deposit", psid="late")
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with db.get_conn() as conn:
        conn.execute("UPDATE bookings SET hold_expires_at=? WHERE id=?", (past, b1["id"]))
    booking.create_booking(room_type_id=fid, check_in=d(3), check_out=d(4), num_guests=2, guest_name="Other")
    monkeypatch.setattr(bot, "_download_proof", lambda url, code: "proof_late.jpg")
    with TestClient(app) as client:
        webhook(client, "late", {"attachments": [{"type": "image", "payload": {"url": "https://x.fbcdn.net/a"}}]},
                mid="L1")
    assert booking.get_booking(booking_id=b1["id"])["status"] == "expired"
    assert "hold had already expired" in texts(sent)[-1] and "AFTER the hold expired" in alerts[-1]
    with pytest.raises(booking.BookingError):
        booking.confirm_deposit(b1["id"])


def test_hidden_tool_arguments_are_ignored():
    r = agent.ToolRunner("x").run("create_booking", {
        "room_type_id": family_id(), "check_in": d(-10), "check_out": d(80), "num_guests": 2,
        "guest_name": "Hacker", "phone": "1", "payment_method": "arrival", "skip_rules": True, "notes": "VIP free"})
    assert "error" in r and booking.list_bookings(search="Hacker") == []
    r = agent.ToolRunner("x").run("check_availability", {"check_in": d(1), "check_out": d(2), "num_guests": "two"})
    assert "error" in r


def test_untrusted_proof_url_is_not_fetched(monkeypatch):
    called = []
    monkeypatch.setattr(bot.httpx, "get", lambda *a, **k: called.append(a))
    assert bot._download_proof("javascript:alert(1)", "BK1") == ""
    assert bot._download_proof("http://169.254.169.254/latest", "BK1") == ""
    assert called == []


def test_reject_proof_restarts_hold_and_confirm_after_cancel_refused(sent):
    fid = family_id()
    b = booking.create_booking(room_type_id=fid, check_in=d(3), check_out=d(4), num_guests=2, guest_name="R",
                               payment_method="deposit", psid="r1")
    booking.attach_payment_proof(b["id"], "proof_r.jpg")
    b = booking.reject_payment_proof(b["id"])
    assert b["payment_proof"] == "" and b["hold_expires_at"]
    booking.cancel_booking(b["code"], psid="r1")
    with pytest.raises(booking.BookingError):
        booking.confirm_deposit(b["id"])


def test_text_from_tool_rounds_is_kept_and_caption_answered(sent, monkeypatch):
    fake = FakeClaude([
        Resp([text("Here are the Family Room photos:"), tool("send_room_photos", {"room_type_id": family_id()})]),
        Resp([]),
    ])
    with db.get_conn() as conn:
        conn.execute("UPDATE room_types SET photos=? WHERE id=?", ('["room3_a.jpg"]', family_id()))
    monkeypatch.setattr(agent, "_client", fake)
    with TestClient(app) as client:
        webhook(client, "ph", {"text": "photos of family room?"}, mid="P1")
    assert texts(sent)[-1] == "Here are the Family Room photos:"
    assert images(sent)[-1].endswith("/media/room3_a.jpg")

    # screenshot + caption: proof stored AND the caption gets an AI answer; history ends with the user turn
    fid = family_id()
    booking.create_booking(room_type_id=fid, check_in=d(3), check_out=d(4), num_guests=2, guest_name="C",
                           payment_method="deposit", psid="ph")
    fake2 = FakeClaude([Resp([text("Yes, airport pickup is available.")])])
    monkeypatch.setattr(agent, "_client", fake2)
    monkeypatch.setattr(bot, "_download_proof", lambda url, code: "proof_c.jpg")
    with TestClient(app) as client:
        webhook(client, "ph", {"text": "Paid! Do you do airport pickup?",
                               "attachments": [{"type": "image", "payload": {"url": "https://a.fbcdn.net/b"}}]},
                mid="P2")
    assert texts(sent)[-1] == "Yes, airport pickup is available."
    msgs = fake2.calls[0]["messages"]
    assert msgs[-1]["role"] == "user" and "airport pickup" in msgs[-1]["content"]
    assert "[System] Guest sent a payment screenshot" in "".join(m["content"] for m in msgs if isinstance(m["content"], str))


def test_photo_delete_path_traversal_blocked(sent):
    victim = config.DATA_DIR / "victim.txt"
    victim.write_text("x")
    with TestClient(app) as client:
        client.post("/admin/login", data={"password": config.ADMIN_PASSWORD})
        r = client.post(f"/admin/rooms/{family_id()}/photos/delete", data={"photo": "../victim.txt"})
        assert r.status_code == 404
    assert victim.exists()


def test_production_config_check(monkeypatch):
    from app import main
    monkeypatch.setattr(config, "BASE_URL", "https://hotel.example.com")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    with pytest.raises(RuntimeError):
        main._check_production_config()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "a-very-strong-pass")
    monkeypatch.setattr(config, "SESSION_SECRET", "x" * 40)
    main._check_production_config()
    monkeypatch.setattr(config, "FB_APP_SECRET", "")
    assert messenger.verify_signature(b"{}", "") is False


# ---------------- Telegram guest channel ----------------
def tg_update(uid, chat_id=555, chat_type="private", **message):
    return {"update_id": uid, "message": {"chat": {"id": chat_id, "type": chat_type},
                                          "from": {"first_name": "Sok", "last_name": "Dara"}, **message}}


def test_telegram_guest_flow(sent, monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, "_call", lambda method, files=None, **kw: calls.append((method, kw)) or {"ok": True})
    monkeypatch.setattr(telegram, "download_photo", lambda file_id, code: "proof_tg.jpg")
    fake = FakeClaude([Resp([text("Hello! How can I help?")]), Resp([text("Nice photo!")])])
    monkeypatch.setattr(agent, "_client", fake)

    bot.handle_telegram_update(tg_update(1, text="/id"))
    assert calls == [("sendMessage", {"chat_id": "555", "text": "Your Telegram chat ID is 555",
                                      "disable_web_page_preview": True})]
    assert agent.get_guest("tg555") == {} and fake.calls == []      # asking for the ID starts no conversation

    bot.handle_telegram_update(tg_update(2, text="/start"))
    bot.handle_telegram_update(tg_update(2, text="/start"))         # redelivered after a restart
    bot.handle_telegram_update(tg_update(3, chat_id=-100, chat_type="group", text="hi all"))
    assert len(fake.calls) == 1 and agent.get_guest("tg555")["name"] == "Sok Dara"
    assert calls[-1] == ("sendMessage", {"chat_id": "555", "text": "Hello! How can I help?",
                                         "disable_web_page_preview": True})

    bot.handle_telegram_update(tg_update(4, photo=[{"file_id": "small"}, {"file_id": "big"}]))
    assert len(fake.calls) == 2                                     # no deposit waiting: the AI answers the photo

    b = booking.create_booking(room_type_id=family_id(), check_in=d(3), check_out=d(4), num_guests=2,
                               guest_name="Sok Dara", phone="012", payment_method="deposit", psid="tg555")
    bot.handle_telegram_update(tg_update(5, photo=[{"file_id": "small"}, {"file_id": "big"}]))
    assert booking.get_booking(code=b["code"])["payment_proof"] == "proof_tg.jpg"
    assert "received your payment screenshot" in calls[-1][1]["text"] and len(fake.calls) == 2

    assert bot.staff_send("tg555", "Hello from the owner") == ""
    assert calls[-1][1]["text"] == "Hello from the owner" and agent.get_guest("tg555")["bot_paused"] == 1
    assert sent == []                                               # nothing went to Messenger


def test_telegram_sends_own_media_as_upload(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, "_call", lambda method, files=None, **kw: calls.append((files, kw)) or {"ok": True})
    (config.UPLOAD_DIR / "khqr_t.png").write_bytes(b"png")
    assert telegram.send_image("tg1", messenger.public_url("khqr_t.png"))
    assert telegram.send_image("tg1", "https://example.com/a.jpg")
    assert calls[0][0]["photo"][0] == "khqr_t.png" and "photo" not in calls[0][1]
    assert calls[1] == (None, {"chat_id": "1", "photo": "https://example.com/a.jpg"})
    assert telegram.is_telegram("tg-42") and not telegram.is_telegram("TEST-dashboard")
