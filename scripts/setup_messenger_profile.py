"""Run once after the Page token is set:  python -m scripts.setup_messenger_profile

Adds the "Get Started" button, the greeting text, and the menu
(Book a room / My booking / Talk to staff) to your Page's Messenger.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, messenger  # noqa: E402

if not config.FB_PAGE_ACCESS_TOKEN:
    sys.exit("FB_PAGE_ACCESS_TOKEN is missing in .env")

db.init_db()
name = db.get_setting("hotel_name")
result = messenger.set_messenger_profile(
    greeting_en=f"Welcome to {name}! Ask us anything or book a room right here, 24/7.",
    greeting_km=f"សូមស្វាគមន៍មកកាន់ {name}! សួរព័ត៌មាន ឬកក់បន្ទប់បាននៅទីនេះ ២៤ម៉ោង។",
)
print(result)
