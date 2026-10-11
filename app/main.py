"""FastAPI app: Messenger webhook, Telegram polling, public media, privacy page and the owner dashboard."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import bot, config, db, messenger, telegram
from .dashboard import router as dashboard_router, templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines print full URLs, which hold the bot token
log = logging.getLogger("main")


async def _expiry_loop():
    while True:
        try:
            n = await asyncio.to_thread(bot.run_expiry_job)
            if n:
                log.info("Expired %s deposit hold(s)", n)
        except Exception:
            log.exception("Expiry job failed")
        await asyncio.sleep(300)


def _check_production_config() -> None:
    if not config.BASE_URL.startswith("https://"):
        return  # local testing
    problems = []
    if config.ADMIN_PASSWORD in ("", "admin", "choose-a-strong-password") or len(config.ADMIN_PASSWORD) < 8:
        problems.append("ADMIN_PASSWORD must be set to a strong password (8+ characters)")
    if config.SESSION_SECRET in ("", "please-change-this-secret", "replace-with-a-long-random-string") \
            or len(config.SESSION_SECRET) < 24:
        problems.append("SESSION_SECRET must be a long random string (24+ characters)")
    if problems:
        raise RuntimeError("Unsafe configuration: " + "; ".join(problems))
    if not config.FB_APP_SECRET:
        log.warning("FB_APP_SECRET is empty: Messenger webhooks will be rejected until you set it.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_production_config()
    db.init_db()
    tasks = [asyncio.create_task(_expiry_loop())]
    if config.TELEGRAM_BOT_TOKEN:
        tasks.append(asyncio.create_task(telegram.poll_loop(bot.handle_telegram_update)))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Hotel Messenger Bot", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, max_age=60 * 60 * 24 * 14,
                   https_only=config.BASE_URL.startswith("https"))
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "app" / "static"), name="static")
app.include_router(dashboard_router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/admin")


@app.get("/health")
def health():
    return {"ok": True}


# ---------- Messenger webhook ----------
@app.get("/webhook")
def verify_webhook(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == config.FB_VERIFY_TOKEN:
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not messenger.verify_signature(raw, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=403, detail="Bad signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad JSON")
    # Reply 200 fast; Meta retries if we are slow. Work happens in the background.
    background.add_task(bot.handle_payload, payload)
    return PlainTextResponse("EVENT_RECEIVED")


# ---------- public media (room photos, KHQR) ----------
@app.get("/media/{filename}")
def media(filename: str):
    if "/" in filename or ".." in filename or filename.startswith("proof_"):
        raise HTTPException(status_code=404)
    path = config.UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {"s": db.get_settings()})
