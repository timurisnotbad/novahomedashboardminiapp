"""Booking.com messages through the Nova Home Chrome extension.

Booking.com gives no messaging API to properties, so a small extension runs in
the owner's own logged-in extranet tab: it reads guest messages from the page
and posts them here (channel «bk»), and takes our replies from the outbox and
types them into the extranet. Selectors live in a server-side config
(«ext_selectors» setting), so the extension is tuned without reinstalling.
"""
import hashlib
import json
import logging
import re
from datetime import datetime

from . import config, database, inbox

logger = logging.getLogger("nova.booking_ext")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_ext_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    jid TEXT,
    reservation TEXT,
    guest TEXT,
    text TEXT,
    wa_id TEXT,
    status TEXT DEFAULT 'pending',   -- pending | taken | done | failed
    error TEXT,
    created_at TEXT,
    taken_at TEXT,
    done_at TEXT
);
"""
_state: dict = {"last_ping": None, "page": "", "version": "", "unread": 0, "errors": []}

DEFAULT_SELECTORS = {
    "search": "input[placeholder*='номер бронирования' i], input[placeholder*='booking number' i]",
    "listItem": "[data-testid*='conversation' i], [class*='conversation-list' i] li, [class*='inbox' i] [role='listitem'], [class*='thread' i][class*='item' i]",
    "listUnread": ".unread, [class*='unread' i], [class*='dot' i]",
    "convHeader": "[class*='header' i] h1, [class*='header' i] h2, [class*='conversation' i] [class*='title' i]",
    "message": "[data-testid*='message' i], [class*='message-bubble' i], [class*='bubble' i], [class*='message' i][class*='item' i]",
    "msgTime": "time, [class*='time' i], [class*='timestamp' i]",
    "composer": "textarea, [contenteditable='true']",
    "sendButton": "button[type='submit'], button[class*='send' i], button[data-testid*='send' i]",
    "reservationLabel": "Номер бронирования|Booking number|Reservation number",
    "guestLabel": "Имя гостя|Guest name",
    "checkinLabel": "Заезд|Check-in",
    "checkoutLabel": "Отъезд|Выезд|Check-out",
    "roomLabel": "номер:|Room|Unit",
    "outgoingHints": "Доставлено|Delivered|Прочитано|Read|Отправлено|Sent",
}


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)
    migrate_meta()


def token() -> str:
    """The extension's key: derived from INBOX_SECRET, shown once in CRM → Каналы."""
    return hashlib.sha256(("ext:" + config.INBOX_SECRET).encode()).hexdigest()[:32]


def selectors() -> dict:
    from . import crm
    try:
        custom = json.loads(crm.get_setting("ext_selectors", "") or "{}")
    except ValueError:
        custom = {}
    return {**DEFAULT_SELECTORS, **{k: v for k, v in custom.items() if isinstance(v, str)}}


def set_selectors(text: str) -> dict:
    from . import crm
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError("Нужен JSON-объект")
    crm.set_setting("ext_selectors", json.dumps(data, ensure_ascii=False))
    return selectors()


def ping(info: dict) -> None:
    _state.update(last_ping=datetime.now().isoformat(timespec="seconds"), page=(info.get("page") or "")[:120],
                  version=(info.get("version") or "")[:20], unread=int(info.get("unread") or 0))
    if info.get("error"):
        _state["errors"] = ([str(info["error"])[:200]] + _state["errors"])[:5]


def status() -> dict:
    with database.get_conn() as conn:
        chats = conn.execute("SELECT COUNT(*) FROM inbox_chats WHERE channel = 'bk'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM inbox_ext_outbox WHERE status IN ('pending', 'taken')").fetchone()[0]
    alive = False
    if _state["last_ping"]:
        alive = (datetime.now() - datetime.fromisoformat(_state["last_ping"])).total_seconds() < 180
    return {**_state, "alive": alive, "chats": chats, "pending": pending, "token": token(), "selectors": selectors(),
            "hook_url": f"{(config.WEBAPP_URL or '').split('?')[0].rstrip('/')}{config.API_PREFIX}/inbox/ext"}


def _jid(reservation: str, guest: str) -> str:
    key = re.sub(r"\D", "", reservation or "") or hashlib.sha1((guest or "").strip().lower().encode()).hexdigest()[:12]
    return f"bk:{key}"


def ingest(payload: dict) -> dict:
    """One conversation from the page: {reservation, guest, checkin, checkout, room, messages:[{id?, dir, text, at?}]}."""
    payload = _normalize(payload)
    guest = payload["guest"]
    reservation = payload["reservation"]
    if not guest and not reservation:
        raise ValueError("Нет гостя и номера брони")
    jid = _jid(reservation, guest)
    n = 0
    msgs = payload.get("messages") or []
    for i, m in enumerate(msgs):
        text = (m.get("text") or "").strip()
        if not text:
            continue
        out = (m.get("dir") or "in") == "out"
        mid = m.get("id") or hashlib.sha1(f"{jid}|{'o' if out else 'i'}|{text}|{m.get('at') or ''}".encode()).hexdigest()[:16]
        at = m.get("at") or datetime.now().isoformat(timespec="seconds")
        r = inbox.store_message({"channel": "bk", "id": f"bk:{mid}", "jid": jid, "phone": "", "from_me": out,
                                 "push_name": None if out else (guest or "Гость Booking.com"), "at": at, "kind": "text", "text": text},
                                notify=not out and i == len(msgs) - 1, history=bool(m.get("history")))
        if r:
            n += 1
    # the stay: remember it on the chat, name the chat after the guest, fill the
    # guest card; the calendar booking is only suggested (see candidates())
    with database.get_conn() as conn:
        c = conn.execute("SELECT id, name FROM inbox_chats WHERE jid = ?", (jid,)).fetchone()
        if c and guest and guest != c["name"]:
            conn.execute("UPDATE inbox_chats SET name = ? WHERE id = ?", (guest, c["id"]))
            inbox._bump(conn)  # noqa: SLF001
        if c:
            conn.execute("INSERT OR REPLACE INTO inbox_ext_meta (chat_id, reservation, guest, checkin, checkout, room, updated_at, guests, total, lang, raw, url) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (c["id"], reservation, guest, payload.get("checkin"), payload.get("checkout"), payload.get("room"),
                          datetime.now().isoformat(timespec="seconds"), payload.get("guests"), payload.get("total"), payload.get("lang"),
                          (payload.get("raw") or "")[:6000], (payload.get("url") or "")[:300]))
    linked = None
    if c:
        try:
            linked = _enrich(c["id"], guest, reservation, payload)
        except Exception:  # noqa: BLE001
            logger.exception("booking enrich failed")
    return {"chat_id": c["id"] if c else None, "stored": n, "booking_id": linked}


_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
           "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def norm_date(s) -> str:
    """'2026-10-25' / 'вс, 25 окт. 2026' / '25 октября 2026' / 'Sun, 25 Oct 2026' / '25.10.2026' → ISO, else ''."""
    s = str(s or "").strip().lower()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})\s+([а-яa-z]{3})[а-яa-z]*\.?\s+(\d{4})", s)
    if m and m.group(2) in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)  # Oct 25, 2026
    if m and m.group(1) in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    return ""


def _normalize(p: dict) -> dict:
    """Whatever version of the extension sent it: clean fields or nothing."""
    out = dict(p or {})
    out["reservation"] = (re.search(r"\d{6,}", str(p.get("reservation") or "")) or [""])[0] if p.get("reservation") else ""
    g = re.sub(r"\s+", " ", str(p.get("guest") or "")).strip()
    out["guest"] = "" if (not g or re.search(r"\d{5,}", g) or len(g) > 80) else g
    out["checkin"], out["checkout"] = norm_date(p.get("checkin")), norm_date(p.get("checkout"))
    room = re.sub(r"\s+", " ", str(p.get("room") or "")).strip()
    out["room"] = room[:80] if room and room not in ("1", "0") else ""
    for k in ("guests", "total", "lang"):
        v = re.sub(r"\s+", " ", str(p.get(k) or "")).strip()
        out[k] = v[:60]
    return out


def _apartment_code(room: str) -> str:
    m = re.search(r"\b([A-Za-zА-Я]{1,4})\s*-?\s*(\d{2,4})\b", room or "")
    return f"{m.group(1).upper()}-{m.group(2)}" if m else ""


def _enrich(chat_id: int, guest: str, reservation: str, p: dict):
    """Guest card from the Booking.com panel + link to the RealtyCalendar booking."""
    from . import crm, crm_ext
    cid = crm.ensure_client_for_chat(chat_id, guest or "Гость Booking.com", "Booking.com")
    lines = [f"Booking.com № {reservation}" if reservation else "Booking.com"]
    if p.get("checkin") and p.get("checkout"):
        lines.append(f"{p['checkin'][8:10]}.{p['checkin'][5:7]}–{p['checkout'][8:10]}.{p['checkout'][5:7]}")
    for key, label in (("guests", "Гостей"), ("total", "Сумма"), ("room", "Номер")):
        if p.get(key):
            lines.append(f"{label}: {p[key]}")
    note = " · ".join(lines)
    with database.get_conn() as conn:
        cur = conn.execute("SELECT notes, lang, source FROM crm_clients WHERE id = ?", (cid,)).fetchone()
        if cur:
            notes = cur["notes"] or ""
            if reservation and reservation not in notes:
                notes = (notes + "\n" if notes else "") + note
            raw_lang = (p.get("lang") or "").lower()
            lang = cur["lang"] or next((code for key, code in (("рус", "RU"), ("russian", "RU"), ("англ", "EN"), ("english", "EN"), ("узб", "UZ"), ("uzbek", "UZ"),
                                                                 ("казах", "KZ"), ("турец", "TR"), ("turkish", "TR"), ("кита", "ZH"), ("chinese", "ZH"), ("араб", "AR"), ("arabic", "AR"),
                                                                 ("немец", "DE"), ("german", "DE"), ("франц", "FR"), ("french", "FR"), ("испан", "ES"), ("spanish", "ES"), ("корей", "KO"), ("korean", "KO"))
                                        if key in raw_lang), raw_lang.replace("на ", "").strip()[:20])
            conn.execute("UPDATE crm_clients SET notes = ?, lang = ?, source = COALESCE(NULLIF(source, ''), 'Booking.com'), updated_at = ? WHERE id = ?",
                         (notes, lang, datetime.now().isoformat(timespec="seconds"), cid))
    # Linking to the calendar booking is half-manual on purpose: Booking.com often
    # shows another room than the calendar and manual edits in RealtyCalendar do not
    # reach Booking — so we only *suggest* candidates, a person confirms in the chat.
    return None


_REASONS = {"res": "№ брони Booking в примечании календаря", "room": "совпали номер и дата заезда",
            "name": "совпали имя гостя и дата заезда", "date": "та же дата заезда", "guest": "то же имя гостя"}


def stay(chat_id: int) -> dict | None:
    """What Booking.com says about the stay (for the chat header / booking picker / «Данные Booking.com»)."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM inbox_ext_meta WHERE chat_id = ?", (chat_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["checkin"], d["checkout"] = norm_date(d.get("checkin")), norm_date(d.get("checkout"))
    return d


def candidates(chat_id: int, limit: int = 6) -> list[dict]:
    """Calendar bookings that look like this Booking.com conversation, best first,
    each with a human reason. Nothing is linked automatically."""
    from . import crm
    m = stay(chat_id)
    if not m:
        return []
    found: dict[int, str] = {}

    def add(rows, reason):
        for r in rows:
            found.setdefault(r["id"], reason)

    with database.get_conn() as conn:
        live = "COALESCE(is_delete, 0) = 0"
        if m.get("reservation"):
            add(conn.execute(f"SELECT id FROM bookings WHERE {live} AND short_notes LIKE ?",  # noqa: S608
                             ("%" + m["reservation"] + "%",)).fetchall(), "res")
        code = _apartment_code(m.get("room") or "")
        if code and m.get("checkin"):
            add(conn.execute(f"SELECT id FROM bookings WHERE {live} AND begin_date = ? AND REPLACE(REPLACE(UPPER(apartment_name), ' ', ''), '-', '') LIKE ?",  # noqa: S608
                             (m["checkin"], "%" + code.replace("-", "") + "%")).fetchall(), "room")
        if m.get("guest") and m.get("checkin"):
            add(conn.execute(f"SELECT id FROM bookings WHERE {live} AND begin_date = ? AND LOWER(client_name) = LOWER(?)",  # noqa: S608
                             (m["checkin"], m["guest"])).fetchall(), "name")
        if m.get("checkin"):
            add(conn.execute(f"SELECT id FROM bookings WHERE {live} AND begin_date = ? ORDER BY apartment_name", (m["checkin"],)).fetchall(), "date")  # noqa: S608
        if m.get("guest") and len(found) < limit:
            last = m["guest"].split()[-1].lower()
            if len(last) >= 3:
                add(conn.execute(f"SELECT id FROM bookings WHERE {live} AND end_date >= date('now', '-30 day') AND LOWER(client_name) LIKE ? ORDER BY begin_date",  # noqa: S608
                                 ("%" + last + "%",)).fetchall(), "guest")
        out = []
        for bid, reason in list(found.items())[:limit]:
            r = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
            if r:
                b = crm._booking_out(dict(r))  # noqa: SLF001
                b["reason"] = _REASONS[reason]
                b["strong"] = reason in ("res", "room", "name")
                out.append(b)
    return out


def enqueue(payload: dict) -> dict:
    """CRM reply for a Booking.com chat → the extension types it in the extranet."""
    jid = payload["jid"]
    with database.get_conn() as conn:
        c = conn.execute("SELECT id, name FROM inbox_chats WHERE jid = ?", (jid,)).fetchone()
        meta = conn.execute("SELECT reservation, guest FROM inbox_ext_meta WHERE chat_id = ?", (c["id"],)).fetchone() if c else None
        if payload.get("media"):
            raise inbox.BridgeError("В Booking.com через расширение уходит только текст")
        rid = conn.execute("INSERT INTO inbox_ext_outbox (chat_id, jid, reservation, guest, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (c["id"] if c else None, jid, (meta["reservation"] if meta else jid.split(":")[1]), (meta["guest"] if meta else (c["name"] if c else "")),
                            payload.get("text") or "", datetime.now().isoformat(timespec="seconds"))).lastrowid
    if not status()["alive"]:
        raise inbox.BridgeError("Расширение Booking.com не на связи: откройте экстранет в Chrome на офисном компьютере")
    return {"id": f"bk:out:{rid}", "status": "pending"}


def outbox() -> list[dict]:
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM inbox_ext_outbox WHERE status = 'pending' ORDER BY id LIMIT 5").fetchall()]
        for r in rows:
            conn.execute("UPDATE inbox_ext_outbox SET status = 'taken', taken_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), r["id"]))
        # anything taken but not confirmed in 3 minutes goes back to pending
        conn.execute("UPDATE inbox_ext_outbox SET status = 'pending' WHERE status = 'taken' AND taken_at < ?",
                     ((datetime.now()).replace(microsecond=0).isoformat() if False else _minutes_ago(3),))
    return rows


def _minutes_ago(n: int) -> str:
    from datetime import timedelta
    return (datetime.now() - timedelta(minutes=n)).isoformat(timespec="seconds")


def outbox_done(oid: int, ok: bool, error: str = "") -> None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM inbox_ext_outbox WHERE id = ?", (oid,)).fetchone()
        if not r:
            return
        conn.execute("UPDATE inbox_ext_outbox SET status = ?, error = ?, done_at = ? WHERE id = ?",
                     ("done" if ok else "failed", (error or "")[:300], datetime.now().isoformat(timespec="seconds"), oid))
    wa_id = f"bk:out:{oid}"
    if ok:
        inbox.store_ack(wa_id, "sent")
    else:
        inbox.mark_failed(wa_id, error or "расширение не смогло отправить")


META_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_ext_meta (
    chat_id INTEGER PRIMARY KEY,
    reservation TEXT,
    guest TEXT,
    checkin TEXT,
    checkout TEXT,
    room TEXT,
    updated_at TEXT,
    guests TEXT,
    total TEXT,
    lang TEXT,
    raw TEXT,
    url TEXT
);
"""
SCHEMA += META_SCHEMA


def migrate_meta() -> None:
    with database.get_conn() as conn:
        conn.executescript(META_SCHEMA)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(inbox_ext_meta)").fetchall()]
        for col in ("guests", "total", "lang", "raw", "url"):
            if col not in cols:
                conn.execute(f"ALTER TABLE inbox_ext_meta ADD COLUMN {col} TEXT")
