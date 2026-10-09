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

from . import config, database

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
    src_key TEXT UNIQUE              -- auto tasks (checkout:<booking id>) are created once
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


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(inbox_templates)").fetchall()]
        if cols and "command" not in cols:
            conn.execute("ALTER TABLE inbox_templates ADD COLUMN command TEXT")
        if "entity" not in [r[1] for r in conn.execute("PRAGMA table_info(crm_fields)").fetchall()]:
            conn.execute("ALTER TABLE crm_fields ADD COLUMN entity TEXT DEFAULT 'client'")
        if "fields" not in [r[1] for r in conn.execute("PRAGMA table_info(crm_deals)").fetchall()]:
            conn.execute("ALTER TABLE crm_deals ADD COLUMN fields TEXT")
        if not conn.execute("SELECT 1 FROM crm_pipelines").fetchone():
            pid = conn.execute("INSERT INTO crm_pipelines (name, sort) VALUES ('Продажи', 0)").lastrowid
            for i, (nm, color, kind) in enumerate(DEFAULT_STAGES):
                conn.execute("INSERT INTO crm_stages (pipeline_id, name, sort, color, kind) VALUES (?, ?, ?, ?, ?)",
                             (pid, nm, i, color, kind))
        if cols is not None and conn.execute("SELECT COUNT(*) FROM inbox_templates").fetchone()[0] == 0:
            for cmd, title, text in DEFAULT_COMMANDS:
                conn.execute("INSERT INTO inbox_templates (title, text, created_at, command) VALUES (?, ?, ?, ?)",
                             (title, text, _now(), cmd or None))
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
    return ("crm:" + (config.INBOX_SECRET or "dev")).encode()


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
def get_setting(key: str, default: str = "") -> str:
    with database.get_conn() as conn:
        r = conn.execute("SELECT value FROM crm_settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str) -> None:
    with database.get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO crm_settings (key, value) VALUES (?, ?)", (key, value))


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
def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _client_out(r) -> dict:
    c = dict(r)
    try:
        c["fields"] = json.loads(c["fields"] or "{}")
    except ValueError:
        c["fields"] = {}
    return c


def _phone_match_sql(phone: str) -> tuple[str, tuple]:
    # the last 9 digits identify a number whatever the country-code spelling
    return "phone LIKE ?", ("%" + phone[-9:],)


def clients(q: str = "", limit: int = 500) -> list[dict]:
    with database.get_conn() as conn:
        rows = [_client_out(r) for r in conn.execute(
            "SELECT * FROM crm_clients ORDER BY updated_at DESC, id DESC LIMIT ?", (5000 if q else limit,)).fetchall()]
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
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM bookings WHERE COALESCE(is_delete, 0) = 0 ORDER BY begin_date DESC").fetchall()
    out = []
    for r in rows:
        if phone and len(phone) >= 7 and digits(r["client_phone"]).endswith(phone[-9:]):
            out.append(_booking_out(dict(r)))
        elif not phone and name and (r["client_name"] or "").strip().lower() == name.strip().lower():
            out.append(_booking_out(dict(r)))
    return out[:50]


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
            conn.execute(
                "UPDATE crm_clients SET name = ?, phone = ?, email = ?, source = ?, notes = ?, fields = ?, updated_at = ? WHERE id = ?",
                (name, phone, (data.get("email") or "").strip(), (data.get("source") or "").strip(),
                 data.get("notes") or "", json.dumps(fld, ensure_ascii=False), _now(), cid))
        else:
            if phone:
                dup = conn.execute("SELECT id FROM crm_clients WHERE phone LIKE ?", ("%" + phone[-9:],)).fetchone()
                if dup:
                    raise ValueError(f"Клиент с этим номером уже есть (#{dup['id']})")
            cid = conn.execute(
                "INSERT INTO crm_clients (name, phone, email, source, notes, fields, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name or ("+" + phone), phone, (data.get("email") or "").strip(), (data.get("source") or "").strip(),
                 data.get("notes") or "", json.dumps(fld, ensure_ascii=False), _now(), _now())).lastrowid
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
    before = len(clients(limit=100000))
    for r in bk:
        ensure_client(r["client_phone"], r["client_name"] or "", config.SOURCE_NAMES.get(r["source_id"], "") or "")
    for r in ch:
        src = {"ig": "Instagram", "tg": "Telegram", "tgbot": "Telegram"}.get(r["channel"] or "wa", "WhatsApp")
        if r["phone"]:
            ensure_client(r["phone"], r["name"] or r["push_name"] or "", src, r["id"])
        else:
            ensure_client_for_chat(r["id"], r["name"] or r["push_name"] or "", src)
    n = len(clients(limit=100000)) - before
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
        cmap = {r["id"]: dict(r) for r in conn.execute("SELECT id, name, phone FROM crm_clients").fetchall()}
    names = user_names()
    out = [_deal_out(r, names, cmap) for r in rows]
    if q:
        ql, qd = q.lower(), digits(q)
        out = [d for d in out if ql in (d["title"] or "").lower() or ql in (d.get("client_name") or "").lower()
               or ql in (d["apartment"] or "").lower() or (qd and qd in (d.get("client_phone") or ""))]
    return out


def get_deal(did: int) -> dict | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM crm_deals WHERE id = ?", (did,)).fetchone()
        cmap = {r2["id"]: dict(r2) for r2 in conn.execute("SELECT id, name, phone FROM crm_clients").fetchall()}
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


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def delete_deal(did: int) -> None:
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_tasks SET deal_id = NULL WHERE deal_id = ?", (did,))
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
def _task_out(r, names, cmap) -> dict:
    t = dict(r)
    t["assignee_name"] = names.get(t["assignee_uid"])
    c = cmap.get(t["client_id"]) if t["client_id"] else None
    t["client_name"] = c["name"] if c else None
    return t


def tasks(assignee_uid: int | None = None, status: str | None = None, client_id: int | None = None,
          deal_id: int | None = None) -> list[dict]:
    where, args = [], []
    if assignee_uid:
        where.append("assignee_uid = ?"); args.append(assignee_uid)
    if status in ("open", "done"):
        where.append("status = ?"); args.append(status)
    if client_id:
        where.append("client_id = ?"); args.append(client_id)
    if deal_id:
        where.append("deal_id = ?"); args.append(deal_id)
    sql = "SELECT * FROM crm_tasks" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, COALESCE(due, '9999') , id DESC LIMIT 500"
    with database.get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
        cmap = {r["id"]: dict(r) for r in conn.execute("SELECT id, name FROM crm_clients").fetchall()}
    names = user_names()
    return [_task_out(r, names, cmap) for r in rows]


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
        vals = (title, due,
                data.get("assignee_uid") if "assignee_uid" in data else ((cur and cur["assignee_uid"]) or uid),
                data.get("client_id") if "client_id" in data else (cur and cur["client_id"]),
                data.get("deal_id") if "deal_id" in data else (cur and cur["deal_id"]),
                status, (_now() if status == "done" else None) if (not cur or cur["status"] != status) else (cur and cur["done_at"]))
        if tid:
            conn.execute("UPDATE crm_tasks SET title=?, due=?, assignee_uid=?, client_id=?, deal_id=?, status=?, done_at=? WHERE id=?",
                         (*vals, tid))
        else:
            tid = conn.execute("INSERT INTO crm_tasks (title, due, assignee_uid, client_id, deal_id, status, done_at, created_by, created_at) "
                               "VALUES (?,?,?,?,?,?,?,?,?)", (*vals, uid, _now())).lastrowid
        r = conn.execute("SELECT * FROM crm_tasks WHERE id = ?", (tid,)).fetchone()
        cmap = {x["id"]: dict(x) for x in conn.execute("SELECT id, name FROM crm_clients").fetchall()}
    return _task_out(r, user_names(), cmap)


def delete_task(tid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_tasks WHERE id = ?", (tid,))


def auto_tasks(day: date | None = None) -> int:
    """Check-in / check-out tasks for the day, once per booking, assigned to
    the first admin. Off with the «auto_tasks» setting."""
    if get_setting("auto_tasks", "1") != "1":
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
                          f"{day.isoformat()}T{(b.get('departure_time') or '12:00')[:5]}"))
        if b["begin_date"] == day.isoformat():
            items.append(("checkin", f"Заезд: отправить адрес и код, встретить — {b.get('client_name') or 'гость'} — {b['apartment_name']}",
                          f"{day.isoformat()}T{(b.get('arrival_time') or '15:00')[:5]}"))
        cid = ensure_client(b.get("client_phone") or "", b.get("client_name") or "")
        with database.get_conn() as conn:
            for kind, title, due in items:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO crm_tasks (title, due, assignee_uid, client_id, status, created_by, created_at, src_key) "
                    "VALUES (?, ?, ?, ?, 'open', 0, ?, ?)", (title, due, uid, cid, _now(), f"{kind}:{b['id']}"))
                n += cur.rowcount
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
        "amount": b.get("amount"), "debt": b.get("debt"), "source": config.SOURCE_NAMES.get(b.get("source_id"), "Другое"),
        "status": b.get("status"), "notes": b.get("short_notes"), "arrival_time": b.get("arrival_time"),
        "departure_time": b.get("departure_time"),
    }


def bookings_day(day: str) -> dict:
    rows = [_booking_out(b) for b in database.get_bookings(day, day)]
    with database.get_conn() as conn:
        deals_map = {r["booking_id"]: r["id"] for r in conn.execute(
            "SELECT id, booking_id FROM crm_deals WHERE booking_id IS NOT NULL").fetchall()}
        cm = {}
        for r in conn.execute("SELECT id, phone FROM crm_clients WHERE phone != ''").fetchall():
            cm[r["phone"][-9:]] = r["id"]
    for b in rows:
        b["deal_id"] = deals_map.get(b["id"])
        b["client_id"] = cm.get((b["phone"] or "")[-9:]) if b["phone"] else None
    return {
        "date": day,
        "arrivals": [b for b in rows if b["checkin"] == day],
        "departures": [b for b in rows if b["checkout"] == day],
        "staying": [b for b in rows if b["checkin"] < day < b["checkout"]],
    }


def bookings_grid(start: str, days: int = 30) -> dict:
    from . import services
    d0 = date.fromisoformat(start)
    d1 = d0 + timedelta(days=days)
    rows = [_booking_out(b) for b in database.get_bookings(d0.isoformat(), d1.isoformat())]
    with database.get_conn() as conn:
        deals_map = {r["booking_id"]: r["id"] for r in conn.execute(
            "SELECT id, booking_id FROM crm_deals WHERE booking_id IS NOT NULL").fetchall()}
    for b in rows:
        b["deal_id"] = deals_map.get(b["id"])
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
        unread = conn.execute("SELECT COALESCE(SUM(unread), 0) FROM inbox_chats").fetchone()[0]
        my_open = conn.execute("SELECT COUNT(*) FROM crm_tasks WHERE status = 'open' AND assignee_uid = ?", (uid,)).fetchone()[0]
        overdue = conn.execute("SELECT COUNT(*) FROM crm_tasks WHERE status = 'open' AND due < ?", (_now(),)).fetchone()[0]
        by_stage = [dict(r) for r in conn.execute(
            "SELECT s.id, s.name, s.color, s.kind, p.name AS pipeline, COUNT(d.id) AS n, COALESCE(SUM(d.amount), 0) AS amount "
            "FROM crm_stages s JOIN crm_pipelines p ON p.id = s.pipeline_id "
            "LEFT JOIN crm_deals d ON d.stage_id = s.id ORDER BY p.sort, s.sort").fetchall()]
        clients_n = conn.execute("SELECT COUNT(*) FROM crm_clients").fetchone()[0]
        recent_chats = [dict(r) for r in conn.execute(
            "SELECT id, name, push_name, phone, last_text, last_at, unread FROM inbox_chats ORDER BY last_at DESC LIMIT 8").fetchall()]
    return {
        "date": today, "arrivals": len(day["arrivals"]), "departures": len(day["departures"]), "staying": len(day["staying"]),
        "unread": unread, "my_open_tasks": my_open, "overdue_tasks": overdue, "clients": clients_n,
        "stages": by_stage, "recent_chats": recent_chats, "last_sync": database.last_sync(),
        "today_tasks": [t for t in tasks(status="open") if (t["due"] or "")[:10] <= today][:10],
    }
