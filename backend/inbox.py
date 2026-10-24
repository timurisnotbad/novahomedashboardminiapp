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
import time
import uuid
from datetime import date, datetime, timedelta

import requests

from . import auth, bg, config, database, util

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
            "ig": "Instagram", "tg": "Telegram", "tgbot": "Telegram-бот", "bk": "Booking.com"}
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
        ccols = [r[1] for r in conn.execute("PRAGMA table_info(inbox_chats)").fetchall()]
        if "receipt_pending" not in ccols:
            conn.execute("ALTER TABLE inbox_chats ADD COLUMN receipt_pending INTEGER DEFAULT 0")  # guest not yet shown «read»
        if "notify_stage" not in ccols:
            conn.execute("ALTER TABLE inbox_chats ADD COLUMN notify_stage INTEGER DEFAULT 9")  # 0 new → 1 pushed → 2 escalated; 9 answered
        if "receipt_info" not in ccols:
            conn.execute("ALTER TABLE inbox_chats ADD COLUMN receipt_info TEXT")  # who showed the guest «read», and when
        if "username" not in ccols:
            conn.execute("ALTER TABLE inbox_chats ADD COLUMN username TEXT")  # @handle on Telegram / Instagram — the lead's identity before a phone is known
        mcols = [r[1] for r in conn.execute("PRAGMA table_info(inbox_messages)").fetchall()]
        if "edited_at" not in mcols:
            conn.execute("ALTER TABLE inbox_messages ADD COLUMN edited_at TEXT")  # the guest (or we) edited the message in the messenger
        if "deleted_at" not in mcols:
            conn.execute("ALTER TABLE inbox_messages ADD COLUMN deleted_at TEXT")  # we deleted it for the guest; the row stays
    config.INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)


_now = util.now


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


_mem_rev = {"rev": -1, "read_at": 0.0}  # last rev this process wrote — lets /poll wait without hitting the DB


def _bump(conn) -> int:
    conn.execute("UPDATE inbox_rev SET rev = rev + 1 WHERE id = 1")
    rev = conn.execute("SELECT rev FROM inbox_rev WHERE id = 1").fetchone()[0]
    _mem_rev["rev"] = max(_mem_rev["rev"], rev)
    return rev


def rev_hint() -> int:
    """Cheap current rev: in-memory, re-read from the DB every 5 s in case someone
    else (a script, a second process) wrote to it."""
    if time.time() - _mem_rev["read_at"] > 5 or _mem_rev["rev"] < 0:
        try:
            _mem_rev["rev"] = max(_mem_rev["rev"], current_rev())
        except Exception:  # noqa: BLE001
            pass
        _mem_rev["read_at"] = time.time()
    return _mem_rev["rev"]


async def wait_for_change(since: int, timeout: float) -> None:
    """Long-poll: return as soon as the inbox rev moves past `since`, or after
    `timeout` seconds. One request every 25 s instead of one every 2.5 s — on a
    Cloudflare tunnel that is the difference between «лагает» and «мгновенно»."""
    import asyncio
    deadline = time.time() + max(0.0, min(timeout, 30.0))
    while time.time() < deadline:
        if rev_hint() > since:
            await asyncio.sleep(0.15)  # let the writing transaction commit
            # _mem_rev is bumped inside the transaction; after a rollback
            # (duplicate wa_id) the DB is behind it and every poll would
            # return at once with nothing — confirm against the DB first
            try:
                real = await asyncio.get_running_loop().run_in_executor(None, current_rev)
            except Exception:  # noqa: BLE001
                return
            if real > since:
                return
            _mem_rev["rev"] = real
            _mem_rev["read_at"] = time.time()
        await asyncio.sleep(0.4)


def current_rev() -> int:
    with database.get_conn() as conn:
        return conn.execute("SELECT rev FROM inbox_rev WHERE id = 1").fetchone()[0]


digits = util.digits


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
    return _sig("media:" + name, 32)


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


_staff_cache: dict = {"at": 0.0, "ids": set()}


def _active_staff_ids() -> set:
    """Active staff ids, 10 s cache: checked on every long-poll of the Mini App."""
    if time.time() - _staff_cache["at"] > 10:
        _staff_cache["ids"] = {r["staff_id"] for r in database.all_staff(active_only=True)}
        _staff_cache["at"] = time.time()
    return _staff_cache["ids"]


def may_use(uid: int) -> bool:
    if uid in config.OWNER_TELEGRAM_IDS:
        return True
    allowed = _allowed_ids()
    if allowed is not None:
        return uid in allowed
    return uid in _active_staff_ids()


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


_agent_seen: dict[int, float] = {}
_cookie_cache: dict[str, tuple[float, dict | None]] = {}


def resolve_user(init_data: str, owner_key: str, inbox_key: str, crm_cookie: str = "") -> dict | None:
    """Who is calling, or None. Dev mode (bot not configured) = full access."""
    if crm_cookie:
        hit = _cookie_cache.get(crm_cookie)
        if hit and time.time() - hit[0] < 5:  # one DB read per cookie per 5 s, not per long-poll
            u = hit[1]
        else:
            from . import crm
            u = crm.user_from_token(crm_cookie)
            if len(_cookie_cache) > 500:
                _cookie_cache.clear()
            _cookie_cache[crm_cookie] = (time.time(), u)
        if u:
            return {"uid": -u["id"], "name": u["name"], "owner": u["role"] == "admin", "crm": True}
    if auth.dev_mode():
        return {"uid": 0, "name": "Оператор", "owner": True}
    uid = _uid_from_key(inbox_key)
    if uid is None and init_data:
        user = auth.parse_init_data(init_data)
        if user and user.get("id"):
            uid = int(user["id"])
            full = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
            if time.time() - _agent_seen.get(uid, 0) > 600:  # one write per 10 min, not per long-poll
                _agent_seen[uid] = time.time()
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
                "SELECT * FROM bookings WHERE COALESCE(is_delete, 0) = 0 AND client_phone IS NOT NULL AND client_phone != ''"
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


def _receipt_text(c: dict) -> str | None:
    """Human line for the chat header: what the guest sees and who caused it."""
    if c.get("last_dir") != "in" and not c.get("receipt_pending"):
        return None
    if c.get("receipt_pending"):
        return "гость видит «не прочитано»"
    info = (c.get("receipt_info") or "").split("|")
    if len(info) < 2:
        return None
    when = info[1][11:16]
    if info[0] == "device":
        return f"прочитано на другом устройстве ({info[2] if len(info) > 2 else 'телефон'}) {when}"
    return f"прочитано: {RECEIPT_HOW.get(info[0], info[0])} {when}"


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
    if c["booking"]:
        try:
            from . import crm
            e = crm.eta_map().get(b["id"]) or {}
            c["booking"]["eta"], c["booking"]["eta_note"] = e.get("eta") or "", e.get("note") or ""
        except Exception:  # noqa: BLE001
            c["booking"]["eta"], c["booking"]["eta_note"] = "", ""
    c["stays"] = _stays_for_chat(c["id"], c.get("phone") or "")
    c["receipt"] = _receipt_text(c)
    if c["channel"] == "bk" and not pb:
        # Booking.com chat without a confirmed calendar booking: show what Booking says
        # and let a person pick the calendar booking (rooms/dates differ too often to guess)
        c["bk_stay"] = _ext_meta().get(c["id"])
    return c


_ext_meta_cache: dict = {"at": 0.0, "map": {}}


def _ext_meta() -> dict:
    """{chat_id: Booking.com stay} for every extension chat, 30 s cache (was a
    query per chat row in the list). booking_ext resets it on every write."""
    if time.time() - _ext_meta_cache["at"] > 30:
        m: dict = {}
        try:
            with database.get_conn() as conn:
                for r in conn.execute("SELECT chat_id, reservation, checkin, checkout, room FROM inbox_ext_meta").fetchall():
                    m[r["chat_id"]] = {"reservation": r["reservation"], "checkin": r["checkin"], "checkout": r["checkout"], "room": r["room"]}
        except Exception:  # noqa: BLE001 — table not there yet
            pass
        _ext_meta_cache.update(at=time.time(), map=m)
    return _ext_meta_cache["map"]


_pin_cache: dict = {"at": 0.0, "map": {}}


def _pinned_booking(chat_id: int):
    """Booking the team pinned to the chat (crm_booking_chats): current/next
    first, else the latest. One JOIN for all chats, 30 s cache (link/unlink
    reset it) — not a query per chat row."""
    if time.time() - _pin_cache["at"] > 30:
        m: dict = {}
        try:
            with database.get_conn() as conn:
                for r in conn.execute("SELECT l.chat_id, b.* FROM crm_booking_chats l JOIN bookings b ON b.id = l.booking_id "
                                      "WHERE COALESCE(b.is_delete, 0) = 0").fetchall():
                    m.setdefault(r["chat_id"], []).append(dict(r))
        except Exception:  # noqa: BLE001 — table not there yet
            pass
        _pin_cache.update(at=time.time(), map=m)
    rows = _pin_cache["map"].get(chat_id)
    if not rows:
        return None
    today = date.today().isoformat()
    cur = [r for r in rows if r["begin_date"] <= today <= r["end_date"]]
    nxt = sorted((r for r in rows if r["begin_date"] > today), key=lambda r: r["begin_date"])
    past = sorted(rows, key=lambda r: r["end_date"], reverse=True)
    b = dict((cur or nxt or past)[0])
    b["when"] = "now" if cur else "next" if nxt else "past"
    return b


def _stays_for_chat(chat_id: int, phone: str) -> list[dict]:
    """Every booking of the guest worth showing in the chat list: the ones pinned to the chat
    plus the ones found by phone; current and upcoming (a guest with several apartments sees
    them all), else the latest past one. [{id, apartment, begin, end, when}]."""
    rows: dict[int, dict] = {}
    _pinned_booking(chat_id)  # fills the cache
    for r in _pin_cache["map"].get(chat_id) or []:
        rows[r["id"]] = r
    d = digits(phone)
    if len(d) >= 7:
        for r in _bookings_by_phone().get(d[-9:]) or []:
            rows.setdefault(r["id"], r)
    if not rows:
        return []
    today = date.today().isoformat()
    def when(r):
        return "now" if r["begin_date"] <= today <= r["end_date"] else "next" if r["begin_date"] > today else "past"
    live = sorted((r for r in rows.values() if when(r) != "past"), key=lambda r: (r["begin_date"], r["apartment_name"] or ""))
    if not live:
        live = sorted(rows.values(), key=lambda r: r["end_date"], reverse=True)[:1]
    return [{"id": r["id"], "apartment": r["apartment_name"], "begin": r["begin_date"], "end": r["end_date"], "when": when(r)} for r in live[:6]]


def _msg_out(r) -> dict:
    m = dict(r)
    if m.get("media"):
        m["media_url"] = f"{config.API_PREFIX}/inbox/media/{m['media']}?s={media_sig(m['media'])}"
    return m


def chat_list(q: str = "", only: str = "all", me: str = "", limit: int = 300, before: str = "") -> list[dict]:
    """`before` = last_at cursor: the page of chats older than that (the list
    used to stop silently at 300)."""
    sql = "SELECT * FROM inbox_chats"
    where, args = [], []
    if only == "unread":
        where.append("unread > 0")
    elif only == "mine":
        where.append("assignee = ?")
        args.append(me)
    if before:
        where.append("last_at < ?")
        args.append(before)
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
        need = conn.execute("SELECT COUNT(*) FROM inbox_chats WHERE unread > 0").fetchone()[0]
    return {"rev": rev, "chats": chats, "messages": msgs, "unread_total": unread, "need_reply": need}


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
                 channel: str = "wa", phone: str | None = None, username: str | None = None):
    username = (username or "").strip().lstrip("@")[:64] or None
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
        changed = False
        if phone and not r["phone"]:
            conn.execute("UPDATE inbox_chats SET phone = ? WHERE id = ?", (phone, r["id"]))  # the guest shared the number / added us
            changed = True
        if username and username != (r["username"] or ""):
            conn.execute("UPDATE inbox_chats SET username = ? WHERE id = ?", (username, r["id"]))
            changed = True
        if changed:
            _lead_updates.append((r["id"], phone or r["phone"] or "", push_name or r["push_name"] or r["name"] or "", CHANNELS.get(channel, channel), channel, username or r["username"] or ""))
        return r["id"]
    phone = phone if phone is not None else phone_of_jid(jid)
    cur = conn.execute(
        "INSERT INTO inbox_chats (channel, jid, lid, phone, push_name, username, created_at, rev, unread) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (channel, jid, lid, phone, push_name, username, _now(), _bump(conn)),
    )
    _lead_updates.append((cur.lastrowid, phone or "", push_name or "", CHANNELS.get(channel, channel), channel, username or ""))
    return cur.lastrowid


_lead_updates: list = []  # (chat_id, phone, name, source, channel, username): new chats and chats that just got a phone / handle
_new_chat_ids = _lead_updates  # old name


def _flush_new_clients() -> None:
    """Every lead gets a client card the moment it writes (outside the chat's
    transaction); a phone or @handle learned later lands on the same card."""
    while _lead_updates:
        chat_id, phone, name, source, channel, username = _lead_updates.pop()
        try:
            from . import crm
            crm.register_lead(chat_id, phone, name, source, channel, username)
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
        conn.execute("UPDATE inbox_chats SET receipt_pending = 1, notify_stage = CASE WHEN notify_stage = 9 THEN 0 ELSE notify_stage END WHERE id = ?", (chat_id,))
    elif direction == "out":
        conn.execute("UPDATE inbox_chats SET notify_stage = 9 WHERE id = ?", (chat_id,))


def store_message(m: dict, notify: bool = False, history: bool = False) -> dict | None:
    """One message from any channel (incoming, or sent from the phone/by us).
    m: id, jid, from_me, push_name, at, kind, text, media, mime, file_name,
    voice, lat, lng, quoted_id, reaction_to; channel (default wa), phone."""
    try:
        return _store_message(m, notify, history)
    except Exception:
        _new_chat_ids.clear()  # the transaction rolled back: those chats do not exist
        raise
    finally:
        _flush_new_clients()


_PHONE_RE = re.compile(r"(?<!\d)(?:\+?998[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}|\+\d[\d\s\-()]{8,16}\d)(?!\d)")


def _phone_hint(m: dict) -> dict | None:
    """A phone number the guest just gave us: a shared contact card, or a number
    written in the text («мой номер +998 90 …»)."""
    kind, text = m.get("kind") or "text", (m.get("text") or "")
    if kind == "contact":
        d = re.sub(r"\D", "", text)
        digits = re.findall(r"\+?\d[\d\s\-()]{7,}\d", text)
        if not digits:
            return None
        num = re.sub(r"\D", "", digits[-1])
        name = re.sub(r"\+?[\d\s\-()]{8,}", "", text).strip()
        return {"phone": num, "name": name, "kind": "contact"} if 9 <= len(num) <= 15 else None
    if kind == "text" and len(text) <= 400:
        found = _PHONE_RE.search(text)
        if found:
            num = re.sub(r"\D", "", found.group(0))
            if 9 <= len(num) <= 15:
                return {"phone": num, "name": "", "kind": "text"}
    return None


def _apply_edit(conn, mid: int, text: str) -> None:
    rev = _bump(conn)
    conn.execute("UPDATE inbox_messages SET text = ?, edited_at = ?, rev = ? WHERE id = ?", (text, _now(), rev, mid))
    r = conn.execute("SELECT chat_id, kind, at FROM inbox_messages WHERE id = ?", (mid,)).fetchone()
    if r:
        last = conn.execute("SELECT id FROM inbox_messages WHERE chat_id = ? ORDER BY at DESC, id DESC LIMIT 1", (r["chat_id"],)).fetchone()
        conn.execute("UPDATE inbox_chats SET rev = ?" + (", last_text = ?" if last and last["id"] == mid else "") + " WHERE id = ?",
                     (rev, _preview(r["kind"], text), r["chat_id"]) if last and last["id"] == mid else (rev, r["chat_id"]))


def edit_message(wa_id: str, text: str) -> bool:
    """An edit that arrives on its own (WhatsApp bridge): update the stored text."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT id, text FROM inbox_messages WHERE wa_id = ?", (wa_id,)).fetchone()
        if not r or (text or "") == (r["text"] or ""):
            return False
        _apply_edit(conn, r["id"], text or "")
    return True


def _tkey(t) -> str:
    return re.sub(r"\s+", "", (t or "")).lower()[:400]


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
        chat_id = _ensure_chat(conn, jid, m.get("lid"), None if out else m.get("push_name"), channel, m.get("phone"), None if out else m.get("username"))
        is_first = conn.execute("SELECT 1 FROM inbox_messages WHERE chat_id = ? LIMIT 1", (chat_id,)).fetchone() is None
        exists = conn.execute("SELECT id, text FROM inbox_messages WHERE wa_id = ?", (m["id"],)).fetchone()
        if exists:
            # the same message again with another text = an edit (Telegram says so; Wazzup re-delivers the id)
            new_text = (m.get("text") or "")
            if (m.get("edited") or channel in ("wz", "wzig", "wztg")) and new_text and new_text != (exists["text"] or ""):
                _apply_edit(conn, exists["id"], new_text)
            return None
        if out:
            # our own reply coming back from the channel before send() stored its id
            # (the channel may trim or re-wrap the text: compare without whitespace)
            key = _tkey(m.get("text"))
            pend = None
            for r in conn.execute("SELECT id, wa_id, text, at FROM inbox_messages WHERE chat_id = ? AND direction = 'out' "
                                  "ORDER BY id DESC LIMIT 8", (chat_id,)).fetchall():
                if _tkey(r["text"]) != key:
                    continue
                if (r["wa_id"] or "").startswith("local-"):
                    pend = r
                    break
                if (r["at"] or "") >= (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S"):
                    return None  # the same text was just sent from the CRM under another id: an echo, not a new message
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
        if out:
            conn.execute("UPDATE inbox_chats SET last_author = 'Телефон' WHERE id = ?", (chat_id,))
    if not out:
        hint = _phone_hint(m)
        if hint:
            try:
                from . import crm
                crm.lead_phone_hint(chat_id, hint["phone"], hint["name"], hint["kind"])
            except Exception:  # noqa: BLE001
                logger.exception("phone hint failed")
    if notify and not out and not history:
        st = _notify_settings()
        if st["delay"] == 0:  # «сразу»: push + Telegram right now, in the background
            bg.run(_notify_chat, chat_id, 1, st, key=f"notify:{chat_id}")
        # auto-replies (first message / off hours) — in the background
        bg.run(_auto_incoming, chat_id, is_first)
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
    try:
        from . import ai
        ai.transcribe_pending(chat_id)  # voice notes → text for the team and the assistant (needs an OpenAI key)
        ai.on_incoming(chat_id)  # the assistant: triage, reply or draft after a short pause
    except Exception:  # noqa: BLE001
        logger.exception("ai incoming failed")


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


def mark_failed(wa_id: str, error: str) -> None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT id, chat_id FROM inbox_messages WHERE wa_id = ?", (wa_id,)).fetchone()
        if not r:
            return
        rev = _bump(conn)
        conn.execute("UPDATE inbox_messages SET status = 'failed', error = ?, rev = ? WHERE id = ?", (error[:300], rev, r["id"]))
        conn.execute("UPDATE inbox_chats SET last_status = 'failed', rev = ? WHERE id = ?", (rev, r["chat_id"]))


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


_bridge_cache: dict = {"at": 0.0, "st": None}


def bridge_status(fresh: bool = False) -> dict:
    """The QR bridge's state. Cached 10 s (3 s while it is not connected):
    it was an HTTP round-trip (up to 5 s when the bridge is down) before every
    send, on the home page and in every chat-list refresh."""
    st = _bridge_cache["st"]
    ttl = 10 if st and st.get("status") == "connected" else 3
    if fresh or st is None or time.time() - _bridge_cache["at"] > ttl:
        try:
            st = _bridge("GET", "/status", timeout=5)
        except BridgeError as exc:
            st = {"status": "offline", "error": str(exc), "qr": None}
        _bridge_cache.update(at=time.time(), st=st)
    return dict(st)


def bridge_logout() -> None:
    _bridge("POST", "/logout")


def mark_read(chat_id: int) -> None:
    """Explicit «handled» without a reply (not called when a chat is merely
    opened: the unread badge means «needs an answer» and stays until someone
    answers or presses ✓✓)."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT unread FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
        if not r or not r["unread"]:
            return
        conn.execute("UPDATE inbox_chats SET unread = 0, rev = ? WHERE id = ?", (_bump(conn), chat_id))


RECEIPT_HOW = {"reply": "ответ из CRM", "button": "кнопка ✓✓ в CRM", "device": "другое устройство"}


def send_receipt(chat_id: int, how: str = "button") -> bool:
    """Tell the guest's messenger we read the chat (two blue ticks). Called when
    we answer (how='reply'), or by the «✓✓» button in the chat header."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT jid, channel, receipt_pending FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
        if not r or not r["receipt_pending"]:
            return False
        ids = [x["wa_id"] for x in conn.execute(
            "SELECT wa_id FROM inbox_messages WHERE chat_id = ? AND direction = 'in' ORDER BY id DESC LIMIT 30", (chat_id,)).fetchall()]
        conn.execute("UPDATE inbox_chats SET receipt_pending = 0, unread = 0, receipt_info = ?, rev = ? WHERE id = ?",
                     (f"{how}|{_now()}", _bump(conn), chat_id))
    bg.run(_safe_read, r["channel"] or "wa", r["jid"], ids)
    return True


def receipt_external(jid: str, where: str) -> None:
    """The messenger says *we* read this chat on another device (the phone, Telegram
    Desktop…): the guest already sees «read», nothing for the CRM to send — but
    the team should see that it was not the CRM."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT id, receipt_pending FROM inbox_chats WHERE jid = ?", (jid,)).fetchone()
        if not r or not r["receipt_pending"]:
            return
        conn.execute("UPDATE inbox_chats SET receipt_pending = 0, receipt_info = ?, rev = ? WHERE id = ?",
                     (f"device|{_now()}|{where}", _bump(conn), r["id"]))


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
        send_receipt(chat_id, "reply")  # answering = we have read it
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
        conn.execute("UPDATE inbox_chats SET last_author = ? WHERE id = ?", (author, chat_id))
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
        if res.get("status") == "pending":  # handed to the Booking.com extension: confirmed later
            conn.execute("UPDATE inbox_messages SET wa_id = ?, rev = ? WHERE id = ?", (res["id"], rev, mid))
        else:
            conn.execute(
                "UPDATE inbox_messages SET wa_id = ?, status = CASE WHEN status IN ('delivered', 'read') "
                "THEN status ELSE 'sent' END, rev = ? WHERE id = ?", (res["id"], rev, mid))
            conn.execute("UPDATE inbox_chats SET last_status = 'sent', rev = ? WHERE id = ? AND last_status = 'pending'",
                         (rev, chat_id))
        row = conn.execute("SELECT * FROM inbox_messages WHERE id = ?", (mid,)).fetchone()
    return _msg_out(row)


def send_album(chat_id: int, author: str, caption: str, items: list) -> list[dict]:
    """Several photos at once: one Telegram album per 10 (the guest sees a
    gallery, not twenty separate messages); other channels get them one by one,
    the caption on the first. items = [(media, mime), …]."""
    items = [(m, mt or "image/jpeg") for m, mt in items if m]
    if not items:
        return []
    chat = get_chat(chat_id)
    if not chat:
        raise BridgeError("Чат не найден")
    if (chat.get("channel") or "wa") != "tg" or len(items) < 2:
        return [send(chat_id, author, caption if i == 0 else "", m, mt) for i, (m, mt) in enumerate(items)]
    from . import tg_channels
    try:
        send_receipt(chat_id, "reply")
    except Exception:  # noqa: BLE001
        pass
    out: list[dict] = []
    for k in range(0, len(items), 10):
        chunk = items[k:k + 10]
        at, mids = _now(), []
        with database.get_conn() as conn:
            for i, (m, mt) in enumerate(chunk):
                kind = "image" if mt.startswith("image/") else "video" if mt.startswith("video/") else "document"
                text = caption if (k == 0 and i == 0) else ""
                cur = conn.execute(
                    "INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, media, mime, status, at, rev) "
                    "VALUES (?, ?, 'out', ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (chat_id, "local-" + uuid.uuid4().hex, author, kind, text, m, mt, at, _bump(conn)))
                mids.append(cur.lastrowid)
            _touch_chat(conn, chat_id, at, "out", kind, text, "pending", 0)
            conn.execute("UPDATE inbox_chats SET last_author = ? WHERE id = ?", (author, chat_id))
            if not chat["assignee"]:
                conn.execute("UPDATE inbox_chats SET assignee = ? WHERE id = ?", (author, chat_id))
        try:
            res = tg_channels.user_send_album({"jid": chat["jid"], "text": caption if k == 0 else "", "media": [m for m, _ in chunk]})
        except BridgeError as exc:
            with database.get_conn() as conn:
                rev = _bump(conn)
                for mid in mids:
                    conn.execute("UPDATE inbox_messages SET status = 'failed', error = ?, rev = ? WHERE id = ?", (str(exc), rev, mid))
                conn.execute("UPDATE inbox_chats SET last_status = 'failed', rev = ? WHERE id = ?", (rev, chat_id))
            raise
        with database.get_conn() as conn:
            rev = _bump(conn)
            for mid, wid in zip(mids, res.get("ids") or []):
                dup = conn.execute("SELECT id FROM inbox_messages WHERE wa_id = ? AND id != ?", (wid, mid)).fetchone()
                if dup:
                    conn.execute("DELETE FROM inbox_messages WHERE id = ?", (dup["id"],))
                conn.execute("UPDATE inbox_messages SET wa_id = ?, status = 'sent', rev = ? WHERE id = ?", (wid, rev, mid))
            conn.execute("UPDATE inbox_chats SET last_status = 'sent', rev = ? WHERE id = ? AND last_status = 'pending'", (rev, chat_id))
            out += [_msg_out(conn.execute("SELECT * FROM inbox_messages WHERE id = ?", (mid,)).fetchone()) for mid in mids]
    return out


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
        if channel == "bk":
            from . import booking_ext
            return booking_ext.enqueue(payload)
    except BridgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("send via %s failed", channel)
        raise BridgeError(f"{CHANNELS.get(channel, channel)}: {exc}") from exc
    raise BridgeError(f"Неизвестный канал {channel}")


# ---------------------------------------------------------------------------
# Missed calls (WhatsApp QR bridge): a «📞» line in the chat, the chat waits
# for a call back, and an auto-reply in the guest's language — Russian for
# Russian-speaking cards and numbers, English for everyone else.
# ---------------------------------------------------------------------------
RU_PHONE_PREFIXES = ("7", "998", "996", "992", "993", "994", "375", "374")
MISSED_CALL_RU = "Здравствуйте! Не смогли ответить на звонок. Напишите, пожалуйста, здесь — ответим в ближайшее время."
MISSED_CALL_EN = "Hello! We missed your call. Please write to us here and we will reply shortly."


def guest_language(chat: dict) -> str:
    """'ru' or 'en': the guest card's language first, else the number's country."""
    try:
        from . import crm_ext
        lang = crm_ext.guest_lang(chat)
    except Exception:  # noqa: BLE001
        lang = ""
    if lang:
        return "ru" if lang in ("RU", "UZ") else "en"
    d = digits(chat.get("phone") or "")
    return "ru" if any(d.startswith(p) for p in RU_PHONE_PREFIXES) else "en"


def on_missed_call(jid: str, call_id: str, status: str = "timeout", at: str = "") -> dict:
    from . import crm
    if not jid:
        return {}
    now = _now()
    with database.get_conn() as conn:
        chat_id = _ensure_chat(conn, jid, None, None, "wa", phone_of_jid(jid))
        key = f"call:{call_id or now}"
        if call_id and conn.execute("SELECT 1 FROM inbox_messages WHERE wa_id = ?", (key,)).fetchone():
            return {"chat_id": chat_id, "dup": True}
        label = "📞 Пропущенный звонок" + (" (отклонён)" if status == "reject" else "")
        conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, status, at, rev) VALUES (?, ?, 'in', '', 'call', ?, 'read', ?, ?)",
                     (chat_id, key, label, now, _bump(conn)))
        _touch_chat(conn, chat_id, now, "in", "call", label, None, 1)
        recent = conn.execute("SELECT 1 FROM inbox_messages WHERE chat_id = ? AND direction = 'out' AND author = 'Автоответ' AND at >= ? LIMIT 1",
                              (chat_id, (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"))).fetchone()
    out = {"chat_id": chat_id, "replied": False}
    if crm.get_setting("missed_call_reply", "1") != "1" or recent:
        return out
    chat = get_chat(chat_id)
    lang = guest_language(chat or {"phone": phone_of_jid(jid)})
    text = crm.get_setting("missed_call_en", "") if lang == "en" else crm.get_setting("missed_call_ru", "")
    text = (text or (MISSED_CALL_EN if lang == "en" else MISSED_CALL_RU)).strip()
    try:
        send(chat_id, "Автоответ", text)
        out.update(replied=True, lang=lang)
    except Exception as exc:  # noqa: BLE001
        logger.warning("missed-call reply failed for %s: %s", jid, exc)
    return out


EDITABLE = {"wa": "WhatsApp", "tg": "Telegram", "tgbot": "Telegram-бот"}


def _own_message(conn, message_id: int, who: dict):
    r = conn.execute("SELECT m.*, c.channel, c.jid FROM inbox_messages m JOIN inbox_chats c ON c.id = m.chat_id WHERE m.id = ?", (message_id,)).fetchone()
    if not r:
        raise BridgeError("Сообщение не найдено")
    if r["direction"] != "out":
        raise BridgeError("Можно менять только свои сообщения")
    if r["status"] in ("pending", "failed") or not r["wa_id"] or str(r["wa_id"]).startswith("local-"):
        raise BridgeError("Сообщение ещё не доставлено")
    if r["deleted_at"]:
        raise BridgeError("Сообщение уже удалено")
    if not who.get("owner") and (r["author"] or "") != (who.get("name") or ""):
        raise BridgeError("Чужое сообщение может изменить только владелец")
    ch = r["channel"] or "wa"
    if ch not in EDITABLE:
        raise BridgeError(f"{CHANNELS.get(ch, ch)}: мессенджер не даёт менять отправленные сообщения")
    return dict(r)


def _dispatch_change(channel: str, action: str, payload: dict) -> None:
    if channel == "wa":
        if bridge_status().get("status") != "connected":
            raise BridgeError("WhatsApp (QR) не подключён — изменить сообщение нельзя")
        _bridge("POST", f"/{action}", payload, timeout=60)
        return
    from . import tg_channels
    if channel == "tg":
        (tg_channels.user_edit if action == "edit" else tg_channels.user_delete)(payload)
    elif channel == "tgbot":
        (tg_channels.bot_edit if action == "edit" else tg_channels.bot_delete)(payload)
    else:
        raise BridgeError("Канал не поддерживает правку")


def edit_sent(message_id: int, text: str, who: dict) -> dict:
    """Change the text of our own message for the guest too (WhatsApp QR, Telegram account, guest bot)."""
    text = (text or "").strip()
    if not text:
        raise BridgeError("Пустой текст")
    with database.get_conn() as conn:
        r = _own_message(conn, message_id, who)
    if r["kind"] not in ("text", "image", "video", "document"):
        raise BridgeError("Этот тип сообщения не редактируется")
    _dispatch_change(r["channel"] or "wa", "edit", {"jid": r["jid"], "id": r["wa_id"], "text": text, "has_media": bool(r["media"])})
    with database.get_conn() as conn:
        _apply_edit(conn, message_id, text)
        conn.execute("UPDATE inbox_chats SET last_text = ? WHERE id = ? AND (SELECT MAX(id) FROM inbox_messages WHERE chat_id = ?) = ?",
                     (text, r["chat_id"], r["chat_id"], message_id))
        row = conn.execute("SELECT * FROM inbox_messages WHERE id = ?", (message_id,)).fetchone()
    return _msg_out(row)


def delete_sent(message_id: int, who: dict) -> dict:
    """Delete our own message for the guest; the row stays in the CRM, marked «удалено»."""
    with database.get_conn() as conn:
        r = _own_message(conn, message_id, who)
    _dispatch_change(r["channel"] or "wa", "delete", {"jid": r["jid"], "id": r["wa_id"]})
    with database.get_conn() as conn:
        rev = _bump(conn)
        conn.execute("UPDATE inbox_messages SET deleted_at = ?, rev = ? WHERE id = ?", (_now(), rev, message_id))
        conn.execute("UPDATE inbox_chats SET last_text = '🚫 Сообщение удалено', rev = ? WHERE id = ? AND (SELECT MAX(id) FROM inbox_messages WHERE chat_id = ?) = ?",
                     (rev, r["chat_id"], r["chat_id"], message_id))
        row = conn.execute("SELECT * FROM inbox_messages WHERE id = ?", (message_id,)).fetchone()
    return _msg_out(row)


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
def _tg_send_all(targets, text: str, token: str | None = None) -> list[tuple[int, int]]:
    from . import notify
    sent = []
    for chat in targets:
        payload = {"chat_id": chat, "text": text[:4000], "disable_notification": config.quiet_now(),
                   "disable_web_page_preview": True}
        thread = notify._thread_for(chat, "inbox")  # noqa: SLF001 — a «Чаты» topic via /topic чаты
        if thread:
            payload["message_thread_id"] = thread
        res = notify._call("sendMessage", payload, token)  # noqa: SLF001 — shared low-level sender
        if isinstance(res, dict) and res.get("message_id"):
            sent.append((chat, res["message_id"]))
    return sent


def _alert(chat_id: int, m: dict, extra_targets=None) -> None:
    """Telegram alert about a guest message: to the work chats (INBOX_NOTIFY_CHAT_IDS)
    and to the personal Telegram of the users who linked it (/link in the bot)."""
    personal = list(dict.fromkeys(extra_targets or []))  # linked CRM users: the CRM bot (or the dashboard bot when it is the same)
    base = [t for t in dict.fromkeys(config.inbox_notify_targets()) if t not in personal]  # work chats: the dashboard bot
    if not (base or personal) or not config.BOT_TOKEN:
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
            link = f"\n🔗 {config.WEBAPP_URL.rstrip('/')}/crm/#messages/{chat_id}" if config.WEBAPP_URL else ""
            text = f"💬 {c.get('channel_name') or 'WhatsApp'} · {who}\n\n{body}\n\n↩️ Ответьте на это сообщение — ответ уйдёт гостю.{link}"
            pairs = _tg_send_all(base, text) + _tg_send_all(personal, text, config.crm_bot_token())
            for tg_chat, tg_msg in pairs:
                with database.get_conn() as conn:
                    conn.execute("INSERT OR REPLACE INTO inbox_tg_map VALUES (?, ?, ?, ?)",
                                 (tg_chat, tg_msg, chat_id, _now()))
        except Exception:  # noqa: BLE001
            logger.exception("inbox alert failed")

    bg.run(run)


def _notify_settings() -> dict:
    try:
        from . import crm
        return {"delay": int(crm.get_setting("notify_delay", "2") or 0), "escalate": int(crm.get_setting("notify_escalate", "10") or 0),
                "push": crm.get_setting("notify_push", "1") == "1", "telegram": crm.get_setting("notify_telegram", "1") == "1"}
    except Exception:  # noqa: BLE001
        return {"delay": 0, "escalate": 10, "push": True, "telegram": True}


def notify_pending() -> int:
    """Every minute: a guest message nobody answered for `delay` minutes → push to
    the responsible manager (everyone when unassigned) + Telegram alert; still
    unanswered after `escalate` more minutes → push to all admins again, marked ⚠️."""
    st = _notify_settings()
    now = datetime.now()
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, last_at, notify_stage, assignee, unread FROM inbox_chats WHERE unread > 0 AND last_dir = 'in' AND notify_stage < 2").fetchall()]
    n = 0
    for c in rows:
        try:
            age = (now - datetime.fromisoformat(c["last_at"])).total_seconds() / 60
        except (TypeError, ValueError):
            continue
        stage = c["notify_stage"] or 0
        if stage == 0 and age >= st["delay"]:
            bg.run(_notify_chat, c["id"], 1, st, key=f"notify:{c['id']}")
            n += 1
        elif stage == 1 and st["escalate"] > 0 and age >= st["delay"] + st["escalate"]:
            bg.run(_notify_chat, c["id"], 2, st, key=f"notify:{c['id']}")
            n += 1
    return n


def _notify_chat(chat_id: int, stage: int, st: dict) -> None:
    from . import crm, webpush
    c = get_chat(chat_id)
    if not c:
        return
    with database.get_conn() as conn:
        conn.execute("UPDATE inbox_chats SET notify_stage = ? WHERE id = ?", (stage, chat_id))
        last = conn.execute("SELECT kind, text FROM inbox_messages WHERE chat_id = ? AND direction = 'in' ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
    body = _preview(last["kind"] if last else "text", last["text"] if last else "") or "Новое сообщение"
    who = c["title"] + (f" · {c['booking']['apartment']}" if c.get("booking") else "")
    title = (f"⚠️ Без ответа {st['delay'] + st['escalate']} мин · " if stage == 2 else "") + f"{c.get('channel_name') or 'Чат'} · {who}"
    url = f"/crm/#messages/{chat_id}"
    if st["push"]:
        users = crm.list_users()
        if stage == 2:
            uids = [u["id"] for u in users if u["role"] == "admin" and u["active"]]
        else:
            uids = [u["id"] for u in users if u["active"] and u["name"] == c.get("assignee")] or [u["id"] for u in users if u["active"]]
        try:
            webpush.send_to(uids, title, body, url, tag=f"nh-{chat_id}")
        except Exception:  # noqa: BLE001
            logger.exception("push notify failed")
    if st["telegram"]:
        # personal Telegram: the responsible manager (everyone when unassigned); the escalation goes to admins
        users = crm.list_users()
        from . import alerts
        if stage == 2:
            personal = [t for t in alerts.targets("messages") if t in crm.users_tg_ids(admins_only=True)]
        else:
            mine = [u["id"] for u in users if u["active"] and u["name"] == c.get("assignee")]
            personal = alerts.targets("messages", mine) if mine else alerts.targets("messages")
        _alert(chat_id, {"kind": last["kind"] if last else "text", "text": (("⚠️ без ответа · " if stage == 2 else "") + (last["text"] if last else ""))}, personal)


def chat_for_alert(tg_chat_id: int, tg_msg_id: int) -> int | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT chat_id FROM inbox_tg_map WHERE tg_chat_id = ? AND tg_msg_id = ?",
                         (tg_chat_id, tg_msg_id)).fetchone()
    return r["chat_id"] if r else None
