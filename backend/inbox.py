"""«Чаты» — the shared WhatsApp inbox (our own Wazzup).

The WhatsApp session itself lives in ``wa-bridge/`` (Node.js, QR login). The
bridge posts every message to ``/api/inbox/hook``; this module stores chats and
messages in the dashboard's SQLite DB, links a chat to the guest's booking by
phone number, alerts the team in Telegram and sends replies back through the
bridge.

Every change bumps a global revision number (``rev``): the inbox page polls
``/api/inbox/poll?since=<rev>`` and gets only what changed — new messages,
delivery ticks, unread counters, assignments.

Access: owners, and the employees allowed by INBOX_USERS (empty = every active
employee registered in the bot). Each person opens the inbox with a personal
link from the bot (/chats), so every reply is signed with their name.
"""
import hashlib
import hmac
import logging
import re
import threading
import time
import uuid
from datetime import date, datetime

import requests

from . import auth, config, database

logger = logging.getLogger("nova.inbox")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT DEFAULT 'wa',
    jid TEXT UNIQUE,
    lid TEXT,
    phone TEXT,
    name TEXT,              -- saved contact name / name set by an operator
    push_name TEXT,         -- the name the guest set in WhatsApp
    last_at TEXT,
    last_text TEXT,
    last_dir TEXT,
    last_status TEXT,
    unread INTEGER DEFAULT 0,
    assignee TEXT,
    created_at TEXT,
    rev INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inbox_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    wa_id TEXT UNIQUE,
    direction TEXT,         -- in / out
    author TEXT,            -- operator name for out; empty for the guest
    kind TEXT,              -- text image video audio document sticker location contact other
    text TEXT,
    media TEXT,             -- file name in INBOX_MEDIA_DIR
    mime TEXT,
    file_name TEXT,
    voice INTEGER DEFAULT 0,
    lat REAL,
    lng REAL,
    quoted_wa_id TEXT,
    reaction TEXT,
    status TEXT,            -- pending sent delivered read failed (out only)
    error TEXT,
    at TEXT,
    rev INTEGER DEFAULT 0
);

-- quick replies («/» in the composer)
CREATE TABLE IF NOT EXISTS inbox_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    text TEXT NOT NULL,
    created_at TEXT
);

-- who works in the inbox: names for the "who answered" label
CREATE TABLE IF NOT EXISTS inbox_agents (
    uid INTEGER PRIMARY KEY,
    name TEXT,
    username TEXT,
    seen_at TEXT
);

-- Telegram alert -> chat, so a reply to the alert goes to the guest
CREATE TABLE IF NOT EXISTS inbox_tg_map (
    tg_chat_id INTEGER,
    tg_msg_id INTEGER,
    chat_id INTEGER,
    created_at TEXT,
    PRIMARY KEY (tg_chat_id, tg_msg_id)
);

CREATE TABLE IF NOT EXISTS inbox_rev (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rev INTEGER NOT NULL
);
INSERT OR IGNORE INTO inbox_rev (id, rev) VALUES (1, 0);

CREATE INDEX IF NOT EXISTS idx_inbox_msg_chat ON inbox_messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_inbox_msg_rev ON inbox_messages(rev);
CREATE INDEX IF NOT EXISTS idx_inbox_chat_rev ON inbox_chats(rev);
CREATE INDEX IF NOT EXISTS idx_inbox_chat_lid ON inbox_chats(lid);
"""

# Channels («труба»): every source lands in the same chats/messages tables.
#   wa    — WhatsApp through the QR bridge (wa-bridge/)
#   wac   — WhatsApp Cloud API (official, Meta)         backend/meta_api.py
#   ig    — Instagram Direct (Meta)                      backend/meta_api.py
#   tg    — Telegram, the company's own account          backend/tg_channels.py
#   tgbot — Telegram bot for guests                      backend/tg_channels.py
CHANNELS = {"wa": "WhatsApp", "wac": "WhatsApp", "wz": "WhatsApp", "wzig": "Instagram", "wztg": "Telegram",
            "ig": "Instagram", "tg": "Telegram", "tgbot": "Telegram-бот"}
WA_LIKE = ("wa", "wac", "wz")  # the same phone number through different transports = one chat
STATUS_ORDER = {"failed": -1, "pending": 0, "sent": 1, "delivered": 2, "read": 3}
PREVIEW = {
    "image": "📷 Фото", "video": "🎬 Видео", "audio": "🎤 Голосовое", "document": "📄 Файл",
    "sticker": "Стикер", "location": "📍 Локация", "contact": "👤 Контакт", "other": "Сообщение",
}


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_msg_chat_at ON inbox_messages(chat_id, at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_chat_last ON inbox_chats(last_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_chat_phone ON inbox_chats(phone)")
        if "receipt_pending" not in [r[1] for r in conn.execute("PRAGMA table_info(inbox_chats)").fetchall()]:
            conn.execute("ALTER TABLE inbox_chats ADD COLUMN receipt_pending INTEGER DEFAULT 0")  # guest not yet shown «read»
    config.INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _local(iso: str | None) -> str:
    """Bridge timestamps are UTC ISO ("...Z") — store local time like the rest
    of the DB."""
    if not iso:
        return _now()
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.isoformat(timespec="seconds")
    except ValueError:
        return _now()


def _bump(conn) -> int:
    conn.execute("UPDATE inbox_rev SET rev = rev + 1 WHERE id = 1")
    return conn.execute("SELECT rev FROM inbox_rev WHERE id = 1").fetchone()[0]


def current_rev() -> int:
    with database.get_conn() as conn:
        return conn.execute("SELECT rev FROM inbox_rev WHERE id = 1").fetchone()[0]


def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def phone_of_jid(jid: str | None) -> str:
    if not jid or not jid.endswith("@s.whatsapp.net"):
        return ""
    return digits(jid.split("@")[0].split(":")[0])


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
def _sig(text: str, n: int = 20) -> str:
    return hmac.new(config.INBOX_SECRET.encode(), text.encode(), hashlib.sha256).hexdigest()[:n]


def personal_key(uid: int) -> str:
    """The key in a person's inbox link: works in any browser (desktop Chrome
    included) and tells the server who is answering."""
    return f"{uid}.{_sig('user:%d' % uid)}"


def media_sig(name: str) -> str:
    return _sig("media:" + name, 16)


def _uid_from_key(key: str):
    uid, _, sig = (key or "").partition(".")
    if uid.isdigit() and sig and hmac.compare_digest(sig, _sig("user:" + uid)):
        return int(uid)
    return None


def _allowed_ids() -> set[int] | None:
    """INBOX_USERS resolved to ids; None = everyone active in the staff registry."""
    if not config.INBOX_USERS:
        return None
    ids, names = set(), set()
    for tok in config.INBOX_USERS:
        if tok.isdigit():
            ids.add(int(tok))
        else:
            names.add(tok.lstrip("@").lower())
    if names:
        for r in database.all_staff(active_only=False):
            if (r.get("username") or "").lower() in names:
                ids.add(r["staff_id"])
    return ids


def may_use(uid: int) -> bool:
    if uid in config.OWNER_TELEGRAM_IDS:
        return True
    allowed = _allowed_ids()
    if allowed is not None:
        return uid in allowed
    return any(r["staff_id"] == uid for r in database.all_staff(active_only=True))


def remember_agent(uid: int, name: str, username: str = "") -> None:
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO inbox_agents (uid, name, username, seen_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET name = COALESCE(NULLIF(excluded.name, ''), name), "
            "username = COALESCE(NULLIF(excluded.username, ''), username), seen_at = excluded.seen_at",
            (uid, name or "", username or "", _now()),
        )


def _agent_name(uid: int) -> str:
    with database.get_conn() as conn:
        r = conn.execute("SELECT name, username FROM inbox_agents WHERE uid = ?", (uid,)).fetchone()
        if r and (r["name"] or r["username"]):
            return r["name"] or "@" + r["username"]
        r = conn.execute("SELECT name, username FROM staff_registry WHERE staff_id = ?", (uid,)).fetchone()
        if r and (r["name"] or r["username"]):
            return r["name"] or "@" + r["username"]
    return "Владелец" if uid in config.OWNER_TELEGRAM_IDS else f"id{uid}"


def resolve_user(init_data: str, owner_key: str, inbox_key: str, crm_cookie: str = "") -> dict | None:
    """Who is calling, or None. Dev mode (bot not configured) = full access."""
    if crm_cookie:
        from . import crm
        u = crm.user_from_token(crm_cookie)
        if u:
            return {"uid": -u["id"], "name": u["name"], "owner": u["role"] == "admin", "crm": True}
    if not config.BOT_TOKEN or not config.OWNER_TELEGRAM_IDS:
        return {"uid": 0, "name": "Оператор", "owner": True}
    uid = _uid_from_key(inbox_key)
    if uid is None and init_data:
        user = auth.parse_init_data(init_data)
        if user and user.get("id"):
            uid = int(user["id"])
            full = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
            remember_agent(uid, full, user.get("username") or "")
    if uid is not None:
        if not may_use(uid):
            return None
        return {"uid": uid, "name": _agent_name(uid), "owner": uid in config.OWNER_TELEGRAM_IDS}
    if auth.key_ok(owner_key):
        return {"uid": 0, "name": "Владелец", "owner": True}
    return None


# ---------------------------------------------------------------------------
# Bookings: who is this guest?
# ---------------------------------------------------------------------------
_phone_cache: dict = {"at": 0.0, "map": {}}


def _bookings_by_phone() -> dict[str, list[dict]]:
    if time.time() - _phone_cache["at"] < 60:
        return _phone_cache["map"]
    m: dict[str, list[dict]] = {}
    try:
        with database.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, apartment_name, begin_date, end_date, client_name, client_phone, "
                "arrival_time, departure_time, amount, debt FROM bookings "
                "WHERE COALESCE(is_delete, 0) = 0 AND client_phone IS NOT NULL AND client_phone != ''"
            ).fetchall()
        for r in rows:
            d = digits(r["client_phone"])
            if len(d) >= 7:
                m.setdefault(d[-9:], []).append(dict(r))
    except Exception:  # noqa: BLE001
        logger.exception("booking phone index failed")
    _phone_cache.update(at=time.time(), map=m)
    return m


def booking_for(phone: str) -> dict | None:
    """The guest's current booking, else the next one, else the latest past."""
    d = digits(phone)
    if len(d) < 7:
        return None
    rows = _bookings_by_phone().get(d[-9:]) or []
    if not rows:
        return None
    today = date.today().isoformat()
    cur = [r for r in rows if r["begin_date"] <= today < r["end_date"] or r["end_date"] == today]
    nxt = sorted((r for r in rows if r["begin_date"] > today), key=lambda r: r["begin_date"])
    past = sorted((r for r in rows if r["end_date"] < today), key=lambda r: r["end_date"], reverse=True)
    b = (cur or nxt or past)[0]
    b = dict(b)
    b["when"] = "now" if cur else "next" if nxt else "past"
    return b


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def _default_times() -> list:
    try:
        from . import crm
        return list(crm.default_times())
    except Exception:  # noqa: BLE001
        return ["14:00", "11:00"]


def _chat_out(r) -> dict:
    c = dict(r)
    c["channel"] = c.get("channel") or "wa"
    c["channel_name"] = CHANNELS.get(c["channel"], c["channel"])
    c["title"] = c.get("name") or c.get("push_name") or (("+" + c["phone"]) if c.get("phone") else c["channel_name"])
    pb = _pinned_booking(c["id"])
    b = pb or booking_for(c.get("phone") or "")
    c["booking"] = ({"id": b["id"], "apartment": b["apartment_name"], "begin": b["begin_date"], "end": b["end_date"],
                     "guest": b["client_name"], "when": b["when"], "pinned": bool(pb),
                     "arrival_time": (b.get("arrival_time") or "")[:5], "departure_time": (b.get("departure_time") or "")[:5], "default_times": _default_times(),
                     "nights": b.get("days_count"), "amount": b.get("amount")} if b else None)
    return c


_pin_cache: dict = {"at": 0.0, "map": {}}


def _pinned_booking(chat_id: int):
    """Booking the team pinned to the chat (crm_booking_chats), 30 s cache."""
    if time.time() - _pin_cache["at"] > 30:
        m: dict = {}
        try:
            with database.get_conn() as conn:
                for r in conn.execute("SELECT chat_id, booking_id FROM crm_booking_chats").fetchall():
                    m.setdefault(r["chat_id"], []).append(r["booking_id"])
        except Exception:  # noqa: BLE001 — table not there yet
            pass
        _pin_cache.update(at=time.time(), map=m)
    if chat_id not in _pin_cache["map"]:
        return None
    try:
        from . import crm_ext
        return crm_ext.pinned_booking_for_chat(chat_id)
    except Exception:  # noqa: BLE001
        return None


def _msg_out(r) -> dict:
    m = dict(r)
    if m.get("media"):
        m["media_url"] = f"{config.API_PREFIX}/inbox/media/{m['media']}?s={media_sig(m['media'])}"
    return m


def chat_list(q: str = "", only: str = "all", me: str = "", limit: int = 300) -> list[dict]:
    sql = "SELECT * FROM inbox_chats"
    where, args = [], []
    if only == "unread":
        where.append("unread > 0")
    elif only == "mine":
        where.append("assignee = ?")
        args.append(me)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_at DESC LIMIT ?"
    args.append(limit if not q else 2000)
    with database.get_conn() as conn:
        rows = [_chat_out(r) for r in conn.execute(sql, args).fetchall()]
    if q:
        ql = q.lower().strip()
        qd = digits(q)
        rows = [c for c in rows
                if ql in (c["title"] or "").lower()
                or (qd and qd in (c.get("phone") or ""))
                or (c["booking"] and (ql in (c["booking"]["apartment"] or "").lower()
                                      or ql in (c["booking"]["guest"] or "").lower()))][:limit]
    return rows


def get_chat(chat_id: int) -> dict | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
        if not r:
            return None
        c = _chat_out(r)
        # the guest's notes from the CRM card («особенности гостя»)
        try:
            n = conn.execute("SELECT id, notes, status FROM crm_clients WHERE chat_id = ? OR (? != '' AND phone LIKE ?) "
                             "ORDER BY CASE WHEN chat_id = ? THEN 0 ELSE 1 END LIMIT 1",
                             (chat_id, c.get("phone") or "", "%" + (c.get("phone") or "")[-9:], chat_id)).fetchone()
            c["client_id"] = n["id"] if n else None
            c["client_notes"] = (n["notes"] or "") if n else ""
            c["client_status"] = (n["status"] or "") if n else ""
        except Exception:  # noqa: BLE001 — CRM tables not there yet
            c["client_id"], c["client_notes"], c["client_status"] = None, "", ""
    return c


def messages(chat_id: int, before_id: int | None = None, limit: int = 60) -> list[dict]:
    sql = "SELECT * FROM inbox_messages WHERE chat_id = ?"
    args: list = [chat_id]
    if before_id:
        sql += " AND id < ?"
        args.append(before_id)
    sql += " ORDER BY at DESC, id DESC LIMIT ?"
    args.append(limit)
    with database.get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_msg_out(r) for r in reversed(rows)]


def poll(since: int, chat_id: int | None) -> dict:
    with database.get_conn() as conn:
        rev = conn.execute("SELECT rev FROM inbox_rev WHERE id = 1").fetchone()[0]
        chats = [_chat_out(r) for r in conn.execute(
            "SELECT * FROM inbox_chats WHERE rev > ? ORDER BY last_at DESC", (since,)).fetchall()]
        msgs = []
        if chat_id:
            msgs = [_msg_out(r) for r in conn.execute(
                "SELECT * FROM inbox_messages WHERE chat_id = ? AND rev > ? ORDER BY at, id",
                (chat_id, since)).fetchall()]
        unread = conn.execute("SELECT COALESCE(SUM(unread), 0) FROM inbox_chats").fetchone()[0]
    return {"rev": rev, "chats": chats, "messages": msgs, "unread_total": unread}


# ---------------------------------------------------------------------------
# Storage from the bridge
# ---------------------------------------------------------------------------
def _find_chat(conn, jid: str, lid: str | None):
    r = conn.execute("SELECT * FROM inbox_chats WHERE jid = ?", (jid,)).fetchone()
    if r:
        return r
    if lid:
        r = conn.execute("SELECT * FROM inbox_chats WHERE jid = ? OR lid = ?", (lid, lid)).fetchone()
        if r:
            if r["jid"] == lid and jid != lid:
                # we learned the phone number behind a LID chat: switch to it
                conn.execute("UPDATE inbox_chats SET jid = ?, phone = ? WHERE id = ?",
                             (jid, phone_of_jid(jid), r["id"]))
            return conn.execute("SELECT * FROM inbox_chats WHERE id = ?", (r["id"],)).fetchone()
    return None


def _ensure_chat(conn, jid: str, lid: str | None = None, push_name: str | None = None,
                 channel: str = "wa", phone: str | None = None):
    r = _find_chat(conn, jid, lid)
    if r:
        if push_name and push_name != r["push_name"]:
            conn.execute("UPDATE inbox_chats SET push_name = ?, rev = ? WHERE id = ?",
                         (push_name, _bump(conn), r["id"]))
        if lid and not r["lid"]:
            conn.execute("UPDATE inbox_chats SET lid = ? WHERE id = ?", (lid, r["id"]))
        if channel != (r["channel"] or "wa") and channel in WA_LIKE and (r["channel"] or "wa") in WA_LIKE:
            # the same WhatsApp number now talks through the other transport: answer there
            conn.execute("UPDATE inbox_chats SET channel = ?, rev = ? WHERE id = ?", (channel, _bump(conn), r["id"]))
        if phone and not r["phone"]:
            conn.execute("UPDATE inbox_chats SET phone = ? WHERE id = ?", (phone, r["id"]))
        return r["id"]
    phone = phone if phone is not None else phone_of_jid(jid)
    cur = conn.execute(
        "INSERT INTO inbox_chats (channel, jid, lid, phone, push_name, created_at, rev, unread) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (channel, jid, lid, phone, push_name, _now(), _bump(conn)),
    )
    _new_chat_ids.append((cur.lastrowid, phone, push_name or "", CHANNELS.get(channel, channel), channel))
    return cur.lastrowid


_new_chat_ids: list = []


def _flush_new_clients() -> None:
    """A new chat = a client card in the CRM (outside the chat's transaction)."""
    while _new_chat_ids:
        chat_id, phone, name, source, channel = _new_chat_ids.pop()
        try:
            from . import crm
            if phone:
                cid = crm.ensure_client(phone, name, source, chat_id)
            else:
                cid = crm.ensure_client_for_chat(chat_id, name, source)
            if cid:
                crm.note_channel(cid, channel, name or (("+" + phone) if phone and channel in ("tg", "wztg") else ""))
        except Exception:  # noqa: BLE001
            logger.exception("crm client from chat failed")


def _preview(kind: str, text: str) -> str:
    t = (text or "").strip()
    if kind in ("text", None) or (t and kind not in PREVIEW):
        return t[:200]
    label = PREVIEW.get(kind, "Сообщение")
    return f"{label} · {t[:150]}" if t else label


def _touch_chat(conn, chat_id: int, at: str, direction: str, kind: str, text: str,
                status: str | None, unread_inc: int) -> None:
    r = conn.execute("SELECT last_at FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
    newer = not r["last_at"] or at >= r["last_at"]
    rev = _bump(conn)
    if newer:
        conn.execute(
            "UPDATE inbox_chats SET last_at = ?, last_text = ?, last_dir = ?, last_status = ?, "
            "unread = CASE WHEN ? = 'out' THEN 0 ELSE unread + ? END, rev = ? WHERE id = ?",
            (at, _preview(kind, text), direction, status, direction, unread_inc, rev, chat_id),
        )
    else:
        conn.execute("UPDATE inbox_chats SET unread = unread + ?, rev = ? WHERE id = ?",
                     (unread_inc, rev, chat_id))
    if direction == "in" and unread_inc:
        conn.execute("UPDATE inbox_chats SET receipt_pending = 1 WHERE id = ?", (chat_id,))


def store_message(m: dict, notify: bool = False, history: bool = False) -> dict | None:
    """One message from any channel (incoming, or sent from the phone/by us).
    m: id, jid, from_me, push_name, at, kind, text, media, mime, file_name,
    voice, lat, lng, quoted_id, reaction_to; channel (default wa), phone."""
    try:
        return _store_message(m, notify, history)
    finally:
        _flush_new_clients()


def _store_message(m: dict, notify: bool, history: bool) -> dict | None:
    jid = m.get("jid")
    if not jid or not m.get("id"):
        return None
    out = bool(m.get("from_me"))
    at = _local(m.get("at"))
    channel = m.get("channel") or "wa"
    with database.get_conn() as conn:
        if m.get("kind") == "reaction":
            target = m.get("reaction_to")
            if target:
                rev = _bump(conn)
                conn.execute("UPDATE inbox_messages SET reaction = ?, rev = ? WHERE wa_id = ?",
                             (m.get("text") or None, rev, target))
            return None
        chat_id = _ensure_chat(conn, jid, m.get("lid"), None if out else m.get("push_name"), channel, m.get("phone"))
        is_first = conn.execute("SELECT 1 FROM inbox_messages WHERE chat_id = ? LIMIT 1", (chat_id,)).fetchone() is None
        exists = conn.execute("SELECT id FROM inbox_messages WHERE wa_id = ?", (m["id"],)).fetchone()
        if exists:
            return None
        if out:
            # our own reply coming back from WhatsApp before send() stored its id
            pend = conn.execute(
                "SELECT id FROM inbox_messages WHERE chat_id = ? AND direction = 'out' "
                "AND wa_id LIKE 'local-%' AND COALESCE(text, '') = ? ORDER BY id DESC LIMIT 1",
                (chat_id, m.get("text") or ""),
            ).fetchone()
            if pend:
                conn.execute("UPDATE inbox_messages SET wa_id = ?, status = 'sent', rev = ? WHERE id = ?",
                             (m["id"], _bump(conn), pend["id"]))
                return None
        rev = _bump(conn)
        conn.execute(
            "INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, media, mime, "
            "file_name, voice, lat, lng, quoted_wa_id, status, at, rev) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, m["id"], "out" if out else "in", "Телефон" if out else "",
             m.get("kind") or "text", m.get("text") or "", m.get("media"), m.get("mime"),
             m.get("file_name"), 1 if m.get("voice") else 0, m.get("lat"), m.get("lng"),
             m.get("quoted_id"), "sent" if out else None, at, rev),
        )
        _touch_chat(conn, chat_id, at, "out" if out else "in", m.get("kind") or "text",
                    m.get("text") or "", "sent" if out else None,
                    0 if (out or history) else 1)
    if notify and not out and not history:
        _alert(chat_id, m)
        # auto-replies (first message / off hours) — in the background
        threading.Thread(target=_auto_incoming, args=(chat_id, is_first), daemon=True).start()
    return {"chat_id": chat_id}


def _auto_incoming(chat_id: int, is_first: bool) -> None:
    try:
        from . import crm_amo
        crm_amo.on_incoming(chat_id)
    except Exception:  # noqa: BLE001
        logger.exception("auto deal failed")
    try:
        from . import crm_ext
        crm_ext.on_incoming(chat_id, is_first)
    except Exception:  # noqa: BLE001
        logger.exception("auto incoming failed")


def store_history(items: list[dict]) -> int:
    n = 0
    for m in sorted(items or [], key=lambda x: x.get("at") or ""):
        try:
            if store_message(m, history=True):
                n += 1
        except Exception:  # noqa: BLE001
            logger.exception("history message failed")
    return n


def store_contacts(items: list[dict]) -> None:
    with database.get_conn() as conn:
        for c in items or []:
            jid, nm = c.get("jid"), (c.get("name") or "").strip()
            if not jid or not nm:
                continue
            r = conn.execute("SELECT id, name, push_name FROM inbox_chats WHERE jid = ?", (jid,)).fetchone()
            if not r:
                continue
            col = "name" if c.get("saved") else "push_name"
            if (r[col] or "") != nm and (col == "push_name" or not r["name"]):
                conn.execute(f"UPDATE inbox_chats SET {col} = ?, rev = ? WHERE id = ?",  # noqa: S608
                             (nm, _bump(conn), r["id"]))


def store_ack(wa_id: str, status: str) -> None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT id, chat_id, status FROM inbox_messages WHERE wa_id = ?", (wa_id,)).fetchone()
        if not r or STATUS_ORDER.get(status, 0) <= STATUS_ORDER.get(r["status"] or "pending", 0):
            return
        rev = _bump(conn)
        conn.execute("UPDATE inbox_messages SET status = ?, rev = ? WHERE id = ?", (status, rev, r["id"]))
        last = conn.execute("SELECT id FROM inbox_messages WHERE chat_id = ? ORDER BY at DESC, id DESC LIMIT 1",
                            (r["chat_id"],)).fetchone()
        if last and last["id"] == r["id"]:
            conn.execute("UPDATE inbox_chats SET last_status = ?, rev = ? WHERE id = ?",
                         (status, rev, r["chat_id"]))


def on_bridge_status(status: str) -> None:
    if status == "logged_out":
        _tg_send_all(config.OWNER_TELEGRAM_IDS,
                     "⚠️ WhatsApp отключился от «Чатов» (устройство отвязано на телефоне).\n"
                     "Откройте /chats → «Подключение» и отсканируйте QR-код заново.")


# ---------------------------------------------------------------------------
# Operator actions
# ---------------------------------------------------------------------------
class BridgeError(Exception):
    pass


def _bridge(method: str, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    try:
        resp = requests.request(method, config.WA_BRIDGE_URL + path, json=payload, timeout=timeout,
                                headers={"X-Inbox-Secret": config.INBOX_SECRET})
    except requests.RequestException as exc:
        raise BridgeError("WhatsApp-мост не запущен (окно «Nova WhatsApp»)") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        raise BridgeError(data.get("error") or f"мост ответил {resp.status_code}")
    return data


def bridge_status() -> dict:
    try:
        return _bridge("GET", "/status", timeout=5)
    except BridgeError as exc:
        return {"status": "offline", "error": str(exc), "qr": None}


def bridge_logout() -> None:
    _bridge("POST", "/logout")


def mark_read(chat_id: int) -> None:
    """Opening a chat clears OUR unread counter only. The guest keeps seeing the
    messages as unread until someone answers or presses «прочитано» (send_receipt):
    a colleague looking at a chat must not read as «we saw it and ignore you»."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT unread FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
        if not r or not r["unread"]:
            return
        conn.execute("UPDATE inbox_chats SET unread = 0, rev = ? WHERE id = ?", (_bump(conn), chat_id))


def send_receipt(chat_id: int) -> bool:
    """Tell the guest's messenger we read the chat (two blue ticks). Called when
    we answer, or by the «✓✓» button in the chat header."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT jid, channel, receipt_pending FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
        if not r or not r["receipt_pending"]:
            return False
        ids = [x["wa_id"] for x in conn.execute(
            "SELECT wa_id FROM inbox_messages WHERE chat_id = ? AND direction = 'in' ORDER BY id DESC LIMIT 30", (chat_id,)).fetchall()]
        conn.execute("UPDATE inbox_chats SET receipt_pending = 0, rev = ? WHERE id = ?", (_bump(conn), chat_id))
    threading.Thread(target=_safe_read, args=(r["channel"] or "wa", r["jid"], ids), daemon=True).start()
    return True


def _safe_read(channel, jid, ids) -> None:
    try:
        if channel == "wa":
            _bridge("POST", "/read", {"jid": jid, "ids": ids}, timeout=10)
        elif channel == "tg":
            from . import tg_channels
            tg_channels.user_read(jid)
    except Exception:  # noqa: BLE001
        pass


def update_chat(chat_id: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items() if k in ("assignee", "name", "unread")}
    if allowed:
        with database.get_conn() as conn:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            conn.execute(f"UPDATE inbox_chats SET {sets}, rev = ? WHERE id = ?",  # noqa: S608
                         (*allowed.values(), _bump(conn), chat_id))
    return get_chat(chat_id)


def send(chat_id: int, author: str, text: str = "", media: str | None = None, mime: str | None = None,
         file_name: str | None = None, quoted_id: str | None = None) -> dict:
    """Store the reply as 'pending', hand it to the chat's channel, then record
    the external id (or the error, so the operator sees a red mark and can retry)."""
    text = (text or "").strip()
    if not text and not media:
        raise BridgeError("Пустое сообщение")
    try:
        send_receipt(chat_id)  # answering = we have read it
    except Exception:  # noqa: BLE001
        pass
    kind = "text"
    if media:
        m = (mime or "")
        kind = ("image" if m.startswith("image/") and m != "image/gif" else "video" if m.startswith("video/")
                else "audio" if m.startswith("audio/") else "document")
    local_id = "local-" + uuid.uuid4().hex
    at = _now()
    with database.get_conn() as conn:
        chat = conn.execute("SELECT * FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
        if not chat:
            raise BridgeError("Чат не найден")
        quoted = None
        if quoted_id:
            quoted = conn.execute("SELECT direction, text FROM inbox_messages WHERE wa_id = ?",
                                  (quoted_id,)).fetchone()
        cur = conn.execute(
            "INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, media, mime, "
            "file_name, quoted_wa_id, status, at, rev) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (chat_id, local_id, author, kind, text, media, mime, file_name, quoted_id, at, _bump(conn)),
        )
        mid = cur.lastrowid
        _touch_chat(conn, chat_id, at, "out", kind, text, "pending", 0)
        if not chat["assignee"]:
            # the first one to answer takes the chat
            conn.execute("UPDATE inbox_chats SET assignee = ? WHERE id = ?", (author, chat_id))
    payload = {"jid": chat["jid"], "text": text, "media": media, "mime": mime, "file_name": file_name}
    if quoted_id and quoted:
        payload.update(quoted_id=quoted_id, quoted_from_me=quoted["direction"] == "out",
                       quoted_text=quoted["text"] or "")
    try:
        res = _dispatch_send(chat["channel"] or "wa", payload)
    except BridgeError as exc:
        with database.get_conn() as conn:
            rev = _bump(conn)
            conn.execute("UPDATE inbox_messages SET status = 'failed', error = ?, rev = ? WHERE id = ?",
                         (str(exc), rev, mid))
            conn.execute("UPDATE inbox_chats SET last_status = 'failed', rev = ? WHERE id = ?", (rev, chat_id))
        raise
    with database.get_conn() as conn:
        rev = _bump(conn)
        dup = conn.execute("SELECT id FROM inbox_messages WHERE wa_id = ? AND id != ?", (res["id"], mid)).fetchone()
        if dup:  # the echo from WhatsApp got stored first: keep ours (it has the author)
            conn.execute("DELETE FROM inbox_messages WHERE id = ?", (dup["id"],))
        conn.execute(
            "UPDATE inbox_messages SET wa_id = ?, status = CASE WHEN status IN ('delivered', 'read') "
            "THEN status ELSE 'sent' END, rev = ? WHERE id = ?", (res["id"], rev, mid))
        conn.execute("UPDATE inbox_chats SET last_status = 'sent', rev = ? WHERE id = ? AND last_status = 'pending'",
                     (rev, chat_id))
        row = conn.execute("SELECT * FROM inbox_messages WHERE id = ?", (mid,)).fetchone()
    return _msg_out(row)


def _dispatch_send(channel: str, payload: dict) -> dict:
    """Hand an outgoing message to its channel; returns {"id": external id}."""
    if channel == "wa":
        st = bridge_status()
        if st.get("status") == "connected":
            return _bridge("POST", "/send", payload, timeout=60)
        # the QR bridge is down: fall back to Wazzup, then to the Cloud API
        if config.WAZZUP_API_KEY:
            channel = "wz"
        elif config.WA_CLOUD_TOKEN and config.WA_CLOUD_PHONE_ID:
            channel = "wac"
        else:
            return _bridge("POST", "/send", payload, timeout=60)  # raises the bridge error
    try:
        if channel in ("wz", "wzig", "wztg"):
            from . import wazzup
            return wazzup.send(payload)
        if channel == "wac":
            from . import meta_api
            return meta_api.wa_send(payload)
        if channel == "ig":
            from . import meta_api
            return meta_api.ig_send(payload)
        if channel == "tg":
            from . import tg_channels
            return tg_channels.user_send(payload)
        if channel == "tgbot":
            from . import tg_channels
            return tg_channels.bot_send(payload)
    except BridgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("send via %s failed", channel)
        raise BridgeError(f"{CHANNELS.get(channel, channel)}: {exc}") from exc
    raise BridgeError(f"Неизвестный канал {channel}")


def retry(message_id: int, author: str) -> dict:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM inbox_messages WHERE id = ? AND status = 'failed'", (message_id,)).fetchone()
        if not r:
            raise BridgeError("Сообщение не найдено")
        conn.execute("DELETE FROM inbox_messages WHERE id = ?", (message_id,))
        _bump(conn)
    return send(r["chat_id"], author, r["text"] or "", r["media"], r["mime"], r["file_name"], r["quoted_wa_id"])


def _norm_phone(phone: str) -> str:
    d = digits(phone)
    if len(d) == 9:  # local Uzbek number without the country code
        d = "998" + d
    if len(d) < 10:
        raise BridgeError("Введите номер с кодом страны, например +998 90 123 45 67")
    return d


def new_chat_channels() -> list[dict]:
    """Which channels can start a conversation by phone number right now."""
    out = []
    st = bridge_status()
    if config.WAZZUP_API_KEY:
        from . import wazzup
        try:
            ok, hint = bool(wazzup.default_channel("whatsapp")), "В Wazzup нет активного WhatsApp-канала"
        except BridgeError as exc:
            ok, hint = False, str(exc)
        out.append({"code": "wz", "name": "WhatsApp (Wazzup)", "ready": ok, "check": False, "hint": "" if ok else hint})
    out.append({"code": "wa", "name": "WhatsApp (QR)", "ready": st.get("status") == "connected", "check": True,
                "hint": "" if st.get("status") == "connected" else "Телефон не привязан (нужен QR)"})
    if config.TG_API_ID and config.TG_API_HASH:
        from . import tg_channels
        ts = tg_channels.user_status()
        out.append({"code": "tg", "name": "Telegram", "ready": bool(ts.get("authorized")), "check": True,
                    "hint": "" if ts.get("authorized") else "Аккаунт не подключён — CRM → Каналы"})
    return out


def check_contact(phone: str, channel: str) -> dict:
    """Is the number reachable through this channel? exists: True / False / None (can't tell)."""
    d = _norm_phone(phone)
    if channel == "wa":
        res = _bridge("POST", "/check", {"phone": d}, timeout=20)
        return {"exists": bool(res.get("exists")), "phone": d, "jid": res.get("jid"),
                "note": "Номер есть в WhatsApp" if res.get("exists") else "У этого номера нет WhatsApp"}
    if channel == "tg":
        from . import tg_channels
        r = tg_channels.user_lookup(d)
        return {"exists": bool(r.get("exists")), "phone": d, "tg_id": r.get("id"), "name": r.get("name") or "",
                "note": ("Номер есть в Telegram" + (" · " + r["name"] if r.get("name") else "")) if r.get("exists")
                else "Номера нет в Telegram или он скрыл себя от поиска по номеру"}
    if channel == "wz":
        return {"exists": None, "phone": d, "note": "Wazzup не умеет проверять номер заранее: если WhatsApp нет, отправка вернёт ошибку"}
    raise BridgeError("Через этот канал нельзя начать чат по номеру")


def start_chat(phone: str, name: str = "", channel: str = "wa") -> dict:
    """«+ Новый чат»: check the number is reachable through the channel, create (or find) the chat."""
    d = _norm_phone(phone)
    channel = channel or "wa"
    if channel == "wa":
        st = bridge_status()
        if st.get("status") != "connected" and config.WAZZUP_API_KEY:
            channel = "wz"  # the QR bridge is down: Wazzup carries WhatsApp
    if channel == "wa":
        res = _bridge("POST", "/check", {"phone": d}, timeout=20)
        if not res.get("exists"):
            raise BridgeError("У этого номера нет WhatsApp")
        jid, phone_val = res["jid"], None
    elif channel == "wz":
        jid, phone_val = f"{d}@s.whatsapp.net", d
    elif channel == "tg":
        r = check_contact(d, "tg")
        if not r.get("exists"):
            raise BridgeError(r["note"])
        jid, phone_val = f"tg:{r['tg_id']}", d
        if not name.strip() and r.get("name"):
            name = r["name"]
    else:
        raise BridgeError("Через этот канал нельзя начать чат по номеру")
    with database.get_conn() as conn:
        chat_id = _ensure_chat(conn, jid, None, None, channel, phone_val)
        if name.strip():
            conn.execute("UPDATE inbox_chats SET name = ?, rev = ? WHERE id = ?", (name.strip(), _bump(conn), chat_id))
        conn.execute("UPDATE inbox_chats SET last_at = COALESCE(last_at, ?), rev = ? WHERE id = ?",
                     (_now(), _bump(conn), chat_id))
    _flush_new_clients()
    return get_chat(chat_id)


def templates() -> list[dict]:
    with database.get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM inbox_templates ORDER BY CASE WHEN command IS NULL OR command = '' THEN 1 ELSE 0 END, command, title, id"
        ).fetchall()]


def add_template(title: str, text: str) -> int:
    with database.get_conn() as conn:
        return conn.execute("INSERT INTO inbox_templates (title, text, created_at) VALUES (?, ?, ?)",
                            (title.strip(), text.strip(), _now())).lastrowid


def delete_template(tid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM inbox_templates WHERE id = ?", (tid,))


def agents() -> list[str]:
    names = {_agent_name(uid) for uid in config.OWNER_TELEGRAM_IDS}
    with database.get_conn() as conn:
        for r in conn.execute("SELECT uid FROM inbox_agents").fetchall():
            if may_use(r["uid"]):
                names.add(_agent_name(r["uid"]))
        try:
            for r in conn.execute("SELECT name FROM crm_users WHERE active = 1").fetchall():
                names.add(r["name"])
        except Exception:  # noqa: BLE001 — CRM tables not created yet
            pass
    return sorted(n for n in names if n)


# ---------------------------------------------------------------------------
# Telegram alerts; a reply to an alert goes back to the guest (see bot.py)
# ---------------------------------------------------------------------------
def _tg_send_all(targets, text: str) -> list[tuple[int, int]]:
    from . import notify
    sent = []
    for chat in targets:
        payload = {"chat_id": chat, "text": text[:4000], "disable_notification": config.quiet_now(),
                   "disable_web_page_preview": True}
        thread = notify._thread_for(chat, "inbox")  # noqa: SLF001 — a «Чаты» topic via /topic чаты
        if thread:
            payload["message_thread_id"] = thread
        res = notify._call("sendMessage", payload)  # noqa: SLF001 — shared low-level sender
        if isinstance(res, dict) and res.get("message_id"):
            sent.append((chat, res["message_id"]))
    return sent


def _alert(chat_id: int, m: dict) -> None:
    targets = config.inbox_notify_targets()
    if not targets or not config.BOT_TOKEN:
        return

    def run():
        try:
            c = get_chat(chat_id)
            if not c:
                return
            who = c["title"]
            if c.get("phone") and who != "+" + c["phone"]:
                who += f" (+{c['phone']})"
            b = c.get("booking")
            if b:
                who += f" · {b['apartment']}"
            body = _preview(m.get("kind") or "text", m.get("text") or "")
            text = f"💬 {c.get('channel_name') or 'WhatsApp'} · {who}\n\n{body}\n\n↩️ Ответьте на это сообщение — ответ уйдёт гостю."
            for tg_chat, tg_msg in _tg_send_all(targets, text):
                with database.get_conn() as conn:
                    conn.execute("INSERT OR REPLACE INTO inbox_tg_map VALUES (?, ?, ?, ?)",
                                 (tg_chat, tg_msg, chat_id, _now()))
        except Exception:  # noqa: BLE001
            logger.exception("inbox alert failed")

    threading.Thread(target=run, daemon=True).start()


def chat_for_alert(tg_chat_id: int, tg_msg_id: int) -> int | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT chat_id FROM inbox_tg_map WHERE tg_chat_id = ? AND tg_msg_id = ?",
                         (tg_chat_id, tg_msg_id)).fetchone()
    return r["chat_id"] if r else None
