"""Configuration for the Nova Home Dashboard backend.

Secrets are read from environment variables (see `.env.example`). When the
Realty Calendar token is not configured the backend automatically switches to
DEMO_MODE and serves generated data so the whole app is runnable out of the box.
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Load .env (optional, no hard dependency on python-dotenv)
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Realty Calendar
# ---------------------------------------------------------------------------
RC_BASE_URL = os.environ.get("RC_BASE_URL", "https://realtycalendar.ru")
RC_TOKEN = os.environ.get("RC_TOKEN", "").strip()

# Cookies are read from a JSON file (RC_COOKIES_FILE) with a `cookies` field,
# or directly from the RC_COOKIES env var as "key=value; key2=value2".
RC_COOKIES_FILE = os.environ.get("RC_COOKIES_FILE", "").strip()


def _load_cookies() -> dict:
    raw = os.environ.get("RC_COOKIES", "").strip()
    if raw:
        cookies = {}
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                cookies[k.strip()] = v.strip()
        return cookies
    if RC_COOKIES_FILE and Path(RC_COOKIES_FILE).exists():
        try:
            data = json.loads(Path(RC_COOKIES_FILE).read_text(encoding="utf-8"))
            cookies = data.get("cookies", {})
            if isinstance(cookies, str):
                parsed = {}
                for part in cookies.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, _, v = part.partition("=")
                        parsed[k.strip()] = v.strip()
                return parsed
            return cookies or {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


RC_COOKIES = _load_cookies()

# DEMO_MODE serves generated data when no real credentials are available.
DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes") or not RC_TOKEN

# ---------------------------------------------------------------------------
# Apartments (17 units of Nova Home, Tashkent)
# ---------------------------------------------------------------------------
APARTMENTS = {
    359690: "A-066",
    359691: "A-067",
    359692: "A-120",
    359694: "A-207",
    359696: "A-210",
    364084: "A-278",
    359697: "B-005",
    359700: "B-007",
    359698: "B-008",
    359701: "B-031",
    359703: "B-051",
    359702: "B-071",
    365510: "B-073",
    359704: "B-078",
    359706: "B-103",
    359707: "B-110",
    359710: "B-135",
}
TOTAL_APARTMENTS = len(APARTMENTS)

# Booking source labels
SOURCE_NAMES = {None: "Прямое", 0: "Прямое", 2: "Booking.com", 3: "Airbnb"}

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()


def _parse_ids(raw: str) -> list[int]:
    ids = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


OWNER_TELEGRAM_IDS = _parse_ids(os.environ.get("OWNER_IDS", ""))

# Personal owner access key (see auth.py): unlocks owner mode on Telegram
# clients that pass no initData to Mini Apps (e.g. native macOS Telegram).
OWNER_KEY = os.environ.get("OWNER_KEY", "").strip()


def _parse_chat_ids(raw: str) -> list[int]:
    """Chat ids may be negative (groups), so accept a leading minus."""
    ids = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        try:
            ids.append(int(part))
        except ValueError:
            pass
    return ids


# Where notifications go: owner private chats + any extra chats (e.g. a team group).
NOTIFY_CHAT_IDS = _parse_chat_ids(os.environ.get("NOTIFY_CHAT_IDS", ""))

# Payments channel(s) the bot reads as admin. Empty = accept posts from any
# channel the bot was added to (their chat ids are logged on arrival).
PAY_CHANNEL_IDS = _parse_chat_ids(os.environ.get("PAY_CHANNEL_IDS", ""))

# Extra people who may see ONLY the "Оплаты" reconciliation block (not the
# rest of Касса). Entries are Telegram ids or @usernames (resolved through the
# bot's staff registry, so the person must have talked to the bot once).
PAY_VIEWERS = [t for t in os.environ.get("PAY_VIEWERS", "").replace(",", " ").split() if t]

# Access key the bot appends to the dashboard URL for PAY_VIEWERS — unlocks
# the Оплаты block on clients that pass no initData (macOS Telegram).
PAY_KEY = os.environ.get("PAY_KEY", "").strip()


def notify_targets() -> list[int]:
    seen: list[int] = []
    for i in list(OWNER_TELEGRAM_IDS) + NOTIFY_CHAT_IDS:
        if i not in seen:
            seen.append(i)
    return seen

# ---------------------------------------------------------------------------
# Google Sheet bridge (cash balances via an Apps Script web app)
# ---------------------------------------------------------------------------
SHEET_API_URL = os.environ.get("SHEET_API_URL", "").strip()
SHEET_API_TOKEN = os.environ.get("SHEET_API_TOKEN", "").strip()
FINANCE_ENABLED = bool(SHEET_API_URL and SHEET_API_TOKEN)
# how long to cache balances in memory before re-querying the sheet (seconds)
FINANCE_CACHE_TTL = int(os.environ.get("FINANCE_CACHE_TTL", "60"))

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "15"))


# ---------------------------------------------------------------------------
# Staff attendance geofence (Tashkent City by default; adjust in .env)
# ---------------------------------------------------------------------------
def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


WORK_LAT = _float_env("WORK_LAT", 41.31255)      # Tashkent City
WORK_LNG = _float_env("WORK_LNG", 69.27920)
WORK_RADIUS_M = int(os.environ.get("WORK_RADIUS_M", "700") or 700)
SHIFT_START = os.environ.get("SHIFT_START", "09:00").strip() or "09:00"
SHIFT_GRACE_MIN = int(os.environ.get("SHIFT_GRACE_MIN", "0") or 0)


def _parse_hhmm(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in raw.strip().split(":")[:2])
        return h, m
    except (ValueError, AttributeError):
        return default


# Attendance workflow: remind staff at ATTEND_REMIND, accept live-location
# check-ins from ATTEND_EARLIEST until ATTEND_DEADLINE, then post the roll call.
ATTEND_REMIND_T = _parse_hhmm(os.environ.get("ATTEND_REMIND", "10:00"), (10, 0))
ATTEND_EARLIEST_T = _parse_hhmm(os.environ.get("ATTEND_EARLIEST", "09:00"), (9, 0))
ATTEND_DEADLINE_T = _parse_hhmm(os.environ.get("ATTEND_DEADLINE", "12:30"), (12, 30))

# End-of-day cleaning control: post to the group which checkouts still have
# no cleaning report.
CLEANING_CHECK_T = _parse_hhmm(os.environ.get("CLEANING_CHECK", "18:00"), (18, 0))

# Quiet hours: bot messages (group + DMs) are delivered silently in this window.
QUIET_FROM_T = _parse_hhmm(os.environ.get("QUIET_FROM", "01:00"), (1, 0))
QUIET_TO_T = _parse_hhmm(os.environ.get("QUIET_TO", "08:00"), (8, 0))


def quiet_now() -> bool:
    from datetime import datetime
    now = datetime.now()
    hm = (now.hour, now.minute)
    f, t = QUIET_FROM_T, QUIET_TO_T
    if f == t:
        return False
    if f < t:
        return f <= hm < t
    return hm >= f or hm < t  # window wraps over midnight


def _parse_staff(raw: str) -> dict[int, str]:
    """STAFF_IDS="123456:Аня, 789012:Бек" -> {123456: "Аня", 789012: "Бек"}."""
    staff: dict[int, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        sid, _, name = part.partition(":")
        sid = sid.strip()
        if sid.isdigit():
            staff[int(sid)] = name.strip() or sid
    return staff


STAFF = _parse_staff(os.environ.get("STAFF_IDS", ""))

# Auto-fine for a no-show at the roll call (in сум). 0 = disabled.
try:
    AUTO_FINE_NOSHOW = float(os.environ.get("AUTO_FINE_NOSHOW", "0") or 0)
except ValueError:
    AUTO_FINE_NOSHOW = 0.0
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "nova_dashboard.db"))
API_PREFIX = "/api"
