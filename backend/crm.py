"""Nova Home CRM — the office web app (/crm/).

Users log in with email + password (roles: admin / manager, blockable); the
session is a signed cookie, so nothing is stored server-side and blocking a
user (bumping `tver`) kills every session at once.

Entities: clients (with custom card fields), deals in pipelines with stages,
tasks with an assignee, quick commands (/wifi …) for the chats, and the
bookings mirrored from RealtyCalendar (day view + chessboard). Chats live in
backend/inbox.py; a CRM session is accepted there too.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

from . import config, database, util

logger = logging.getLogger("nova.crm")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    pass_hash TEXT NOT NULL,
    role TEXT DEFAULT 'manager',     -- admin | manager
    active INTEGER DEFAULT 1,
    tver INTEGER DEFAULT 1,          -- token version: bump = log out everywhere
    created_at TEXT,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS crm_pipelines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS crm_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    sort INTEGER DEFAULT 0,
    color TEXT,
    kind TEXT DEFAULT 'open'         -- open | won | lost
);

CREATE TABLE IF NOT EXISTS crm_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    phone TEXT,                      -- digits only
    email TEXT,
    source TEXT,
    notes TEXT,
    fields TEXT,                     -- JSON {field_id: value}
    chat_id INTEGER,                 -- inbox chat
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    client_id INTEGER,
    pipeline_id INTEGER NOT NULL,
    stage_id INTEGER NOT NULL,
    amount REAL,
    apartment TEXT,
    checkin TEXT,
    checkout TEXT,
    guests INTEGER,
    booking_id INTEGER,
    owner_uid INTEGER,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    due TEXT,                        -- ISO datetime (local)
    assignee_uid INTEGER,
    client_id INTEGER,
    deal_id INTEGER,
    status TEXT DEFAULT 'open',      -- open | done
    created_by INTEGER,
    created_at TEXT,
    done_at TEXT,
    src_key TEXT UNIQUE,             -- auto tasks (checkout:<booking id>) are created once
    booking_id INTEGER               -- task belongs to a booking (card «Задачи по брони»)
);

-- bookings that already got their checklist (so a deleted item does not come back)
CREATE TABLE IF NOT EXISTS crm_booking_checklist (
    booking_id INTEGER PRIMARY KEY,
    created_by INTEGER,
    created_at TEXT,
    n INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS crm_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'text',        -- text | number | date | select | checkbox
    options TEXT,                    -- select: one per line
    sort INTEGER DEFAULT 0
);

-- amo-style «digital pipeline»: what happens when a deal enters a stage
CREATE TABLE IF NOT EXISTS crm_stage_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- message | task | notify
    text TEXT,                       -- message/notify text (variables allowed) or task title
    days INTEGER DEFAULT 0,          -- task: due in N days
    at_time TEXT DEFAULT '10:00',    -- task: due time
    sort INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS crm_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_crm_clients_phone ON crm_clients(phone);
CREATE INDEX IF NOT EXISTS idx_crm_deals_stage ON crm_deals(stage_id);
CREATE INDEX IF NOT EXISTS idx_crm_tasks_status ON crm_tasks(status, due);
"""

DEFAULT_STAGES = [
    ("Новый запрос", "#5B8DEF", "open"), ("Подбор", "#8E6CC6", "open"), ("Ожидает оплаты", "#E08A2B", "open"),
    ("Забронировано", "#2A9DA8", "open"), ("Заселён", "#2EAD6B", "won"), ("Отказ", "#D9534F", "lost"),
]
DEFAULT_COMMANDS = [
    ("заезд", "Напоминание о заезде", "{имя}, напоминаем: ваш заезд {заезд время}, апартаменты {объект}. Напишите, если приедете в другое время — подстроимся."),
    ("выезд", "Напоминание о выезде", "{имя}, напоминаем: выезд {выезд время}. Если нужно задержаться — напишите заранее, посмотрим, что можно сделать."),
    ("привет", "Приветствие",
     "Здравствуйте, {имя}! Это Nova Home. Подскажите, пожалуйста, даты заезда и выезда и количество гостей — подберём апартаменты."),
    ("правила", "Правила проживания",
     "Заезд с 15:00, выезд до 12:00. Возвратный депозит $50. Бесплатная отмена — не позднее чем за 3 дня до заезда."),
    ("оплата", "Способы оплаты",
     "Оплатить можно при заселении: наличными, картой, через Payme или Click. Предоплата не требуется."),
    ("", "Спасибо за проживание",
     "{имя}, спасибо, что выбрали Nova Home! Будем благодарны за отзыв — это очень помогает нам. Ждём вас снова!"),
]
COOKIE = "nh_crm"
SESSION_DAYS = 30


_now = util.now


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute("CREATE TABLE IF NOT EXISTS crm_eta (booking_id INTEGER PRIMARY KEY, eta TEXT, note TEXT, set_by TEXT, set_at TEXT)")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(inbox_templates)").fetchall()]
        if cols and "command" not in cols:
            conn.execute("ALTER TABLE inbox_templates ADD COLUMN command TEXT")
        if "entity" not in [r[1] for r in conn.execute("PRAGMA table_info(crm_fields)").fetchall()]:
            conn.execute("ALTER TABLE crm_fields ADD COLUMN entity TEXT DEFAULT 'client'")
        if "tg_id" not in [r[1] for r in conn.execute("PRAGMA table_info(crm_users)").fetchall()]:
            conn.execute("ALTER TABLE crm_users ADD COLUMN tg_id INTEGER")  # personal Telegram for alerts (/link in the bot)
        conn.execute("CREATE TABLE IF NOT EXISTS crm_apartment_info (apartment TEXT PRIMARY KEY, door_code TEXT, keys_in TEXT, keys_out TEXT, "
                     "wifi TEXT, note TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS crm_apartment_photos (id INTEGER PRIMARY KEY AUTOINCREMENT, apartment TEXT NOT NULL, "
                     "media TEXT NOT NULL, mime TEXT, sort INTEGER DEFAULT 0, at TEXT)")
        if "descr" not in [r[1] for r in conn.execute("PRAGMA table_info(crm_apartment_info)").fetchall()]:
            conn.execute("ALTER TABLE crm_apartment_info ADD COLUMN descr TEXT")  # description for guests (rooms, guests, floor, view)
        # v89: automatic «Заезд»/«Выезд» tasks are off by default and the open ones go (they only cluttered the list)
        if not conn.execute("SELECT 1 FROM crm_settings WHERE key = 'auto_tasks_purged'").fetchone():
            conn.execute("DELETE FROM crm_tasks WHERE status = 'open' AND (src_key LIKE 'checkin:%' OR src_key LIKE 'checkout:%')")
            conn.execute("INSERT OR REPLACE INTO crm_settings (key, value) VALUES ('auto_tasks_purged', '1')")
        dcols = [r[1] for r in conn.execute("PRAGMA table_info(crm_deals)").fetchall()]
        if "fields" not in dcols:
            conn.execute("ALTER TABLE crm_deals ADD COLUMN fields TEXT")
        if "flow_key" not in dcols:  # the last stage the booking flow put the deal in (manual moves are respected)
            conn.execute("ALTER TABLE crm_deals ADD COLUMN flow_key TEXT")
        tcols = [r[1] for r in conn.execute("PRAGMA table_info(crm_tasks)").fetchall()]
        for col in ("auto_text", "auto_status", "auto_text_en", "auto_at"):
            if col not in tcols:
                conn.execute(f"ALTER TABLE crm_tasks ADD COLUMN {col} TEXT")  # noqa: S608
        if "auto_tries" not in tcols:
            conn.execute("ALTER TABLE crm_tasks ADD COLUMN auto_tries INTEGER DEFAULT 0")
        if "booking_id" not in tcols:
            conn.execute("ALTER TABLE crm_tasks ADD COLUMN booking_id INTEGER")
            # tasks «Заезд/Выезд» created before this version: attach them to their booking
            conn.execute("UPDATE crm_tasks SET booking_id = CAST(substr(src_key, instr(src_key, ':') + 1) AS INTEGER) "
                         "WHERE booking_id IS NULL AND (src_key LIKE 'checkin:%' OR src_key LIKE 'checkout:%')")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_crm_tasks_booking ON crm_tasks(booking_id)")
        old = conn.execute("SELECT value FROM crm_settings WHERE key = 'booking_checklist'").fetchone()
        if old and old[0].strip() == OLD_CHECKLIST_V42.strip():
            conn.execute("DELETE FROM crm_settings WHERE key = 'booking_checklist'")  # the v42 default → new default with messages
        _settings_cache["at"] = 0.0
        ccols = [r[1] for r in conn.execute("PRAGMA table_info(crm_clients)").fetchall()]
        for col in ("status", "instagram", "telegram", "phone2", "birthday", "passport", "city", "lang"):
            if col not in ccols:
                conn.execute(f"ALTER TABLE crm_clients ADD COLUMN {col} TEXT")  # noqa: S608
        if not conn.execute("SELECT 1 FROM crm_pipelines").fetchone():
            pid = conn.execute("INSERT INTO crm_pipelines (name, sort) VALUES ('Продажи', 0)").lastrowid
            for i, (nm, color, kind) in enumerate(DEFAULT_STAGES):
                conn.execute("INSERT INTO crm_stages (pipeline_id, name, sort, color, kind) VALUES (?, ?, ?, ?, ?)",
                             (pid, nm, i, color, kind))
        if cols is not None and conn.execute("SELECT COUNT(*) FROM inbox_templates").fetchone()[0] == 0:
            for cmd, title, text in DEFAULT_COMMANDS:
                conn.execute("INSERT INTO inbox_templates (title, text, created_at, command) VALUES (?, ?, ?, ?)",
                             (title, text, _now(), cmd or None))
        elif cols is not None:  # existing installs get the new reminder commands once
            for cmd, title, text in DEFAULT_COMMANDS:
                if cmd in ("заезд", "выезд") and not conn.execute("SELECT 1 FROM inbox_templates WHERE command = ?", (cmd,)).fetchone():
                    conn.execute("INSERT INTO inbox_templates (title, text, created_at, command) VALUES (?, ?, ?, ?)", (title, text, _now(), cmd))
    # first admin from .env (otherwise the setup page in the browser asks for one)
    if not has_users() and config.CRM_ADMIN_EMAIL and config.CRM_ADMIN_PASSWORD:
        create_user("Администратор", config.CRM_ADMIN_EMAIL, config.CRM_ADMIN_PASSWORD, "admin")


# ---------------------------------------------------------------------------
# Passwords & sessions
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), 200_000)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:  # noqa: BLE001
        return False


def _secret() -> bytes:
    return ("crm:" + config.APP_SECRET).encode()


def make_token(user: dict) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    body = f"{user['id']}.{user['tver']}.{exp}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return body + "." + sig


def user_from_token(token: str) -> dict | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    body = ".".join(parts[:3])
    if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32], parts[3]):
        return None
    uid, tver, exp = parts[:3]
    if not (uid.isdigit() and tver.isdigit() and exp.isdigit()) or int(exp) < time.time():
        return None
    u = get_user(int(uid))
    if not u or not u["active"] or u["tver"] != int(tver):
        return None
    return u


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def _user_out(r) -> dict:
    u = dict(r)
    u.pop("pass_hash", None)
    u["active"] = bool(u["active"])
    return u


def has_users() -> bool:
    with database.get_conn() as conn:
        return conn.execute("SELECT 1 FROM crm_users LIMIT 1").fetchone() is not None


def get_user(uid: int) -> dict | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM crm_users WHERE id = ?", (uid,)).fetchone()
    return _user_out(r) if r else None


def link_code(uid: int) -> str:
    """A one-time code the user sends to the work bot as «/link CODE»: the bot
    then knows which Telegram account gets this user's alerts."""
    import secrets as _s
    code = _s.token_hex(3).upper()
    set_setting(f"tg_link:{code}", f"{uid}|{(datetime.now() + timedelta(minutes=30)).isoformat(timespec='seconds')}")
    return code


def link_telegram(code: str, tg_id: int, tg_name: str = "") -> dict | None:
    """Called by the bot: «/link CODE» → the CRM user behind the code gets tg_id."""
    code = (code or "").strip().upper()
    raw = get_setting(f"tg_link:{code}")
    if not raw:
        return None
    uid, _, exp = raw.partition("|")
    if not uid.isdigit() or exp < datetime.now().isoformat(timespec="seconds"):
        return None
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_users SET tg_id = NULL WHERE tg_id = ?", (tg_id,))  # one Telegram = one CRM user
        conn.execute("UPDATE crm_users SET tg_id = ? WHERE id = ?", (tg_id, int(uid)))
        conn.execute("DELETE FROM crm_settings WHERE key = ?", (f"tg_link:{code}",))
    settings_dirty()
    return get_user(int(uid))


def unlink_telegram(uid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_users SET tg_id = NULL WHERE id = ?", (uid,))


def users_tg_ids(uids=None, admins_only: bool = False) -> list[int]:
    """Telegram ids of active users (all, the given ones, or admins only)."""
    out = []
    for u in list_users():
        if not u["active"] or not u.get("tg_id"):
            continue
        if uids is not None and u["id"] not in uids:
            continue
        if admins_only and u["role"] != "admin":
            continue
        out.append(int(u["tg_id"]))
    return out


# ---------------------------------------------------------------------------
# Apartments: door codes, keys, Wi-Fi — the «…» of the check-in instruction
# ---------------------------------------------------------------------------
APT_FIELDS = ("door_code", "keys_in", "keys_out", "wifi", "note", "descr")
APT_VARS = {"код домофона": "door_code", "ключи": "keys_in", "ключи при выезде": "keys_out", "wifi": "wifi", "заметка квартиры": "note"}


def apartment_info() -> list[dict]:
    """One row per apartment from the calendar plus the fallbacks «A», «B»
    (per block) and «*» (every apartment); saved values on top."""
    with database.get_conn() as conn:
        saved = {r["apartment"]: dict(r) for r in conn.execute("SELECT * FROM crm_apartment_info").fetchall()}
    names = ["*", "A", "B"] + sorted(set(config.APARTMENTS.values()) | {k for k in saved if k not in ("*", "A", "B")})
    out = []
    for n in names:
        row = {"apartment": n, **{f: "" for f in APT_FIELDS}}
        row.update({k: v or "" for k, v in (saved.get(n) or {}).items() if k in APT_FIELDS})
        row["kind"] = "all" if n == "*" else "block" if n in ("A", "B") else "apt"
        out.append(row)
    return out


def save_apartment_info(rows: list) -> list[dict]:
    with database.get_conn() as conn:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            name = str(r.get("apartment") or "").strip()[:40]
            if not name:
                continue
            vals = [str(r.get(f) or "").strip()[:500] for f in APT_FIELDS]
            if not any(vals):
                conn.execute("DELETE FROM crm_apartment_info WHERE apartment = ?", (name,))
            else:
                conn.execute("INSERT OR REPLACE INTO crm_apartment_info (apartment, door_code, keys_in, keys_out, wifi, note, descr, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (name, *vals, _now()))
    _apt_cache["at"] = 0.0
    return apartment_info()


def apartment_photos(apartment: str | None = None) -> list[dict]:
    """Photos per apartment (A-066) or per block (A, B) as a fallback; with signed URLs for the CRM."""
    from . import inbox
    with database.get_conn() as conn:
        if apartment:
            rows = conn.execute("SELECT * FROM crm_apartment_photos WHERE apartment = ? ORDER BY sort, id", (apartment,)).fetchall()
            if not rows and apartment[:1] in ("A", "B"):
                rows = conn.execute("SELECT * FROM crm_apartment_photos WHERE apartment = ? ORDER BY sort, id", (apartment[:1],)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM crm_apartment_photos ORDER BY apartment, sort, id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["url"] = f"{config.API_PREFIX}/inbox/media/{d['media']}?s={inbox.media_sig(d['media'])}"
        out.append(d)
    return out


def add_apartment_photo(apartment: str, body: bytes, mime: str) -> dict:
    import uuid
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
    name = f"apt{uuid.uuid4().hex}.{ext}"
    config.INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (config.INBOX_MEDIA_DIR / name).write_bytes(body)
    with database.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM crm_apartment_photos WHERE apartment = ?", (apartment,)).fetchone()[0]
        pid = conn.execute("INSERT INTO crm_apartment_photos (apartment, media, mime, sort, at) VALUES (?, ?, ?, ?, ?)",
                           (apartment, name, mime, n, _now())).lastrowid
    return next((p for p in apartment_photos(apartment) if p["id"] == pid), {"id": pid})


def delete_apartment_photo(photo_id: int) -> None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT media FROM crm_apartment_photos WHERE id = ?", (photo_id,)).fetchone()
        conn.execute("DELETE FROM crm_apartment_photos WHERE id = ?", (photo_id,))
    if r:
        try:
            (config.INBOX_MEDIA_DIR / r["media"]).unlink(missing_ok=True)
        except OSError:
            pass


_apt_cache: dict = {"at": 0.0, "map": {}}


def apartment_vars(apartment: str | None) -> dict:
    """{var: value} for a booking's apartment: the apartment's own value, else
    its block's («A»/«B»), else «*». Cached 30 s (filled on every message)."""
    if time.time() - _apt_cache["at"] > 30:
        with database.get_conn() as conn:
            _apt_cache["map"] = {r["apartment"]: dict(r) for r in conn.execute("SELECT * FROM crm_apartment_info").fetchall()}
        _apt_cache["at"] = time.time()
    m = _apt_cache["map"]
    apt = (apartment or "").strip().upper()
    chain = [m.get((apartment or "").strip()), m.get(apt[:1]) if apt[:1] in ("A", "B") else None, m.get("*")]
    out = {}
    for var, field in APT_VARS.items():
        out[var] = next((r[field] for r in chain if r and (r.get(field) or "").strip()), "")
    return out


def list_users() -> list[dict]:
    with database.get_conn() as conn:
        return [_user_out(r) for r in conn.execute("SELECT * FROM crm_users ORDER BY id").fetchall()]


def create_user(name: str, email: str, password: str, role: str = "manager") -> dict:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Некорректный email")
    if len(password) < 6:
        raise ValueError("Пароль — не короче 6 символов")
    with database.get_conn() as conn:
        if conn.execute("SELECT 1 FROM crm_users WHERE email = ?", (email,)).fetchone():
            raise ValueError("Пользователь с таким email уже есть")
        uid = conn.execute(
            "INSERT INTO crm_users (name, email, pass_hash, role, active, tver, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)",
            (name.strip() or email, email, hash_password(password), "admin" if role == "admin" else "manager", _now()),
        ).lastrowid
    return get_user(uid)


def update_user(uid: int, name=None, email=None, password=None, role=None, active=None) -> dict:
    with database.get_conn() as conn:
        u = conn.execute("SELECT * FROM crm_users WHERE id = ?", (uid,)).fetchone()
        if not u:
            raise ValueError("Пользователь не найден")
        sets, args = [], []
        if name is not None:
            sets.append("name = ?"); args.append(name.strip() or u["name"])
        if email is not None:
            email = email.strip().lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("Некорректный email")
            if conn.execute("SELECT 1 FROM crm_users WHERE email = ? AND id != ?", (email, uid)).fetchone():
                raise ValueError("Этот email уже занят")
            sets.append("email = ?"); args.append(email)
        if password:
            if len(password) < 6:
                raise ValueError("Пароль — не короче 6 символов")
            sets.append("pass_hash = ?"); args.append(hash_password(password))
            sets.append("tver = tver + 1")
        if role is not None:
            sets.append("role = ?"); args.append("admin" if role == "admin" else "manager")
        if active is not None:
            sets.append("active = ?"); args.append(1 if active else 0)
            if not active:
                sets.append("tver = tver + 1")  # blocked = logged out everywhere
        if sets:
            conn.execute(f"UPDATE crm_users SET {', '.join(sets)} WHERE id = ?", (*args, uid))  # noqa: S608
    return get_user(uid)


def bump_tver(uid: int) -> None:
    """Invalidate every session cookie of the user (logout everywhere)."""
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_users SET tver = tver + 1 WHERE id = ?", (uid,))


def login(email: str, password: str) -> dict | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM crm_users WHERE email = ?", (email.strip().lower(),)).fetchone()
        if not r or not r["active"] or not check_password(password, r["pass_hash"]):
            return None
        conn.execute("UPDATE crm_users SET last_login = ? WHERE id = ?", (_now(), r["id"]))
    return _user_out(r)


def user_names() -> dict[int, str]:
    return {u["id"]: u["name"] for u in list_users()}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
_settings_cache: dict = {"at": 0.0, "map": {}}
SETTINGS_TTL = 10.0


def settings_dirty() -> None:
    """Call after any direct write to crm_settings."""
    _settings_cache["at"] = 0.0


def _settings() -> dict:
    """The whole table, cached for a few seconds: get_setting runs in loops
    (every chat row, task, step, scheduler tick) and used to open a connection
    each time."""
    if time.time() - _settings_cache["at"] > SETTINGS_TTL:
        with database.get_conn() as conn:
            _settings_cache["map"] = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM crm_settings").fetchall()}
        _settings_cache["at"] = time.time()
    return _settings_cache["map"]


def get_setting(key: str, default: str = "") -> str:
    v = _settings().get(key)
    return default if v is None else v


def _hm(value, default: str = "00:00") -> str:
    """'9:5' / '14:00:00' / garbage → 'HH:MM' (string compare of times is only
    safe when every time has the same shape)."""
    m = re.match(r"^\s*(\d{1,2})[:.](\d{1,2})", str(value or ""))
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return default
    return f"{h:02d}:{mi:02d}"


def default_times() -> tuple[str, str]:
    """House check-in / check-out time (Интеграции → Время заезда и выезда)."""
    return _hm(get_setting("checkin_time", "14:00"), "14:00"), _hm(get_setting("checkout_time", "11:00"), "11:00")


def set_setting(key: str, value: str) -> None:
    with database.get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO crm_settings (key, value) VALUES (?, ?)", (key, value))
    _settings_cache["map"][key] = value  # visible at once, whatever the TTL


# ---------------------------------------------------------------------------
# Pipelines & stages
# ---------------------------------------------------------------------------
def pipelines() -> list[dict]:
    with database.get_conn() as conn:
        ps = [dict(r) for r in conn.execute("SELECT * FROM crm_pipelines ORDER BY sort, id").fetchall()]
        for p in ps:
            p["stages"] = [dict(r) for r in conn.execute(
                "SELECT * FROM crm_stages WHERE pipeline_id = ? ORDER BY sort, id", (p["id"],)).fetchall()]
            for s in p["stages"]:
                s["actions"] = [dict(r) for r in conn.execute(
                    "SELECT * FROM crm_stage_actions WHERE stage_id = ? ORDER BY sort, id", (s["id"],)).fetchall()]
    return ps


def save_pipeline(pid: int | None, name: str, stages: list[dict]) -> dict:
    """Create/rename a pipeline and sync its stages: {id?, name, color, kind}.
    Deals on a removed stage move to the pipeline's first stage."""
    name = name.strip()
    if not name:
        raise ValueError("Название воронки")
    stages = [s for s in stages if (s.get("name") or "").strip()]
    if not stages:
        raise ValueError("Нужен хотя бы один этап")
    with database.get_conn() as conn:
        if pid:
            if not conn.execute("SELECT 1 FROM crm_pipelines WHERE id = ?", (pid,)).fetchone():
                raise ValueError("Воронка не найдена")
            conn.execute("UPDATE crm_pipelines SET name = ? WHERE id = ?", (name, pid))
        else:
            pid = conn.execute("INSERT INTO crm_pipelines (name, sort) VALUES (?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM crm_pipelines))",
                               (name,)).lastrowid
        keep = []
        for i, s in enumerate(stages):
            kind = s.get("kind") if s.get("kind") in ("open", "won", "lost") else "open"
            sid = s.get("id")
            if sid and conn.execute("SELECT 1 FROM crm_stages WHERE id = ? AND pipeline_id = ?", (sid, pid)).fetchone():
                conn.execute("UPDATE crm_stages SET name = ?, sort = ?, color = ?, kind = ? WHERE id = ?",
                             (s["name"].strip(), i, s.get("color") or None, kind, sid))
            else:
                sid = conn.execute("INSERT INTO crm_stages (pipeline_id, name, sort, color, kind) VALUES (?, ?, ?, ?, ?)",
                                   (pid, s["name"].strip(), i, s.get("color") or None, kind)).lastrowid
            keep.append(sid)
            # stage actions: replace the set
            conn.execute("DELETE FROM crm_stage_actions WHERE stage_id = ?", (sid,))
            for j, a in enumerate(s.get("actions") or []):
                if a.get("kind") not in ("message", "task", "notify") or not (a.get("text") or "").strip():
                    continue
                conn.execute("INSERT INTO crm_stage_actions (stage_id, kind, text, days, at_time, sort) VALUES (?, ?, ?, ?, ?, ?)",
                             (sid, a["kind"], a["text"].strip(), int(a.get("days") or 0), (a.get("at_time") or "10:00")[:5], j))
        first = keep[0]
        gone = [r[0] for r in conn.execute("SELECT id FROM crm_stages WHERE pipeline_id = ?", (pid,)).fetchall()
                if r[0] not in keep]
        for sid in gone:
            conn.execute("UPDATE crm_deals SET stage_id = ? WHERE stage_id = ?", (first, sid))
            conn.execute("DELETE FROM crm_stages WHERE id = ?", (sid,))
            conn.execute("DELETE FROM crm_stage_actions WHERE stage_id = ?", (sid,))
    return next(p for p in pipelines() if p["id"] == pid)


def delete_pipeline(pid: int) -> None:
    with database.get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM crm_pipelines").fetchone()[0] <= 1:
            raise ValueError("Нельзя удалить последнюю воронку")
        if conn.execute("SELECT 1 FROM crm_deals WHERE pipeline_id = ? LIMIT 1", (pid,)).fetchone():
            raise ValueError("В воронке есть сделки — сначала перенесите их")
        conn.execute("DELETE FROM crm_stage_actions WHERE stage_id IN (SELECT id FROM crm_stages WHERE pipeline_id = ?)", (pid,))
        conn.execute("DELETE FROM crm_stages WHERE pipeline_id = ?", (pid,))
        conn.execute("DELETE FROM crm_pipelines WHERE id = ?", (pid,))


# ---------------------------------------------------------------------------
# Card fields
# ---------------------------------------------------------------------------
def fields(entity: str | None = None) -> list[dict]:
    """Custom fields; entity = client | deal (None = all)."""
    with database.get_conn() as conn:
        out = []
        for r in conn.execute("SELECT * FROM crm_fields ORDER BY sort, id").fetchall():
            f = dict(r)
            f["entity"] = f.get("entity") or "client"
            if entity and f["entity"] != entity:
                continue
            f["options"] = [o.strip() for o in (f["options"] or "").splitlines() if o.strip()]
            out.append(f)
        return out


def save_field(fid: int | None, name: str, type_: str, options: list[str], entity: str = "client") -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Название поля")
    type_ = type_ if type_ in ("text", "number", "date", "select", "checkbox") else "text"
    entity = entity if entity in ("client", "deal") else "client"
    opts = "\n".join(o.strip() for o in options if o.strip())
    with database.get_conn() as conn:
        if fid:
            conn.execute("UPDATE crm_fields SET name = ?, type = ?, options = ?, entity = ? WHERE id = ?", (name, type_, opts, entity, fid))
        else:
            fid = conn.execute("INSERT INTO crm_fields (name, type, options, entity, sort) VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM crm_fields))",
                               (name, type_, opts, entity)).lastrowid
    return next(f for f in fields() if f["id"] == fid)


def delete_field(fid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_fields WHERE id = ?", (fid,))


def reorder(table: str, ids: list[int]) -> None:
    if table not in ("crm_fields", "crm_pipelines"):
        return
    with database.get_conn() as conn:
        for i, x in enumerate(ids):
            conn.execute(f"UPDATE {table} SET sort = ? WHERE id = ?", (i, x))  # noqa: S608


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
digits = util.digits


def _client_out(r) -> dict:
    c = dict(r)
    try:
        c["fields"] = json.loads(c["fields"] or "{}")
    except ValueError:
        c["fields"] = {}
    c["status"] = c.get("status") or ""
    for k in PROFILE_FIELDS:
        c[k] = c.get(k) or ""
    return c


PROFILE_FIELDS = ("instagram", "telegram", "phone2", "birthday", "passport", "city", "lang")


def client_chats(cid: int) -> list[dict]:
    """All conversations with this guest across channels (by phone and by the chat on the card)."""
    from . import inbox
    with database.get_conn() as conn:
        c = conn.execute("SELECT phone, chat_id FROM crm_clients WHERE id = ?", (cid,)).fetchone()
        if not c:
            return []
        ids = []
        if c["chat_id"]:
            ids.append(c["chat_id"])
        if c["phone"] and len(c["phone"]) >= 7:
            ids += [r["id"] for r in conn.execute("SELECT id FROM inbox_chats WHERE phone LIKE ? ORDER BY last_at DESC",
                                                   ("%" + c["phone"][-9:],)).fetchall()]
    out, seen = [], set()
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        ch = inbox.get_chat(i)
        if ch:
            out.append({k: ch.get(k) for k in ("id", "title", "channel", "channel_name", "last_text", "last_at", "unread", "phone")})
    return out


def note_channel(cid: int, channel: str, handle: str = "") -> None:
    """A guest that came through Instagram/Telegram: remember the handle on the card."""
    col = "instagram" if channel in ("ig", "wzig") else "telegram" if channel in ("tg", "wztg", "tgbot") else None
    if not col or not handle:
        return
    with database.get_conn() as conn:
        conn.execute(f"UPDATE crm_clients SET {col} = COALESCE(NULLIF({col}, ''), ?), updated_at = ? WHERE id = ?",  # noqa: S608
                     (handle.strip(), _now(), cid))


# ---------------------------------------------------------------------------
# Client statuses («Без брони», «Тёплый», «Постоянник»…): the owner edits the
# list himself (Поля карточек). One line = «Название | #цвет».
# ---------------------------------------------------------------------------
DEFAULT_CLIENT_STATUSES = """Новый | #6B7280
Без брони | #9CA3AF
Тёплый | #F59E0B
Забронировал | #2563EB
Живёт сейчас | #10B981
Постоянник | #8B5CF6
Отменил бронь | #F97316
VIP | #DB2777
Не беспокоить | #EF4444"""

# statuses the calendar sets by itself; the rest are set by people and never overwritten
AUTO_STATUSES = ("Новый", "Без брони", "Забронировал", "Живёт сейчас", "Постоянник", "Отменил бронь")


def auto_client_status(c: dict, cancelled: bool = False) -> str | None:
    """The guest's latest state from the calendar: living now / booked / regular / no booking."""
    today = date.today().isoformat()
    bs = [b for b in (c.get("bookings") or [])]
    living = any(b["checkin"] <= today < b["checkout"] for b in bs)
    future = any(b["checkin"] > today for b in bs)
    past = [b for b in bs if b["checkout"] <= today]
    if living:
        return "Живёт сейчас"
    if future:
        return "Забронировал"
    if cancelled:
        return "Отменил бронь"
    if len(past) >= 2:
        return "Постоянник"
    if past:
        return "Без брони"
    return None


def refresh_client_statuses(cancelled_ids: set | None = None) -> int:
    """After every sync: clients whose status is automatic (or empty) follow the calendar."""
    names = {s["name"] for s in client_statuses()}
    n = 0
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT id, phone, status FROM crm_clients WHERE phone != ''").fetchall()]
    # one phone → stays index for the whole pass (was: a scan of every booking
    # with a regex per client — a million regex calls on a big base)
    from . import inbox
    idx = inbox._bookings_by_phone()  # noqa: SLF001
    for r in rows:
        if r["status"] and r["status"] not in AUTO_STATUSES:
            continue  # set by a person: keep
        ph = digits(r["phone"])
        c = {"bookings": [{"checkin": b["begin_date"], "checkout": b["end_date"]} for b in (idx.get(ph[-9:]) if len(ph) >= 7 else None) or []]}
        want = auto_client_status(c, cancelled=r["id"] in (cancelled_ids or set()))
        if want and want in names and want != (r["status"] or ""):
            set_client_status(r["id"], want)
            n += 1
    return n


def client_statuses() -> list[dict]:
    out = []
    for line in (get_setting("client_statuses", DEFAULT_CLIENT_STATUSES) or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, color = line.partition("|")
        color = color.strip()
        out.append({"name": name.strip(), "color": color if re.match(r"^#[0-9A-Fa-f]{6}$", color) else "#6B7280"})
    return out


def set_client_status(cid: int, status: str) -> None:
    status = (status or "").strip()
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_clients SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), cid))


def clients(q: str = "", limit: int = 500, status: str | None = None) -> list[dict]:
    with database.get_conn() as conn:
        rows = [_client_out(r) for r in conn.execute(
            "SELECT * FROM crm_clients ORDER BY updated_at DESC, id DESC LIMIT ?", (5000 if (q or status) else limit,)).fetchall()]
    if status is not None:
        rows = [c for c in rows if (c["status"] or "") == status][:limit]
    if q:
        ql, qd = q.lower().strip(), digits(q)
        rows = [c for c in rows if ql in (c["name"] or "").lower() or ql in (c["email"] or "").lower()
                or (qd and qd in (c["phone"] or "")) or ql in (c["notes"] or "").lower()][:limit]
    return rows


def get_client(cid: int) -> dict | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM crm_clients WHERE id = ?", (cid,)).fetchone()
    if not r:
        return None
    c = _client_out(r)
    c["deals"] = deals(client_id=cid)
    c["tasks"] = tasks(client_id=cid)
    c["bookings"] = client_bookings(c["phone"] or "", c["name"] or "")
    if not c["chat_id"] and c["phone"]:
        c["chat_id"] = _chat_id_for_phone(c["phone"])
    c["chats"] = client_chats(cid)
    try:
        from . import docs
        c["documents"] = docs.documents(client_id=cid)
    except Exception:  # noqa: BLE001
        c["documents"] = []
    return c


def _chat_id_for_phone(phone: str):
    if len(phone) < 7:
        return None
    with database.get_conn() as conn:
        r = conn.execute("SELECT id FROM inbox_chats WHERE phone LIKE ? ORDER BY last_at DESC LIMIT 1",
                         ("%" + phone[-9:],)).fetchone()
    return r["id"] if r else None


def client_bookings(phone: str, name: str = "") -> list[dict]:
    phone = digits(phone)
    if phone and len(phone) >= 7:
        from . import inbox
        rows = sorted(inbox._bookings_by_phone().get(phone[-9:]) or [], key=lambda r: r["begin_date"], reverse=True)  # noqa: SLF001
        return [_booking_out(dict(r)) for r in rows[:50]]
    if not name:
        return []
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM bookings WHERE COALESCE(is_delete, 0) = 0 AND LOWER(TRIM(COALESCE(client_name, ''))) = ? "
                            "ORDER BY begin_date DESC LIMIT 50", (name.strip().lower(),)).fetchall()
    return [_booking_out(dict(r)) for r in rows]


def save_client(cid: int | None, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    phone = digits(data.get("phone"))
    if len(phone) == 9:
        phone = "998" + phone
    if not name and not phone:
        raise ValueError("Укажите имя или телефон")
    fld = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    with database.get_conn() as conn:
        if cid:
            cur = conn.execute("SELECT * FROM crm_clients WHERE id = ?", (cid,)).fetchone()
            status = (data.get("status") if "status" in data else (cur["status"] if cur else "")) or ""
            prof = [((data.get(k) if k in data else (cur[k] if cur else "")) or "").strip() for k in PROFILE_FIELDS]
            conn.execute(
                "UPDATE crm_clients SET name = ?, phone = ?, email = ?, source = ?, notes = ?, fields = ?, status = ?, "
                "instagram = ?, telegram = ?, phone2 = ?, birthday = ?, passport = ?, city = ?, lang = ?, updated_at = ? WHERE id = ?",
                (name, phone, (data.get("email") or "").strip(), (data.get("source") or "").strip(),
                 data.get("notes") or "", json.dumps(fld, ensure_ascii=False), status.strip(), *prof, _now(), cid))
        else:
            if phone:
                dup = conn.execute("SELECT id FROM crm_clients WHERE phone LIKE ?", ("%" + phone[-9:],)).fetchone()
                if dup:
                    raise ValueError(f"Клиент с этим номером уже есть (#{dup['id']})")
            prof = [(data.get(k) or "").strip() for k in PROFILE_FIELDS]
            cid = conn.execute(
                "INSERT INTO crm_clients (name, phone, email, source, notes, fields, status, instagram, telegram, phone2, birthday, passport, city, lang, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name or ("+" + phone), phone, (data.get("email") or "").strip(), (data.get("source") or "").strip(),
                 data.get("notes") or "", json.dumps(fld, ensure_ascii=False), (data.get("status") or "").strip(), *prof, _now(), _now())).lastrowid
    return get_client(cid)


def delete_client(cid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_deals SET client_id = NULL WHERE client_id = ?", (cid,))
        conn.execute("UPDATE crm_tasks SET client_id = NULL WHERE client_id = ?", (cid,))
        conn.execute("DELETE FROM crm_clients WHERE id = ?", (cid,))


def ensure_client(phone: str, name: str = "", source: str = "", chat_id=None) -> int | None:
    """Find or create the client for a phone number (chats, bookings)."""
    phone = digits(phone)
    if len(phone) < 7:
        return None
    with database.get_conn() as conn:
        r = conn.execute("SELECT id, name, chat_id FROM crm_clients WHERE phone LIKE ? ORDER BY id LIMIT 1",
                         ("%" + phone[-9:],)).fetchone()
        if r:
            if (name and (not r["name"] or r["name"].startswith("+"))) or (chat_id and not r["chat_id"]):
                conn.execute("UPDATE crm_clients SET name = COALESCE(NULLIF(?, ''), name), chat_id = COALESCE(chat_id, ?), updated_at = ? WHERE id = ?",
                             (name if (not r["name"] or r["name"].startswith("+")) else "", chat_id, _now(), r["id"]))
            return r["id"]
        return conn.execute(
            "INSERT INTO crm_clients (name, phone, source, fields, chat_id, created_at, updated_at) VALUES (?, ?, ?, '{}', ?, ?, ?)",
            (name.strip() or "+" + phone, phone, source, chat_id, _now(), _now())).lastrowid


def ensure_client_for_chat(chat_id: int, name: str = "", source: str = "") -> int:
    """Client card for a chat without a phone number (Instagram, Telegram)."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT id FROM crm_clients WHERE chat_id = ?", (chat_id,)).fetchone()
        if r:
            return r["id"]
        return conn.execute(
            "INSERT INTO crm_clients (name, phone, source, fields, chat_id, created_at, updated_at) VALUES (?, '', ?, '{}', ?, ?, ?)",
            (name.strip() or source or "Гость", source, chat_id, _now(), _now())).lastrowid


def import_clients() -> int:
    """Create clients for every phone seen in bookings and chats."""
    n = 0
    with database.get_conn() as conn:
        bk = conn.execute("SELECT client_name, client_phone, source_id FROM bookings WHERE COALESCE(is_delete, 0) = 0 "
                          "AND client_phone IS NOT NULL AND client_phone != '' ORDER BY begin_date DESC").fetchall()
        ch = conn.execute("SELECT id, phone, name, push_name, channel FROM inbox_chats").fetchall()
        known = {}  # last 9 digits → (name, chat_id): skip the lookup for everyone already on file
        for r in conn.execute("SELECT phone, name, chat_id FROM crm_clients WHERE phone != ''").fetchall():
            d = digits(r["phone"])
            if len(d) >= 7:
                known.setdefault(d[-9:], (r["name"] or "", r["chat_id"]))
        chat_known = {r[0] for r in conn.execute("SELECT chat_id FROM crm_clients WHERE chat_id IS NOT NULL").fetchall()}
        before = conn.execute("SELECT COUNT(*) FROM crm_clients").fetchone()[0]

    def needs(phone, name, chat_id=None):
        d = digits(phone)
        if len(d) < 7:
            return False
        k = known.get(d[-9:])
        if k is None:
            return True
        return bool(name and (not k[0] or k[0].startswith("+"))) or bool(chat_id and not k[1])

    for r in bk:
        if needs(r["client_phone"], r["client_name"]):
            ensure_client(r["client_phone"], r["client_name"] or "", config.SOURCE_NAMES.get(r["source_id"], "") or "")
            known[digits(r["client_phone"])[-9:]] = (r["client_name"] or "", None)
    for r in ch:
        src = {"ig": "Instagram", "tg": "Telegram", "tgbot": "Telegram"}.get(r["channel"] or "wa", "WhatsApp")
        if r["phone"]:
            if needs(r["phone"], r["name"] or r["push_name"], r["id"]):
                ensure_client(r["phone"], r["name"] or r["push_name"] or "", src, r["id"])
                known[digits(r["phone"])[-9:]] = (r["name"] or r["push_name"] or "", r["id"])
        elif r["id"] not in chat_known:
            ensure_client_for_chat(r["id"], r["name"] or r["push_name"] or "", src)
    with database.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM crm_clients").fetchone()[0] - before
    return n


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------
def _deal_out(r, names=None, clients_map=None) -> dict:
    d = dict(r)
    try:
        d["fields"] = json.loads(d.get("fields") or "{}")
    except (ValueError, TypeError):
        d["fields"] = {}
    if names is not None:
        d["owner_name"] = names.get(d["owner_uid"])
    if clients_map is not None and d["client_id"]:
        c = clients_map.get(d["client_id"])
        d["client_name"] = c["name"] if c else None
        d["client_phone"] = c["phone"] if c else None
    return d


def deals(pipeline_id: int | None = None, client_id: int | None = None, q: str = "") -> list[dict]:
    where, args = [], []
    if pipeline_id:
        where.append("pipeline_id = ?"); args.append(pipeline_id)
    if client_id:
        where.append("client_id = ?"); args.append(client_id)
    sql = "SELECT * FROM crm_deals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC, id DESC"
    with database.get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
        cmap = _clients_map(conn, [r["client_id"] for r in rows], "id, name, phone")
        act: dict = {}
        try:
            for a in conn.execute("SELECT l.deal_id, COALESCE(SUM(c.unread), 0) AS unread, MAX(c.last_at) AS last_at, COUNT(c.id) AS chats "
                                  "FROM crm_deal_chats l JOIN inbox_chats c ON c.id = l.chat_id GROUP BY l.deal_id").fetchall():
                act[a["deal_id"]] = {"unread": a["unread"], "last_msg_at": a["last_at"], "chats": a["chats"]}
        except Exception:  # noqa: BLE001 — amo tables not there yet
            pass
    names = user_names()
    out = []
    for r in rows:
        d = _deal_out(r, names, cmap)
        d.update(act.get(d["id"]) or {"unread": 0, "last_msg_at": None, "chats": 0})
        out.append(d)
    if q:
        ql, qd = q.lower(), digits(q)
        out = [d for d in out if ql in (d["title"] or "").lower() or ql in (d.get("client_name") or "").lower()
               or ql in (d["apartment"] or "").lower() or (qd and qd in (d.get("client_phone") or ""))]
    return out


def get_deal(did: int) -> dict | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM crm_deals WHERE id = ?", (did,)).fetchone()
        cmap = _clients_map(conn, [r["client_id"]], "id, name, phone") if r else {}
    if not r:
        return None
    d = _deal_out(r, user_names(), cmap)
    d["tasks"] = tasks(deal_id=did)
    return d


def save_deal(did: int | None, data: dict, uid: int) -> dict:
    title = (data.get("title") or "").strip()
    with database.get_conn() as conn:
        if did:
            cur = conn.execute("SELECT * FROM crm_deals WHERE id = ?", (did,)).fetchone()
            if not cur:
                raise ValueError("Сделка не найдена")
        else:
            cur = None
        pipeline_id = data.get("pipeline_id") or (cur and cur["pipeline_id"])
        stage_id = data.get("stage_id") or (cur and cur["stage_id"])
        if not pipeline_id:
            pr = conn.execute("SELECT id FROM crm_pipelines ORDER BY sort, id LIMIT 1").fetchone()
            pipeline_id = pr["id"]
        st = conn.execute("SELECT * FROM crm_stages WHERE id = ? AND pipeline_id = ?", (stage_id, pipeline_id)).fetchone() if stage_id else None
        if not st:
            st = conn.execute("SELECT * FROM crm_stages WHERE pipeline_id = ? ORDER BY sort, id LIMIT 1", (pipeline_id,)).fetchone()
        if not st:
            raise ValueError("В воронке нет этапов")
        client_id = data.get("client_id") if "client_id" in data else (cur and cur["client_id"])
        if not title and cur and "title" not in data:
            title = cur["title"]  # a stage move / note edit keeps the name
        if not title:
            cn = conn.execute("SELECT name FROM crm_clients WHERE id = ?", (client_id,)).fetchone() if client_id else None
            title = (cn["name"] if cn else "Сделка") + (f" · {data.get('apartment')}" if data.get("apartment") else "")
        closed = _now() if st["kind"] in ("won", "lost") else None
        vals = dict(
            title=title, client_id=client_id, pipeline_id=pipeline_id, stage_id=st["id"],
            amount=_num(data.get("amount")) if "amount" in data else (cur and cur["amount"]),
            apartment=(data.get("apartment") or "").strip() or None if "apartment" in data else (cur and cur["apartment"]),
            checkin=(data.get("checkin") or None) if "checkin" in data else (cur and cur["checkin"]),
            checkout=(data.get("checkout") or None) if "checkout" in data else (cur and cur["checkout"]),
            guests=_num(data.get("guests")) if "guests" in data else (cur and cur["guests"]),
            booking_id=data.get("booking_id") if "booking_id" in data else (cur and cur["booking_id"]),
            owner_uid=data.get("owner_uid") if "owner_uid" in data else ((cur and cur["owner_uid"]) or uid),
            notes=(data.get("notes") or "") if "notes" in data else (cur and cur["notes"]),
            fields=json.dumps(data["fields"], ensure_ascii=False) if isinstance(data.get("fields"), dict) else (cur and cur["fields"]),
        )
        stage_changed = not cur or cur["stage_id"] != st["id"]
        if did:
            conn.execute(
                "UPDATE crm_deals SET title=?, client_id=?, pipeline_id=?, stage_id=?, amount=?, apartment=?, checkin=?, "
                "checkout=?, guests=?, booking_id=?, owner_uid=?, notes=?, fields=?, updated_at=?, closed_at=? WHERE id=?",
                (*vals.values(), _now(), closed, did))
        else:
            did = conn.execute(
                "INSERT INTO crm_deals (title, client_id, pipeline_id, stage_id, amount, apartment, checkin, checkout, "
                "guests, booking_id, owner_uid, notes, fields, created_at, updated_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*vals.values(), _now(), _now(), closed)).lastrowid
    if stage_changed:
        # «digital pipeline»: the stage's actions run after the deal is saved
        try:
            from . import crm_ext
            crm_ext.run_stage_actions(did, st["id"], uid)
        except Exception:  # noqa: BLE001
            logger.exception("stage actions failed")
    return get_deal(did)


_num = util.num


def delete_deal(did: int) -> None:
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_tasks SET deal_id = NULL WHERE deal_id = ?", (did,))
        conn.execute("DELETE FROM crm_deal_chats WHERE deal_id = ?", (did,))
        conn.execute("DELETE FROM crm_notes WHERE deal_id = ?", (did,))
        conn.execute("DELETE FROM crm_deals WHERE id = ?", (did,))


def deal_for_booking(booking_id: int, uid: int) -> dict:
    """«Сделка» on a booking: open the existing one or create it from the booking."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT id FROM crm_deals WHERE booking_id = ?", (booking_id,)).fetchone()
        if r:
            return get_deal(r["id"])
        b = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if not b:
            raise ValueError("Бронь не найдена")
        booked = conn.execute("SELECT s.id, s.pipeline_id FROM crm_stages s JOIN crm_pipelines p ON p.id = s.pipeline_id "
                              "WHERE s.name LIKE 'Заброн%' ORDER BY p.sort, s.sort LIMIT 1").fetchone()
    cid = ensure_client(b["client_phone"] or "", b["client_name"] or "", config.SOURCE_NAMES.get(b["source_id"], "") or "")
    data = {"client_id": cid, "apartment": b["apartment_name"], "checkin": b["begin_date"], "checkout": b["end_date"],
            "amount": b["amount"], "booking_id": booking_id,
            "title": f"{b['client_name'] or 'Гость'} · {b['apartment_name']} · {_dm(b['begin_date'])}–{_dm(b['end_date'])}"}
    if booked:
        data.update(pipeline_id=booked["pipeline_id"], stage_id=booked["id"])
    return save_deal(None, data, uid)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
def _booking_label(b: dict | None) -> str | None:
    if not b:
        return None
    return f"{b.get('apartment_name') or ''} · {_dm(b.get('begin_date'))}–{_dm(b.get('end_date'))} · {b.get('client_name') or 'гость'}"


def _task_out(r, names, cmap, bmap=None) -> dict:
    t = dict(r)
    t["assignee_name"] = names.get(t["assignee_uid"])
    c = cmap.get(t["client_id"]) if t["client_id"] else None
    t["client_name"] = c["name"] if c else None
    b = (bmap or {}).get(t.get("booking_id")) if t.get("booking_id") else None
    t["booking"] = {"id": b["id"], "apartment": b.get("apartment_name"), "checkin": b.get("begin_date"), "checkout": b.get("end_date"),
                    "guest": b.get("client_name"), "label": _booking_label(b)} if b else None
    t["is_message"] = bool(t.get("auto_text"))
    return t


def _clients_map(conn, ids, cols: str = "id, name") -> dict:
    """{client_id: row} for the ids at hand — not the whole table per request."""
    ids = [i for i in set(ids) if i]
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in conn.execute(f"SELECT {cols} FROM crm_clients WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall():  # noqa: S608
            out[r["id"]] = dict(r)
    return out


def _bookings_map(conn, ids) -> dict:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in conn.execute("SELECT id, apartment_name, begin_date, end_date, client_name, client_phone, arrival_time, departure_time, status "
                              f"FROM bookings WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall():
            out[r["id"]] = dict(r)
    return out


def _view_until(view: str | None) -> str | None:
    """Tasks page filter: today = due by tonight, week = by the end of +7 days."""
    if view == "today":
        return date.today().isoformat() + "T23:59"
    if view == "week":
        return (date.today() + timedelta(days=7)).isoformat() + "T23:59"
    return None


MANUAL_ONLY = "src_key IS NULL"  # tasks people created (or the assistant): not the automatic check-in/out and scenario steps


def tasks(assignee_uid: int | None = None, status: str | None = None, client_id: int | None = None,
          deal_id: int | None = None, booking_id: int | None = None, view: str | None = None,
          limit: int = 500, offset: int = 0) -> list[dict]:
    where, args = [], []
    if assignee_uid:
        where.append("assignee_uid = ?"); args.append(assignee_uid)
    if status in ("open", "done"):
        where.append("status = ?"); args.append(status)
    if client_id:
        where.append("client_id = ?"); args.append(client_id)
    if deal_id:
        where.append("deal_id = ?"); args.append(deal_id)
    if booking_id:
        where.append("booking_id = ?"); args.append(booking_id)
    if not booking_id and not deal_id:
        where.append(MANUAL_ONLY)  # automatic steps (check-in/out, scenario messages) are not people's to-dos
    until = _view_until(view)
    if until:  # tasks without a date are always «current»
        where.append("(due IS NULL OR due <= ?)"); args.append(until)
    sql = "SELECT * FROM crm_tasks" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, COALESCE(due, '9999') , id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit or 500), 2000)), max(0, int(offset or 0))]
    with database.get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
        cmap = _clients_map(conn, [r["client_id"] for r in rows])
        bmap = _bookings_map(conn, [r["booking_id"] for r in rows])
    names = user_names()
    return [_task_out(r, names, cmap, bmap) for r in rows]


def tasks_by_booking(assignee_uid: int | None = None, status: str = "open") -> list[dict]:
    """Tasks page → «По броням»: one group per booking (nearest check-in first),
    with progress, so nothing about a stay is forgotten."""
    rows = [t for t in tasks(assignee_uid) if t.get("booking_id")]  # all statuses: progress counts the whole list
    groups: dict[int, dict] = {}
    now = _now()
    for t in rows:
        g = groups.get(t["booking_id"])
        if not g:
            b = t["booking"] or {"id": t["booking_id"], "label": f"Бронь #{t['booking_id']}", "checkin": "9999"}
            g = groups[t["booking_id"]] = {"booking": b, "tasks": [], "open": 0, "done": 0, "overdue": 0}
        if status == "all" or t["status"] == status:
            g["tasks"].append(t)
        if t["status"] == "done":
            g["done"] += 1
        else:
            g["open"] += 1
            if t["due"] and t["due"] < now:
                g["overdue"] += 1
    return sorted([g for g in groups.values() if g["tasks"]], key=lambda g: (g["booking"].get("checkin") or "9999", g["booking"]["id"]))


def task_counts(conn, ids) -> dict:
    """{booking_id: {"open", "done", "overdue"}} for the day view / chess board."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    now = _now()
    out: dict[int, dict] = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in conn.execute("SELECT booking_id, SUM(status = 'open') AS o, SUM(status = 'done') AS d, "
                              "SUM(status = 'open' AND due IS NOT NULL AND due < ?) AS late FROM crm_tasks "
                              f"WHERE booking_id IN ({','.join('?' * len(chunk))}) GROUP BY booking_id", (now, *chunk)).fetchall():
            out[r["booking_id"]] = {"open": r["o"] or 0, "done": r["d"] or 0, "overdue": r["late"] or 0}
    return out


def save_task(tid: int | None, data: dict, uid: int) -> dict:
    with database.get_conn() as conn:
        cur = conn.execute("SELECT * FROM crm_tasks WHERE id = ?", (tid,)).fetchone() if tid else None
        if tid and not cur:
            raise ValueError("Задача не найдена")
        title = (data.get("title") if "title" in data else (cur and cur["title"]) or "").strip()
        if not title:
            raise ValueError("Текст задачи")
        due = data.get("due") if "due" in data else (cur and cur["due"])
        due = (due or "")[:16] or None
        status = data.get("status") if data.get("status") in ("open", "done") else (cur["status"] if cur else "open")
        booking_id = data.get("booking_id") if "booking_id" in data else (cur and cur["booking_id"])
        booking_id = int(booking_id) if booking_id else None
        client_id = data.get("client_id") if "client_id" in data else (cur and cur["client_id"])
        if booking_id and not client_id:  # a booking task is also the guest's task
            b = conn.execute("SELECT client_name, client_phone FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            if b and b["client_phone"]:  # a booking already gone from the calendar still keeps its tasks editable
                client_id = ensure_client(b["client_phone"], b["client_name"] or "")
        vals = (title, due,
                data.get("assignee_uid") if "assignee_uid" in data else ((cur and cur["assignee_uid"]) or uid),
                client_id,
                data.get("deal_id") if "deal_id" in data else (cur and cur["deal_id"]),
                status, (_now() if status == "done" else None) if (not cur or cur["status"] != status) else (cur and cur["done_at"]),
                booking_id)
        if tid:
            conn.execute("UPDATE crm_tasks SET title=?, due=?, assignee_uid=?, client_id=?, deal_id=?, status=?, done_at=?, booking_id=? WHERE id=?",
                         (*vals, tid))
        else:
            tid = conn.execute("INSERT INTO crm_tasks (title, due, assignee_uid, client_id, deal_id, status, done_at, booking_id, created_by, created_at) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?)", (*vals, uid, _now())).lastrowid
        r = conn.execute("SELECT * FROM crm_tasks WHERE id = ?", (tid,)).fetchone()
        cmap = _clients_map(conn, [r["client_id"]])
        bmap = _bookings_map(conn, [r["booking_id"]])
    return _task_out(r, user_names(), cmap, bmap)


def delete_task(tid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_tasks WHERE id = ?", (tid,))


def auto_tasks(day: date | None = None) -> int:
    """Check-in / check-out tasks for the day, once per booking, assigned to
    the first admin. Off with the «auto_tasks» setting."""
    if get_setting("auto_tasks", "0") != "1":
        return 0
    day = day or date.today()
    admins = [u for u in list_users() if u["role"] == "admin" and u["active"]]
    if not admins:
        return 0
    uid = admins[0]["id"]
    n = 0
    for b in database.get_bookings(day.isoformat(), day.isoformat()):
        if b.get("status") in ("cancelled", "canceled"):
            continue
        items = []
        if b["end_date"] == day.isoformat():
            items.append(("checkout", f"Выезд: принять квартиру, вернуть депозит — {b.get('client_name') or 'гость'} — {b['apartment_name']}",
                          f"{day.isoformat()}T{(b.get('departure_time') or default_times()[1])[:5]}"))
        if b["begin_date"] == day.isoformat():
            items.append(("checkin", f"Заезд: отправить адрес и код, встретить — {b.get('client_name') or 'гость'} — {b['apartment_name']}",
                          f"{day.isoformat()}T{(b.get('arrival_time') or default_times()[0])[:5]}"))
        cid = ensure_client(b.get("client_phone") or "", b.get("client_name") or "")
        with database.get_conn() as conn:
            for kind, title, due in items:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO crm_tasks (title, due, assignee_uid, client_id, status, created_by, created_at, src_key, booking_id) "
                    "VALUES (?, ?, ?, ?, 'open', 0, ?, ?, ?)", (title, due, uid, cid, _now(), f"{kind}:{b['id']}", b["id"]))
                n += cur.rowcount
    return n


# ---------------------------------------------------------------------------
# Booking checklist («Задачи по брони»): a template the owner edits himself.
# One line = one task:  Текст задачи | заезд -1 10:00
#   anchor: заезд / выезд / сегодня, then day offset (+1, -2, 0), then time.
# ---------------------------------------------------------------------------
OLD_CHECKLIST_V42 = """Подтвердить бронь и предоплату | сегодня 0 10:00
Отправить адрес, код домофона и правила | заезд -1 10:00
Проверить уборку и готовность квартиры | заезд 0 12:00
Встретить гостя / передать ключи | заезд 0 15:00
Напомнить о времени выезда | выезд -1 18:00
Принять квартиру, вернуть депозит | выезд 0 12:00
Попросить отзыв | выезд +1 11:00"""

DEFAULT_CHECKLIST = """# Одна строка = одна задача:  Текст | заезд -1 15:00 | текст сообщения гостю
# Точка отсчёта: заезд, выезд, сегодня или сразу. Если после второй | есть текст —
# это сообщение: в назначенное время CRM сама отправит его гостю в чат (WhatsApp /
# Telegram, привязанный к брони или найденный по номеру) и закроет задачу.
# Нет чата или отправка не удалась — задача остаётся менеджеру.
Подтвердить бронь гостю | сразу | {имя}, здравствуйте! Это Nova Home. Ваша бронь подтверждена: {объект}, заезд {заезд время}, выезд {выезд время}, {ночей} ночей. Если появятся вопросы — пишите сюда, мы на связи.
Узнать время приезда | заезд -1 15:00 | {имя}, добрый день! Завтра ждём вас в {объект}. Подскажите, во сколько примерно приедете? Заезд с {время заезда}, если нужно раньше — постараемся подстроиться.
Отправить инструкцию по заселению | заезд 0 14:00 | {имя}, инструкция по заселению в {объект}: адрес — ..., код домофона — ..., ключи — .... Wi-Fi: ... Если что-то не получается — звоните или пишите, поможем.
Принять паспорт | заезд 0
Принять оплату (остаток {долг} $) | заезд 0
Спросить, как прошло заселение | заезд 0 19:00 | {имя}, как прошло заселение? Всё ли в порядке в квартире? Если что-то нужно — напишите, решим.
Напомнить о выселении | выезд -1 20:00 | {имя}, напоминаем: завтра выезд до {время выезда}. Ключи оставьте ..., если нужно задержаться — напишите заранее, посмотрим, что можно сделать.
Поблагодарить и пожелать хорошей дороги | выезд 0 11:30 | {имя}, спасибо, что выбрали Nova Home! Хорошей дороги. Если что-то было не так — напишите нам, нам важно это знать.
Попросить отзыв | выезд +1 15:00 | {имя}, будем очень благодарны за отзыв о проживании на площадке, где вы бронировали, и в Google Картах: ... Это помогает нам становиться лучше. До новых встреч!"""

# ---- stages of a stay (v2): structured steps, RU + EN text, stored as JSON ----
# Real Nova Home data (Nest One): the texts below are the defaults the owner tunes
# in Интеграции → «Этапы заезда гостя». [в квадратных скобках] = fill in once.
DEFAULT_STEPS = [
    {"title": "Подтвердить бронь гостю", "anchor": "now", "offset": 0, "time": "", "enabled": True,
     "message": "{имя}, здравствуйте! Это Nova Home Apartments (Nest One, Ташкент). Ваша бронь подтверждена: {объект}, заезд {заезд время}, выезд {выезд время}, {ночей} ноч. Если появятся вопросы — пишите сюда, мы на связи 24/7.",
     "message_en": "Hello {имя}! This is Nova Home Apartments (Nest One, Tashkent). Your booking is confirmed: {объект}, check-in {заезд время}, check-out {выезд время}, {ночей} night(s). If you have any questions, just message us here — we are available 24/7."},
    {"title": "Узнать время приезда", "anchor": "checkin", "offset": -1, "time": "15:00", "enabled": True,
     "message": "{имя}, добрый день! Завтра ждём вас в {объект}. Подскажите, во сколько примерно приедете и откуда (самолёт, поезд, авто)? Заезд с {время заезда}; если нужно раньше — напишите, постараемся подстроиться. Нужен ли трансфер из аэропорта / с вокзала?",
     "message_en": "Hello {имя}! We are expecting you tomorrow at {объект}. What time will you arrive, and how (plane, train, car)? Check-in starts at {время заезда}; if you need an earlier check-in, let us know and we will try to arrange it. Do you need an airport / railway station transfer?"},
    {"title": "Отправить инструкцию по заселению", "anchor": "checkin", "offset": 0, "time": "10:00", "enabled": True,
     "message": "{имя}, инструкция по заселению в {объект}:\n📍 Nest One, Ташкент — {блок}.\n🅰️ Блок A: вход со стороны улицы Ислама Каримова, рядом с табличкой NOVA HOME.\n🅱️ Блок B: западное здание комплекса, вход со стороны улицы Янги Тошкент.\n🚪 Код домофона: {код домофона}.\n🔑 Ключи и карта доступа: {ключи}.\n📶 Wi-Fi: {wifi}.\n⏰ Заезд с {время заезда}. Если что-то не получается — звоните {телефон компании}, поможем.",
     "message_en": "{имя}, your check-in instructions for {объект}:\n📍 Nest One, Tashkent — {блок}.\n🅰️ Block A: entrance from Islam Karimov Street, next to the NOVA HOME sign.\n🅱️ Block B: western building of the complex, entrance from Yangi Toshkent Street.\n🚪 Door code: {код домофона}.\n🔑 Keys and access card: {ключи}.\n📶 Wi-Fi: {wifi}.\n⏰ Check-in from {время заезда}. If anything goes wrong, call {телефон компании} — we will help."},
    {"title": "Принять паспорт и заполнить данные гостя", "anchor": "checkin", "offset": 0, "time": "", "enabled": True, "message": "", "message_en": ""},
    {"title": "Принять оплату (остаток {долг} $)", "anchor": "checkin", "offset": 0, "time": "", "enabled": True, "message": "", "message_en": ""},
    {"title": "Спросить, как прошло заселение", "anchor": "checkin", "offset": 0, "time": "19:00", "enabled": True,
     "message": "{имя}, как прошло заселение? Всё ли в порядке в квартире? Напомню, что в Nest One для гостей: смотровая площадка на 48 этаже блока A (10:00–20:00), спортзал на 28 этаже блока A (6:00–20:00), парк на 2 этаже блока A — круглосуточно, вход по карте от лифта. Если что-то нужно — напишите, решим.",
     "message_en": "{имя}, how was your check-in? Is everything fine in the apartment? A reminder of the Nest One facilities: observation deck on the 48th floor of Block A (10 AM – 8 PM), gym on the 28th floor of Block A (6 AM – 8 PM), private park on the 2nd floor of Block A — open 24/7; use your elevator access card. If you need anything, just message us."},
    {"title": "Напомнить о выезде", "anchor": "checkout", "offset": -1, "time": "20:00", "enabled": True,
     "message": "{имя}, напоминаем: завтра выезд до {время выезда}. Ключи и карту доступа оставьте, пожалуйста, {ключи при выезде}. Если нужно задержаться — напишите заранее, посмотрим, что можно сделать. Нужен ли трансфер в аэропорт?",
     "message_en": "{имя}, a reminder: check-out tomorrow by {время выезда}. Please leave the keys and the access card: {ключи при выезде}. If you need a late check-out, let us know in advance and we will see what we can do. Do you need a transfer to the airport?"},
    {"title": "Проверить квартиру после выезда и вернуть депозит", "anchor": "checkout", "offset": 0, "time": "", "enabled": True, "message": "", "message_en": ""},
    {"title": "Поблагодарить и попросить отзыв", "anchor": "checkout", "offset": 0, "time": "13:00", "enabled": True,
     "message": "{имя}, спасибо, что выбрали Nova Home! Хорошей дороги. Будем очень благодарны за отзыв на площадке, где вы бронировали, и в Google Картах: {ссылка на отзыв}. Если что-то было не так — напишите нам, нам важно это знать. До новых встреч!",
     "message_en": "{имя}, thank you for staying with Nova Home! Have a safe trip. We would be very grateful for a review on the platform you booked with and on Google Maps: {ссылка на отзыв}. If anything was not right, please tell us — it matters to us. See you again!"},
]


def steps() -> list[dict]:
    """The stages of a stay as configured (JSON setting), else the legacy text
    template converted, else the defaults."""
    raw = get_setting("booking_steps", "")
    if raw:
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                return [_step_norm(x) for x in items if isinstance(x, dict) and (x.get("title") or "").strip()]
        except ValueError:
            pass
    legacy = get_setting("booking_checklist", "")
    if legacy and legacy.strip() != DEFAULT_CHECKLIST.strip() and legacy.strip() != OLD_CHECKLIST_V42.strip():
        return [_step_norm({**it, "message": it.get("message", "")}) for it in parse_checklist(legacy)]
    return [dict(x) for x in DEFAULT_STEPS]


def _step_norm(x: dict) -> dict:
    a = str(x.get("anchor") or "checkin").lower()
    anchor = "checkin" if a in ("заезд", "checkin") else "checkout" if a in ("выезд", "checkout") else "now" if a in ("сразу", "now") else "today" if a in ("сегодня", "today") else "checkin"
    t = str(x.get("time") or "").strip()
    if t and not re.match(r"^\d{1,2}:\d{2}$", t):
        t = ""
    try:
        off = int(x.get("offset") or 0)
    except (TypeError, ValueError):
        off = 0
    return {"title": str(x.get("title") or "").strip()[:200], "anchor": anchor, "offset": max(-30, min(30, off)), "time": t,
            "enabled": bool(x.get("enabled", True)), "message": str(x.get("message") or "").strip()[:3000], "message_en": str(x.get("message_en") or "").strip()[:3000]}


def save_steps(items: list) -> list[dict]:
    norm = [_step_norm(x) for x in items if isinstance(x, dict) and (x.get("title") or "").strip()]
    set_setting("booking_steps", json.dumps(norm, ensure_ascii=False))
    return norm


_CL_RE = re.compile(r"^(?P<title>.+?)\s*\|\s*(?P<anchor>заезд|выезд|сегодня|сразу|checkin|checkout|today|now)\s*(?P<off>[+-]?\d+)?\s*(?P<time>\d{1,2}:\d{2})?\s*(?:\|\s*(?P<msg>.+))?$", re.I)


def parse_checklist(text: str) -> list[dict]:
    items = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _CL_RE.match(line)
        if m:
            a = m.group("anchor").lower()
            anchor = "checkin" if a in ("заезд", "checkin") else "checkout" if a in ("выезд", "checkout") else "now" if a in ("сразу", "now") else "today"
            items.append({"title": m.group("title").strip(), "anchor": anchor, "offset": int(m.group("off") or 0),
                          "time": m.group("time") or "", "message": (m.group("msg") or "").strip()})
        else:
            parts = [x.strip() for x in line.split("|")]
            items.append({"title": parts[0], "anchor": "checkin", "offset": 0, "time": "", "message": parts[2] if len(parts) > 2 else ""})
    return items


def checklist_preview(text: str | None = None) -> list[dict]:
    if text is not None:
        return parse_checklist(text)
    return [x for x in steps() if x.get("enabled", True)]


def _cl_due(item: dict, b: dict) -> str:
    if item["anchor"] == "now":
        # «right after the booking appeared» counts from when we first saw the
        # booking, not from when the checklist happened to be created
        seen = (b.get("first_seen") or b.get("created_at") or "")[:16]
        try:
            base_dt = datetime.fromisoformat(seen) if seen else datetime.now()
        except ValueError:
            base_dt = datetime.now()
        return (base_dt + timedelta(days=int(item.get("offset") or 0))).strftime("%Y-%m-%dT%H:%M")
    base = date.today() if item["anchor"] == "today" else date.fromisoformat((b["end_date"] if item["anchor"] == "checkout" else b["begin_date"])[:10])
    d = base + timedelta(days=int(item.get("offset") or 0))
    ci, co = default_times()
    t = _hm(item.get("time")) if item.get("time") else _hm((b.get("departure_time") if item["anchor"] == "checkout" else b.get("arrival_time")),
                                                             co if item["anchor"] == "checkout" else ci)
    return f"{d.isoformat()}T{t}"


def create_checklist(bid: int, uid: int, assignee_uid: int | None = None, force: bool = False) -> dict:
    """Create the template tasks for a booking (once; existing items are kept,
    missing ones are added when called again by hand)."""
    items = checklist_preview()
    with database.get_conn() as conn:
        b = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
        if not b:
            raise ValueError("Бронь не найдена")
        b = dict(b)
        if b.get("status") in ("cancelled", "canceled"):
            raise ValueError("Бронь отменена")
        done = conn.execute("SELECT 1 FROM crm_booking_checklist WHERE booking_id = ?", (bid,)).fetchone()
        if done and not force:
            return {"created": 0, "already": True}
        cid = ensure_client(b.get("client_phone") or "", b.get("client_name") or "") if b.get("client_phone") else None
        n = 0
        from . import crm_ext
        bo = _booking_out(b)
        stale = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
        for it in items:
            key = f"cl:{bid}:{hashlib.sha1(it['title'].lower().encode()).hexdigest()[:10]}"
            title = crm_ext.fill(it["title"], bo, None)
            due = _cl_due(it, b)
            # a «right away» step for a booking we have known for more than a
            # day is history, not a message to send now (import / re-enable)
            old_now = it["anchor"] == "now" and due < stale
            cur = conn.execute(
                "INSERT OR IGNORE INTO crm_tasks (title, due, assignee_uid, client_id, status, created_by, created_at, src_key, booking_id, auto_text, auto_text_en, done_at, auto_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, due, assignee_uid or uid, cid, "done" if old_now else "open", uid, _now(), key, bid, it.get("message") or None, it.get("message_en") or None,
                 _now() if old_now else None, "skipped: бронь старше суток" if old_now else None))
            n += cur.rowcount
        conn.execute("INSERT OR REPLACE INTO crm_booking_checklist (booking_id, created_by, created_at, n) VALUES (?, ?, ?, ?)",
                     (bid, uid, _now(), n))
    return {"created": n, "already": False}


def auto_checklists(days_ahead: int = 30) -> int:
    """Setting «auto_checklist»: every new booking (check-in within N days)
    gets the checklist automatically, assigned to the first admin."""
    if get_setting("auto_checklist", "1") != "1":
        return 0
    admins = [u for u in list_users() if u["role"] == "admin" and u["active"]]
    if not admins:
        return 0
    today = date.today()
    n = 0
    with database.get_conn() as conn:
        done = {r[0] for r in conn.execute("SELECT booking_id FROM crm_booking_checklist").fetchall()}
    for b in database.get_bookings(today.isoformat(), (today + timedelta(days=days_ahead)).isoformat()):
        if b["id"] in done or b.get("status") in ("cancelled", "canceled") or b["begin_date"] < today.isoformat():
            continue
        try:
            n += create_checklist(b["id"], 0, admins[0]["id"])["created"]
        except ValueError:
            continue
    return n


# ---------------------------------------------------------------------------
# Bookings (mirrored from RealtyCalendar)
# ---------------------------------------------------------------------------
def _dm(s):
    return f"{s[8:10]}.{s[5:7]}" if s else ""


def _booking_out(b: dict) -> dict:
    return {
        "id": b["id"], "apartment": b.get("apartment_name"), "checkin": b.get("begin_date"), "checkout": b.get("end_date"),
        "nights": b.get("days_count"), "guest": b.get("client_name"), "phone": digits(b.get("client_phone")),
        "amount": b.get("amount"), "debt": (round(float(b.get("amount") or 0) - _paid_amount(b), 2) if b.get("amount") else b.get("debt")),
        "source": config.SOURCE_NAMES.get(b.get("source_id"), "Другое"),
        "status": b.get("status"), "notes": b.get("short_notes"), "arrival_time": b.get("arrival_time"),
        "departure_time": b.get("departure_time"), "prepayment": b.get("prepayment"),
        "pay": _pay_state(b), "paid": _paid_amount(b),
    }


def _paid_amount(b: dict) -> float:
    """How much the guest has actually paid, from what RealtyCalendar gives us.
    Only positive evidence counts: payments (prepayment), a debt smaller than
    the amount, the % progress, or the «paid» status. A booking with amount and
    no payment data is unpaid, not paid."""
    amount = float(b.get("amount") or 0)
    debt = float(b.get("debt") or 0)
    prep = float(b.get("prepayment") or 0)
    prog = float(b.get("prepayment_progress") or 0)
    st = (b.get("status") or "").lower()
    paid = 0.0
    if prep > 0:
        paid = prep
    elif 0 < debt < amount:
        paid = amount - debt
    elif prog > 0 and amount:
        paid = amount * min(prog, 100) / 100
    elif st == "paid":
        paid = amount
    return round(min(paid, amount) if amount else paid, 2)


def _pay_state(b: dict) -> str:
    """Payment colour like the calendar: paid / prepaid / unpaid / unconfirmed."""
    st = (b.get("status") or "").lower()
    amount = float(b.get("amount") or 0)
    if st == "not_confirmed":
        return "unconfirmed"
    paid = _paid_amount(b)
    if amount > 0:
        if paid >= amount - 0.005:
            return "paid"
        return "prepaid" if paid > 0 else "unpaid"
    return {"paid": "paid", "prepaid": "prepaid"}.get(st, "unpaid")


def _contact_states(rows: list[dict]) -> None:
    """Has anyone talked to the guest? contact = contacted (we wrote) /
    incoming (guest wrote, nobody answered) / none (no chat at all)."""
    with database.get_conn() as conn:
        chats = {}
        for r in conn.execute("SELECT c.id, c.phone, "
                              "EXISTS(SELECT 1 FROM inbox_messages m WHERE m.chat_id = c.id AND m.direction = 'out') AS out_ "
                              "FROM inbox_chats c").fetchall():
            chats[r["id"]] = (r["phone"] or "", bool(r["out_"]))
        pinned = {}
        for r in conn.execute("SELECT booking_id, chat_id FROM crm_booking_chats").fetchall():
            pinned.setdefault(r["booking_id"], []).append(r["chat_id"])
    by_phone: dict[str, list] = {}
    for cid, (phone, out) in chats.items():
        if len(phone) >= 7:
            by_phone.setdefault(phone[-9:], []).append(out)
    for b in rows:
        outs = [chats[c][1] for c in pinned.get(b["id"], []) if c in chats]
        if b.get("phone") and len(b["phone"]) >= 7:
            outs += by_phone.get(b["phone"][-9:], [])
        b["contact"] = "none" if not outs else ("contacted" if any(outs) else "incoming")


RU_MONTHS_GEN = util.RU_MONTHS_GEN


# ---- time of arrival the guest told us (chat / card) — a light note, not calendar data ----
_eta_cache: dict = {"at": 0.0, "map": {}}


def eta_map(force: bool = False) -> dict:
    """{booking_id: {"eta","note","set_by","set_at"}} — cached 20 s (the chat list asks often)."""
    if force or time.time() - _eta_cache["at"] > 20:
        with database.get_conn() as conn:
            _eta_cache["map"] = {r["booking_id"]: dict(r) for r in conn.execute("SELECT * FROM crm_eta").fetchall()}
        _eta_cache["at"] = time.time()
    return _eta_cache["map"]


def set_eta(bid: int, eta: str, note: str, who: str) -> dict | None:
    eta = (eta or "").strip()[:20]
    m = re.match(r"^(\d{1,2})[:.](\d{2})$", eta)
    if m:
        eta = f"{int(m.group(1)):02d}:{m.group(2)}"
    old = eta_map(force=True).get(bid)
    with database.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM bookings WHERE id = ?", (bid,)).fetchone():
            raise ValueError("Бронь не найдена")
        if not eta and not (note or "").strip():
            conn.execute("DELETE FROM crm_eta WHERE booking_id = ?", (bid,))
        else:
            conn.execute("INSERT OR REPLACE INTO crm_eta (booking_id, eta, note, set_by, set_at) VALUES (?, ?, ?, ?, ?)",
                         (bid, eta, (note or "").strip()[:120], who, _now()))
    new = eta_map(force=True).get(bid)
    from . import bg
    bg.run(refresh_arrivals_report, bid, old, new, who)
    return new


def arrivals_report(day: str, d: dict | None = None) -> dict:
    """The morning list for the housekeepers: who arrives when (ETA from the chat
    first, then the calendar's arrival time, else the house default with «?»),
    plus departures and tight turnovers. Returned as rows + ready-to-send text.
    `d` = an already computed bookings_day(day) (the home page has it)."""
    d = d or bookings_day(day)
    etas = eta_map(force=True)
    dci, dco = default_times()
    rows = []
    for b in d["arrivals"]:
        e = etas.get(b["id"]) or {}
        if e.get("eta"):
            t, src = e["eta"], "chat"
        elif b.get("arrival_time"):
            t, src = b["arrival_time"][:5], "calendar"
        else:
            t, src = dci, "default"
        dep = next((x for x in d["departures"] if x["apartment"] == b["apartment"]), None)
        rows.append({"booking_id": b["id"], "apartment": b["apartment"], "guest": b["guest"], "nights": b.get("nights"), "eta": t, "eta_src": src,
                     "note": e.get("note") or "", "turnover": bool(dep), "departure_time": (dep.get("departure_time") or dco)[:5] if dep else None,
                     "source": b.get("source"), "phone": b.get("phone")})
    rows.sort(key=lambda r: (r["eta"] if re.match(r"^\d{2}:\d{2}$", r["eta"]) else "99", r["apartment"] or ""))
    dt = date.fromisoformat(day)
    human = f"{dt.day} {RU_MONTHS_GEN[dt.month - 1]}"
    lines = [f"🔑 Заезды {human} — {len(rows)}"]
    for i, r in enumerate(rows, 1):
        t = r["eta"] + ("" if r["eta_src"] == "chat" else " (по брони)" if r["eta_src"] == "calendar" else " (не уточнено)")
        extra = f" — {r['note']}" if r["note"] else ""
        turn = f" · выезд {r['departure_time']}, уборка между" if r["turnover"] else ""
        lines.append(f"{i}. {r['apartment']} — {r['guest'] or 'Гость'} · {t}{extra}{turn}")
    if not rows:
        lines.append("Заездов нет.")
    deps = sorted(d["departures"], key=lambda b: ((b.get("departure_time") or dco)[:5], b["apartment"] or ""))
    if deps:
        lines += ["", f"🚪 Выезды — {len(deps)}"]
        lines += [f"• {b['apartment']} — {b['guest'] or 'Гость'} · до {(b.get('departure_time') or dco)[:5]}" for b in deps]
    unknown = [r for r in rows if r["eta_src"] == "default"]
    if unknown:
        lines += ["", "❓ Не уточнено (ожидаем к 14:00): " + ", ".join(r["apartment"] for r in unknown)]
    stamp = get_setting("report_last_sent", "")
    if stamp.startswith(day) and "|" in stamp:
        lines += ["", f"Обновлено {_now()[11:16]}"]
    return {"date": day, "rows": rows, "departures": [{"booking_id": b["id"], "apartment": b["apartment"], "guest": b["guest"], "time": (b.get("departure_time") or dco)[:5]} for b in deps],
            "text": "\n".join(lines), "unknown": len(unknown)}


def send_arrivals_report(day: str, who: str = "") -> dict:
    """Post the list to the work chats (Telegram: setting report_chat_ids, else the
    usual notify targets, «cleaning» topic in forum groups)."""
    from . import config as _cfg, notify
    rep = arrivals_report(day)
    ids = [int(x) for x in re.findall(r"-?\d+", get_setting("report_chat_ids", ""))] or _cfg.notify_targets()
    if not _cfg.BOT_TOKEN:
        raise ValueError("Telegram-бот не настроен (BOT_TOKEN)")
    if not ids:
        raise ValueError("Не указаны рабочие чаты: Интеграции → Отчёт о заездах")
    # THE list message of the day: later changes edit it in place (see refresh_arrivals_report)
    notify.send_replacing(f"arrivals:{day}", rep["text"], targets=ids, topic="cleaning")
    set_setting("report_last_sent", f"{day}|{_now()}")
    return {"ok": True, "chats": len(ids), "text": rep["text"]}


def refresh_arrivals_report(bid: int, old: dict | None, new: dict | None, who: str) -> None:
    """An arrival time changed after the morning list went out: edit the list
    message and post a one-line notice so the team notices (edits are silent)."""
    from . import config as _cfg, notify
    try:
        with database.get_conn() as conn:
            b = conn.execute("SELECT begin_date, apartment_name, client_name FROM bookings WHERE id = ?", (bid,)).fetchone()
        if not b:
            return
        day = b["begin_date"]
        if not get_setting("report_last_sent", "").startswith(day) or not _cfg.BOT_TOKEN:
            return  # the list for that day was not sent yet — the morning run will carry the time
        ids = [int(x) for x in re.findall(r"-?\d+", get_setting("report_chat_ids", ""))] or _cfg.notify_targets()
        if not ids:
            return
        rep = arrivals_report(day)
        notify.send_replacing(f"arrivals:{day}", rep["text"], targets=ids, topic="cleaning")
        was = (old or {}).get("eta") or "14:00 (не уточнено)"
        now = (new or {}).get("eta") or "14:00 (не уточнено)"
        if was != now or ((old or {}).get("note") or "") != ((new or {}).get("note") or ""):
            note = f" — {new['note']}" if new and new.get("note") else ""
            notify.send(f"🔄 {b['apartment_name']} — {b['client_name'] or 'Гость'}: приедет в {now}{note} (было {was}). Список выше обновлён.",
                        targets=ids, topic="cleaning")
    except Exception:  # noqa: BLE001
        logger.exception("arrivals report refresh failed")


def bookings_day(day: str) -> dict:
    rows = [_booking_out(b) for b in database.get_bookings(day, day)]
    with database.get_conn() as conn:
        deals_map = {r["booking_id"]: r["id"] for r in conn.execute(
            "SELECT id, booking_id FROM crm_deals WHERE booking_id IS NOT NULL").fetchall()}
        cm = {}
        for r in conn.execute("SELECT id, phone FROM crm_clients WHERE phone != ''").fetchall():
            cm[r["phone"][-9:]] = r["id"]
        tc = task_counts(conn, [b["id"] for b in rows])
    for b in rows:
        b["deal_id"] = deals_map.get(b["id"])
        b["client_id"] = cm.get((b["phone"] or "")[-9:]) if b["phone"] else None
        b["tasks"] = tc.get(b["id"])
    _contact_states(rows)
    dci, dco = default_times()
    t_in = lambda b: _hm(b.get("arrival_time"), dci)  # noqa: E731
    t_out = lambda b: _hm(b.get("departure_time"), dco)  # noqa: E731
    arrivals = sorted([b for b in rows if b["checkin"] == day], key=lambda b: (t_in(b), b["apartment"] or ""))
    departures = sorted([b for b in rows if b["checkout"] == day], key=lambda b: (t_out(b), b["apartment"] or ""))
    # preparation plan: which apartments to get ready first — by arrival time, with the
    # same-day departure (turnover) that must be cleaned before the guest comes
    out_by_apt = {b["apartment"]: b for b in departures}
    plan = []
    for b in arrivals:
        dep = out_by_apt.get(b["apartment"])
        gap = None
        if dep:
            h1, m1 = map(int, t_out(dep).split(":")[:2])
            h2, m2 = map(int, t_in(b).split(":")[:2])
            gap = (h2 * 60 + m2) - (h1 * 60 + m1)
        e = eta_map().get(b["id"]) or {}
        plan.append({"booking_id": b["id"], "apartment": b["apartment"], "arrival_time": e.get("eta") or t_in(b), "time_set": bool(e.get("eta") or b.get("arrival_time")),
                     "eta": e.get("eta") or "", "eta_note": e.get("note") or "",
                     "guest": b["guest"], "nights": b.get("nights"), "turnover": bool(dep), "departure_time": t_out(dep) if dep else None,
                     "gap_min": gap, "tight": gap is not None and gap < 180, "pay": b.get("pay"), "contact": b.get("contact")})
    return {
        "date": day,
        "arrivals": arrivals,
        "departures": departures,
        "staying": [b for b in rows if b["checkin"] < day < b["checkout"]],
        "plan": plan,
    }


def bookings_grid(start: str, days: int = 30) -> dict:
    from . import services
    d0 = date.fromisoformat(start)
    d1 = d0 + timedelta(days=days)
    rows = [_booking_out(b) for b in database.get_bookings(d0.isoformat(), d1.isoformat())]
    with database.get_conn() as conn:
        deals_map = {r["booking_id"]: r["id"] for r in conn.execute(
            "SELECT id, booking_id FROM crm_deals WHERE booking_id IS NOT NULL").fetchall()}
        tc = task_counts(conn, [b["id"] for b in rows])
    for b in rows:
        b["deal_id"] = deals_map.get(b["id"])
        b["tasks"] = tc.get(b["id"])
    _contact_states(rows)
    apts = services.apartment_names()
    return {"start": d0.isoformat(), "days": days, "apartments": apts, "bookings": rows}


# ---------------------------------------------------------------------------
# Quick commands (inbox templates with /command + variables)
# ---------------------------------------------------------------------------
def commands() -> list[dict]:
    with database.get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM inbox_templates ORDER BY CASE WHEN command IS NULL OR command = '' THEN 1 ELSE 0 END, command, title").fetchall()]


def save_command(tid: int | None, command: str, title: str, text: str) -> dict:
    command = re.sub(r"[^\w\-]", "", command.strip().lstrip("/").lower())[:30]
    text = text.strip()
    if not text:
        raise ValueError("Текст команды")
    title = title.strip() or (("/" + command) if command else text[:40])
    with database.get_conn() as conn:
        if command and conn.execute("SELECT 1 FROM inbox_templates WHERE command = ? AND id != ?", (command, tid or 0)).fetchone():
            raise ValueError(f"Команда /{command} уже есть")
        if tid:
            conn.execute("UPDATE inbox_templates SET command = ?, title = ?, text = ? WHERE id = ?", (command or None, title, text, tid))
        else:
            tid = conn.execute("INSERT INTO inbox_templates (command, title, text, created_at) VALUES (?, ?, ?, ?)",
                               (command or None, title, text, _now())).lastrowid
        return dict(conn.execute("SELECT * FROM inbox_templates WHERE id = ?", (tid,)).fetchone())


def delete_command(tid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM inbox_templates WHERE id = ?", (tid,))


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------
def home(uid: int) -> dict:
    today = date.today().isoformat()
    day = bookings_day(today)
    with database.get_conn() as conn:
        unread = conn.execute("SELECT COUNT(*) FROM inbox_chats WHERE unread > 0").fetchone()[0]
        unread_msgs = conn.execute("SELECT COALESCE(SUM(unread), 0) FROM inbox_chats").fetchone()[0]
        my_open = conn.execute(f"SELECT COUNT(*) FROM crm_tasks WHERE status = 'open' AND {MANUAL_ONLY} AND assignee_uid = ? AND (due IS NULL OR due <= ?)",
                               (uid, _view_until("today"))).fetchone()[0]
        overdue = conn.execute(f"SELECT COUNT(*) FROM crm_tasks WHERE status = 'open' AND {MANUAL_ONLY} AND due < ?", (_now(),)).fetchone()[0]
        by_stage = [dict(r) for r in conn.execute(
            "SELECT s.id, s.name, s.color, s.kind, p.name AS pipeline, COUNT(d.id) AS n, COALESCE(SUM(d.amount), 0) AS amount "
            "FROM crm_stages s JOIN crm_pipelines p ON p.id = s.pipeline_id "
            "LEFT JOIN crm_deals d ON d.stage_id = s.id GROUP BY s.id ORDER BY p.sort, s.sort").fetchall()]
        clients_n = conn.execute("SELECT COUNT(*) FROM crm_clients").fetchone()[0]
        recent_chats = [dict(r) for r in conn.execute(
            "SELECT id, name, push_name, phone, last_text, last_at, unread FROM inbox_chats ORDER BY last_at DESC LIMIT 8").fetchall()]
    return {
        "date": today, "arrivals": len(day["arrivals"]), "departures": len(day["departures"]), "staying": len(day["staying"]),
        "report": arrivals_report(today, day), "report_last_sent": get_setting("report_last_sent", ""),
        "unread": unread, "need_reply": unread, "unread_msgs": unread_msgs, "my_open_tasks": my_open, "overdue_tasks": overdue, "clients": clients_n,
        # the morning questions: who has not paid, whom we never reached
        "arrivals_unpaid": [{"id": b["id"], "apartment": b["apartment"], "guest": b["guest"], "amount": b["amount"], "paid": b.get("paid"),
                             "debt": b.get("debt"), "pay": b.get("pay")} for b in day["arrivals"] if b.get("pay") in ("unpaid", "prepaid", "unconfirmed")],
        "arrivals_nocontact": [{"id": b["id"], "apartment": b["apartment"], "guest": b["guest"]} for b in day["arrivals"] if b.get("contact") == "none"],
        "features": config.features(),
        "stages": by_stage, "recent_chats": recent_chats, "last_sync": database.last_sync(),
        "today_tasks": tasks(status="open", view="today", limit=10),
    }
