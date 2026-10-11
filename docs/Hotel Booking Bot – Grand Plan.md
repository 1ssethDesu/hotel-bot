# Hotel Booking Bot – Grand Plan

Oct 11, 2026 · @Chhoraseth CHHORT

## What we're building

One AI receptionist answers guests on **Facebook Messenger** and **Telegram**, takes bookings on its own, and keeps everything in **one Google Sheet**. You only step in to confirm KHQR deposits and handle rare unusual requests, from a private Telegram staff bot.

The five parts:

| Part | What it is | Why |
| --- | --- | --- |
| Guest channels | Your Facebook Page (Messenger) + a public Telegram bot, e.g. @MyHotelBot | Where your guests already book today |
| Server | One small program running 24/7 on Railway or Render | Receives every message, runs the bot |
| AI receptionist | Claude, with your hotel info and booking tools | Understands Khmer and English, books rooms |
| Google Sheet | Tabs for rooms, bookings, closed dates, prices, hotel info, chats | Your only data store; you can open and edit it any time |
| Staff bot | A second, private Telegram bot only you (and staff) can use | Booking alerts, deposit buttons, replying to guests |

Design rules that keep it simple now and easy to grow later:

- **One brain, many channels.** Messenger and Telegram are thin "adapters". The booking logic is written once, so adding WhatsApp or Instagram later means adding one adapter.
- **The Sheet is the source of truth.** You edit rooms, prices and hotel info directly in the Sheet; the bot reads it within a minute.
- **One storage module.** All Sheet reads and writes go through one file, so moving to a real database later changes one file, not the whole system.

&#91;embedded content: system parts · 2 guest channels, 1 server, 1 Sheet\]

Guests on either app reach the same server; it asks the AI what to say and reads or writes the Sheet, and tells you only what needs you.

## How it works

Every guest message, from either channel, follows the same path; only the deposit check and rare handovers need you.

&#91;embedded content: booking flow · 1 decision, 2 payment paths\]

Pay-on-arrival bookings are confirmed instantly; deposit bookings wait for your tap, and expire on their own if no screenshot arrives.

**A. Guest books a room (same on Messenger and Telegram)**

1. Guest writes, e.g. "Room for 2 this Saturday, 2 nights?" or in Khmer.
2. The server receives it, finds or creates the guest in the **Guests** tab, and saves the message in **Chats**.
3. The AI reads the hotel info and room list from the Sheet, turns "this Saturday" into exact dates and answers in the guest's language.
4. The AI calls *check availability*: the server counts rooms in **Rooms** minus overlapping rows in **Bookings** and **Closed**, and applies **Prices**.
5. The AI offers only free rooms that fit the group, with the exact total.
6. It collects full name, phone (Telegram guests can tap "Share my phone") and payment choice.
7. It shows a summary and asks "Confirm?". Only after a clear yes, it calls *create booking*.
8. The server re-checks availability (so two guests can't take the last room), writes a new row in **Bookings** with a code like BK7Q2MX, and replies with the code.

**B. Payment**

- **Pay on arrival:** status = Confirmed. The staff bot sends you "New booking". Nothing else to do.
- **KHQR deposit:**
  1. Status = Waiting for deposit, held for 12 hours. The bot sends your KHQR image and the amount.
  2. Guest pays and sends the screenshot in the chat.
  3. The staff bot shows you the screenshot with **Deposit received** / **Not received** buttons.
  4. You check your bank app and tap. The Sheet row and the guest are updated automatically.
  5. No screenshot within 12 hours: status = Expired, room released, guest told.

**C. Handover (rare)**

1. Discount requests, complaints, questions not answered by the hotel info, or "I want a person".
2. The bot tells the guest a team member will reply, and pauses for that guest.
3. The staff bot alerts you. You **reply to that alert** in Telegram; your text goes to the guest on their channel.
4. The bot resumes after 12 hours of silence from you, or when you tap Resume.

**D. Your daily part**

- Walk-in, phone or Booking.com booking: add a row in **Bookings** (or `/walkin` in the staff bot), so the bot never sells that room.
- Repairs or private use: add a row in **Closed**.
- Holiday price: add a row in **Prices**.
- Morning check: `/today` in the staff bot (arrivals, departures, deposits waiting, rooms free tonight).

## The Google Sheet

One spreadsheet with eight tabs. You fill the first five; the bot writes the last three (you can also add walk-ins to Bookings). Keep the header row (row 1) exactly as named below.

| Tab | Who edits | Columns (row 1) |
| --- | --- | --- |
| Rooms | You | key, name\_en, name\_km, description\_en, description\_km, max\_guests, quantity, price\_per\_night, photo\_urls, active |
| Hotel Info | You | topic, answer (e.g. Wi-Fi / Free in all rooms; Breakfast / Not included) |
| Settings | You | setting, value: hotel\_name, phone, address, map\_link, check\_in\_time, check\_out\_time, currency, allow\_pay\_on\_arrival, allow\_deposit, deposit\_percent, hold\_hours, khqr\_image\_url, bot\_on |
| Closed | You | room\_key, from\_night, last\_night, rooms, reason |
| Prices | You | room\_key, from\_night, last\_night, price, note |
| Bookings | Bot + you | code, created\_at, channel, guest\_id, guest\_name, phone, room\_key, check\_in, check\_out, nights, guests, total, payment, deposit, status, hold\_until, notes |
| Guests | Bot | guest\_id, channel, name, phone, language, first\_seen, last\_message\_at, bot\_paused |
| Chats | Bot | time, guest\_id, channel, from (guest / bot / staff), message |

Rules that make it work:

- **One row = one booking.** `status` is one of: Waiting for deposit, Confirmed, Checked in, Checked out, Cancelled, Expired. Make it a dropdown in the Sheet.
- **Dates as YYYY-MM-DD** (Format → Number → Plain text, or a date format). Check-out is the day the guest leaves, not a night.
- **Walk-ins:** type a row with `channel = walk-in` and `status = Confirmed`. The bot counts it right away.
- **Availability is calculated, not typed.** Free rooms = quantity − active bookings − closed rooms for each night. No calendar tab to keep in sync.
- **Photos and KHQR** are links (e.g. images in a public Google Drive folder), pasted into `photo_urls` and `khqr_image_url`.
- The bot re-reads Rooms, Hotel Info, Settings, Closed and Prices **every 60 seconds**, so your edits apply within a minute.

## Build & setup plan

Seven phases, in this order. Telegram goes live before Messenger because it needs no Meta setup, so you can test the whole flow early. Tick each box as you go.

**Phase 0 – Accounts and keys (you, \~1 hour)**

- [x] Google account for the hotel (owns the Sheet)
- [x] Claude API key at console.anthropic.com, with a monthly spending limit
- [ ] Telegram: in @BotFather create **two** bots: the guest bot (e.g. @MyHotelBot) and the staff bot (e.g. @MyHotelStaffBot). Save both tokens — *guest bot done (@neangpoun_bot); staff bot still to create*
- [ ] Meta developer account at developers.facebook.com (with the Facebook account that is admin of your Page)
- [x] Railway (or Render) account and a free GitHub account

**Phase 1 – Google Sheet + access (you, \~1 hour; I send the template)**

- [ ] Make a copy of the template Sheet and fill **Rooms**, **Hotel Info** and **Settings**
- [ ] Put your KHQR image and room photos in a Google Drive folder shared "Anyone with the link"; paste the links into the Sheet
- [ ] In Google Cloud Console: create a project → enable **Google Sheets API** → create a **service account** → download its JSON key
- [ ] Share the Sheet with the service account's email as **Editor** (this is how the bot gets access)
- [ ] Copy the Sheet ID (the long code in its link)

**Phase 2 – Build the code (me)**

- [ ] Reuse the tested booking brain from the MVP; replace SQLite with a Google Sheet storage module
- [ ] Telegram guest adapter (text, photos, "Share my phone" button) and Messenger adapter — *Telegram text and photos working on the MVP code (tested in local Docker); "Share my phone" still to do*
- [ ] Staff bot: alerts, deposit buttons, reply-to-guest, `/today`, `/walkin`, `/test`, `/boton` `/botoff`
- [ ] Automated tests for every flow in the testing checklist, run against a fake Sheet

**Phase 3 – Put the server online (you, with my guide, \~30 min)**

- [ ] Upload the code to a private GitHub repo and create a Railway project from it — *repo pushed and Railway project created; first build failed, Dockerfile fix not pushed yet*
- [ ] Paste the settings: Claude key, both Telegram tokens, Sheet ID, service-account JSON, your staff chat ID
- [ ] Note the public address, e.g. https://myhotel-bot.up.railway.app

**Phase 4 – Go live on Telegram (you, \~30 min)**

- [ ] Send `/start` to the staff bot; it shows your chat ID; add it to the settings and restart
- [ ] Run the testing checklist below on Telegram, using your own and a friend's phone
- [ ] Put the guest bot link (t.me/MyHotelBot) on your Facebook Page, Google Maps and booking confirmations

**Phase 5 – Go live on Messenger (you, \~1 hour)**

- [ ] Create the Meta app (Messenger use case), connect your Page, copy the Page token and App secret into the settings
- [ ] Set the webhook to `https://YOUR-SERVER/webhook`; subscribe to messages, messaging\_postbacks, message\_echoes
- [ ] Turn off "Instant reply" in Meta Business Suite → Inbox → Automations
- [ ] Switch the app to **Live**; ask a friend with no role on the app to message the Page. Bot replies = done. No reply = request App Review

**Phase 6 – Soft launch (you, 1 week)**

- [ ] Watch every booking and chat in the staff bot and the Sheet for 7 days
- [ ] Each time the bot hands over or answers poorly, add the missing answer to **Hotel Info**
- [ ] After a clean week, tell regular guests and partners to book through the bots

## Testing checklist

Go live on a channel only when every line passes on that channel. Each test lists what you do and what must happen.

| # | Test | You do | Must happen |
| --- | --- | --- | --- |
| 1 | Question in English | Ask "Do you have Wi-Fi?" | Answer from Hotel Info, no booking started |
| 2 | Question in Khmer | Ask the same in Khmer | Reply in Khmer |
| 3 | Unknown question | Ask something not in Hotel Info | Bot says it will check, you get a handover alert |
| 4 | Pay on arrival | Book 2 nights, choose pay on arrival, confirm | New row in Bookings = Confirmed, code sent, staff alert |
| 5 | No confirm, no booking | Get the summary, then say "wait" | No row in Bookings |
| 6 | Deposit, received | Book with deposit, send a screenshot, tap Deposit received | QR sent; status Waiting → Confirmed; guest told |
| 7 | Deposit, rejected | Same, tap Not received | Guest asked to resend; hold restarts |
| 8 | Deposit, expired | Book with deposit, send nothing (set hold\_hours to 0.1 for the test) | Status Expired, room free again, guest told |
| 9 | Full hotel | Close all rooms of a type in Closed, then ask for it | Bot says it's not available and offers another type |
| 10 | Last room race | Two phones book the last room at the same time | Only one booking; the other is told it's gone |
| 11 | Walk-in row | Type a walk-in row in Bookings, then ask for that night | Room counted as taken |
| 12 | Holiday price | Add a row in Prices, ask for those nights | Total uses the new price |
| 13 | Too many guests | Ask for 5 people in a 2-person room | Bot offers a bigger room or two rooms |
| 14 | Own booking | Ask "show my booking", then "cancel it" | Correct booking shown; status Cancelled; staff alert |
| 15 | Handover + reply | Ask for a discount; reply to the alert in the staff bot | Your text reaches the guest; bot stays quiet; Resume works |
| 16 | Stranger in staff bot | Message the staff bot from another phone | Ignored (only shows its chat ID) |
| 17 | Bot off | `/botoff`, then message as a guest | No reply; `/boton` restores it |
| 18 | Sheet edit | Change a room price in Rooms | Bot quotes the new price within a minute |
| 19 | Same flow on Messenger | Repeat 1, 4, 6 and 15 on Messenger | Same results; your manual reply in Meta Business Suite pauses the bot |

## Costs, limits and rules

Running costs are small: the AI is the only cost that grows with bookings.

| Item | Cost | Note |
| --- | --- | --- |
| Claude AI (claude-sonnet-5-5) | About a few US cents per booking conversation | $2 / $10 per million input / output tokens; claude-haiku-5-5 is cheaper. Set a monthly limit |
| Server (Railway or Render) | Small monthly fee | No disk needed: data lives in the Sheet |
| Google Sheet + Sheets API | Free | Within Google's per-minute request limits |
| Telegram bots, Messenger | Free |  |

Rules to respect:

- **Messenger 24-hour rule:** the bot can only message a Messenger guest within 24 hours of their last message. If you confirm a deposit later, the staff bot tells you to call the guest. Telegram has no such limit once the guest has started the bot.
- **Google Sheets speed:** fine for a small hotel (dozens of bookings a day). The bot caches the Sheet for 60 seconds and writes one row per booking; a single server makes sure two bookings never take the same last room.
- **Don't rename tabs or header columns,** and don't sort the Bookings tab by hand in a way that splits rows. Adding your own extra columns at the right is fine.
- **Privacy:** guests' names and phones sit in the Sheet; share it only with staff who need it. Keep the service-account key and tokens secret.
- **Bookings from other places** must be added to the Sheet, or the bot can sell the same room twice.

Prices from [Claude models overview](https://platform.claude.com/docs/en/about-claude/models/overview).

## Later: how it scales up

Each upgrade replaces one part and leaves the rest untouched, in roughly this order.

| When you notice | Upgrade | What changes |
| --- | --- | --- |
| More than \~50 bookings a day, or the Sheet feels slow | Move storage to a real database (SQLite or Postgres); keep the Sheet as a read-only report | Only the storage module |
| Editing the Sheet on a phone is painful | Owner web dashboard (already built in the earlier version) | Adds pages; bot unchanged |
| Checking screenshots takes too long | Automatic payment check through ABA PayWay or Bakong | Deposit step only |
| Guests on other apps | WhatsApp or Instagram adapter | One new adapter |
| You list on Booking.com / Agoda | Channel manager sync (iCal or API) instead of typing walk-ins | Adds a sync job |
| Guests ask for reminders | Check-in reminder the day before (Telegram freely; Messenger needs an approved template) | Adds a daily job |
