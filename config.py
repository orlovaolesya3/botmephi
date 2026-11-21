import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "changeme")

DB_PATH = os.getenv("DB_PATH", "bot.db")

MAX_PHOTOS = 3
MAX_TAGS = 10
BIO_MAX = 1500
SKIP_COOLDOWN_DAYS = 7