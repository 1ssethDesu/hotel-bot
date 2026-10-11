# Hotel Messenger Booking Bot

An AI receptionist for your Facebook Page. It answers guests in **Khmer or English**, checks real room
availability, takes bookings (**pay on arrival** or **deposit by KHQR**), and gives you a private
**web dashboard** to manage everything, so you no longer need to reply to every message yourself.

```
Guest on Messenger ──► Facebook ──► this server (/webhook)
                                     │
                                     ├─ AI receptionist (Claude) ── tools ──► booking database (SQLite)
                                     │      check availability · create/cancel booking · send photos/QR · call staff
                                     │
                                     └─ Owner dashboard (/admin) ◄── you: bookings, calendar, prices, chats
```

## What it does

**For guests (on Messenger, 24/7)**
- Answers questions from the hotel info you write (Wi-Fi, breakfast, location, policies, and so on). It never makes up answers.
- Understands phrases like "2 nights from Friday" and "this weekend", checks which rooms are free, and quotes the exact total, including holiday prices.
- Collects name, phone and payment choice, shows a summary, and only books after the guest says yes. Then it sends a booking code.
- Deposit option: it sends your KHQR code automatically and holds the room for X hours. The guest sends a screenshot, you verify, and the guest gets a confirmation.
- Sends room photos when asked.
- Lets guests check or cancel their own booking.
- Hands over to you (and pauses itself) for discounts, complaints, unusual requests, questions it can't answer, or when the guest asks for a person.

**For you (dashboard, works on phone)**
- **Overview:** guests waiting for you, deposits to verify, arrivals and departures today, rooms free for the next 7 nights.
- **Bookings:** search, check-in, check-out, no-show, cancel (the guest is told on Messenger), confirm a deposit with one tap.
- **Add booking:** for walk-ins, phone calls and Booking.com/Agoda bookings, so the bot never double-books.
- **Calendar:** 14-day availability, and closing rooms for maintenance.
- **Rooms & prices:** room types, how many of each, price, Khmer and English names, photos, special holiday prices.
- **Hotel info:** everything the bot is allowed to say.
- **Chats:** read every conversation, reply as staff, pause or resume the bot per guest.
- **Test bot:** chat with your bot before going live.
- **Settings:** payment options, deposit %, hold time, KHQR image, bot on/off.

**Safety built in:** no double booking (database locking), unpaid holds expire automatically, payment
screenshots are private (owner only), Facebook signature check, and Meta's 24-hour messaging rule is respected.
When you reply by hand in the Page inbox, the bot notices and stays quiet. It resumes after a set number of hours of silence from you (you choose how many).

---

## Setup (about 1–2 hours, plus Meta's review time)

### 1. Get your keys ready
| What | Where | Goes into `.env` as |
|---|---|---|
| AI key | console.anthropic.com → API Keys (add a payment method) | `ANTHROPIC_API_KEY` |
| Facebook Page | You already have one (it must be a Page, not a personal profile) | – |
| Meta developer account | developers.facebook.com (log in with the Facebook account that is admin of the Page) | – |
| Telegram alerts (optional) | Talk to @BotFather → `/newbot` → token. Send your bot a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat id | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

### 2. Put the server online (needs an HTTPS address)
Facebook must be able to reach the server over HTTPS, 24/7. Pick one option:

**Option A: Railway or Render (easiest, no server admin).**
1. Put this folder in a private GitHub repository.
2. Create a new project from the repo. The provider detects the `Dockerfile`.
3. Add a **persistent volume/disk mounted at `/data`**. This is important: it stores the database and photos.
4. Add the variables from `.env.example` in the provider's "Variables" screen.
5. Copy the public URL the provider gives you (e.g. `https://hotel-bot.up.railway.app`) into `BASE_URL`.

**Option B: a small VPS (DigitalOcean, Vultr, a Cambodian hosting company, and so on).**
```bash
git clone <your repo> hotel-bot && cd hotel-bot
cp .env.example .env && nano .env          # fill in values
docker build -t hotel-bot .
docker run -d --restart=always --env-file .env -p 8000:8000 -v $PWD/data:/data --name hotel-bot hotel-bot
```
Then put HTTPS in front with Caddy (`your-domain.com { reverse_proxy localhost:8000 }`) or Cloudflare.

**Run locally for a first test:**
```bash
pip install -r requirements.txt
cp .env.example .env      # set ADMIN_PASSWORD and ANTHROPIC_API_KEY at least
uvicorn app.main:app --reload
```
Open http://localhost:8000/admin. To let Facebook reach your laptop during testing, use `ngrok http 8000` and use the ngrok URL as `BASE_URL`.

### 3. Fill in your hotel (dashboard)
1. **Rooms & prices:** edit the 3 sample room types (or add yours). Set the *number of rooms* of each type, the price and the Khmer names, and upload 2–3 photos each.
2. **Hotel info:** address, map link, check-in/out times, and everything guests usually ask.
3. **Settings:** upload your **KHQR** image (from your ABA, ACLEDA or other bank app's "Receive" QR), and set deposit % (default 30%) and hold time (default 12 h).
4. **Test bot:** try conversations in English and Khmer, e.g. "Do you have a room for 2 people this Saturday?" and "តើមានបន្ទប់សម្រាប់ ៤នាក់ ថ្ងៃស្អែកទេ?" Adjust the hotel info until the answers are right. Then press **Reset test**.
5. Add any existing future bookings with **+ Add booking**.

### 4. Connect your Facebook Page (developers.facebook.com)
1. **My Apps → Create app.** Choose the use case to **engage with customers on Messenger** (business type) and link it to your business portfolio.
2. **Messenger → Messenger API settings:**
   - *Generate access tokens* → connect your hotel Page → copy the **Page access token** → `FB_PAGE_ACCESS_TOKEN`.
   - *Configure webhooks* → Callback URL: `https://YOUR-BASE-URL/webhook`, Verify token: the same text as `FB_VERIFY_TOKEN` → **Verify and save**.
   - Subscribe the Page to these webhook fields: **messages, messaging_postbacks, message_echoes**.
3. **App settings → Basic:** copy the **App secret** → `FB_APP_SECRET`. Set the **Privacy Policy URL** to `https://YOUR-BASE-URL/privacy` (this page is included).
4. Restart the server so it picks up the new values, then run once:
   ```bash
   python -m scripts.setup_messenger_profile     # adds Get Started button, greeting and menu
   ```
5. In **Meta Business Suite → Inbox → Automations**, turn **off** "Instant reply" and other automated replies. The bot does that job now, and those replies would make the bot think you took over the chat.
6. **Test:** while the app is in *Development* mode, only people with a role on the app (you, and testers you add under *App roles*) get bot replies. Message your Page from your own account.

### 5. Go live (Meta App Review)
To let **all** guests talk to the bot, request **Advanced Access** for `pages_messaging` (and `pages_manage_metadata` if the dashboard asks) in **App Review → Permissions and features**. Meta usually asks for:
- a short screen recording showing a guest asking a question and making a booking on Messenger,
- a description, for example: *"Our hotel's automated receptionist answers guest questions and takes room bookings in Messenger. Guests can ask for a human at any time."*
- the privacy policy URL, and sometimes **Business Verification** (business documents).

Review usually takes a few days. When it's approved, switch the app to **Live**.
*Optional:* request the **Human Agent** permission. Without it, your own replies from the dashboard can only be sent within 24 hours of the guest's last message. With it, the window is 7 days.

---

## Meta's 24-hour rule (important)
Meta lets a Page message a guest freely for **24 hours after the guest's last message**. The bot always answers
inside that window. In 2026 Meta retired the old "booking update" message tags, so if you confirm a deposit or
cancel a booking more than 24 hours after the guest last wrote, the system **cannot** message them. Instead it
sends you an alert with the guest's phone number so you can call. In practice this is rare: guests usually send the payment screenshot right away. Your own replies from the dashboard work for up to 7 days if your app has the
**Human Agent** permission.

## Daily use
- Turn on Telegram alerts (or keep the dashboard open on your phone). You'll be pinged for each **new booking**, **payment screenshot**, **expired hold** and **guest who needs you**.
- **Deposit flow:** the guest pays and sends a screenshot. You get an alert, open the booking, check your bank app, and tap **Deposit received – confirm**. The guest receives the confirmation automatically.
  If no screenshot arrives within the hold time, the room is released and the guest is told.
- **Taking over a chat:** reply in the dashboard (Chats) or in Meta Business Suite. The bot pauses for that guest and resumes automatically after the hours you set in Settings, or when you tap **Resume bot**.
- **Bookings from other channels** (walk-in, phone, OTAs): always add them with **+ Add booking** so availability stays correct.
- Switch the bot off entirely in **Settings** if needed. Messages then wait for you in Chats.

## Costs (approximate)
- **AI:** you pay Anthropic per message. With `claude-sonnet-5-5` a full booking conversation usually costs a few US cents. Set `AI_MODEL=claude-haiku-5-5` for a much cheaper (slightly less capable) option. You can set a monthly spending limit in console.anthropic.com.
- **Hosting:** a small server or Railway/Render instance plus a 1 GB disk is enough for a hotel of this size.
- **Messenger:** free.

## Backups
Everything is in the `data/` folder (`hotel.db` plus `uploads/`). Copy it regularly. On Railway/Render, use volume snapshots.

## Project layout
```
app/
  main.py        webhook, public media, privacy page, background job (expires unpaid holds every 5 min)
  bot.py         handles Messenger events: messages, menu, payment screenshots, staff echo, notifications
  agent.py       AI receptionist: system prompt (built from your dashboard data) + booking tools
  booking.py     availability, prices, holds, booking lifecycle (no double booking)
  messenger.py   Facebook Send API, signature check, 24h rule
  dashboard.py   owner dashboard routes;  templates/ + static/ for the pages
  db.py          SQLite schema and settings
scripts/setup_messenger_profile.py   Get Started button + menu
tests/           automated tests (run: pytest)
```

## Ideas for later
- Day-before check-in reminder (needs an approved Meta message template / marketing messages).
- Automatic payment verification through a PayWay/Bakong API instead of screenshots.
- Sync with Booking.com/Agoda via a channel manager's iCal or API.
- Instagram DMs and WhatsApp use the same AI and booking engine; only the messaging layer changes.
