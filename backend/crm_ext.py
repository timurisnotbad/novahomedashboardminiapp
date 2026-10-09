"""CRM, part 2: bookings as the hub (linked chats, guest card), guest history
with Excel export, revenue by channel, and the auto-message rules the owner
tunes himself (see run_auto_rules — called by the scheduler every 5 minutes
and by the inbox on incoming messages).

Everything reads the same tables the dashboard fills: `bookings` (RC mirror),
inbox chats/messages, crm_clients.
"""
import io
import logging
import re
from datetime import date, datetime, timedelta

from . import config, crm, database, inbox

logger = logging.getLogger("nova.crm2")

SCHEMA = """
-- a chat (any channel) pinned to a booking by the team
CREATE TABLE IF NOT EXISTS crm_booking_chats (
    booking_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    linked_by TEXT,
    linked_at TEXT,
    PRIMARY KEY (booking_id, chat_id)
);

-- auto-message rules: trigger + offset + template, switchable by the owner
CREATE TABLE IF NOT EXISTS crm_auto_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER DEFAULT 0,
    trigger TEXT NOT NULL,       -- new_booking | before_checkin | checkin_day | checkout_day | after_checkout | first_message | off_hours
    offset_days INTEGER DEFAULT 0,
    at_time TEXT DEFAULT '10:00',
    hours_from TEXT,             -- off_hours: work day is hours_from..hours_to (outside = off hours)
    hours_to TEXT,
    channel TEXT DEFAULT 'auto', -- auto | wa | wac | tg | tgbot | ig
    sources TEXT,                -- comma list of booking sources to match ('' = all)
    only_if_chat INTEGER DEFAULT 1,  -- don't start a new WhatsApp chat, only existing ones
    text TEXT NOT NULL,
    sort INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_auto_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER,
    rule_name TEXT,
    booking_id INTEGER,
    chat_id INTEGER,
    chat_title TEXT,
    text TEXT,
    status TEXT,                 -- sent | skipped | failed
    error TEXT,
    at TEXT,
    dedupe TEXT UNIQUE           -- rule:booking or rule:chat:day — one send per event
);

CREATE INDEX IF NOT EXISTS idx_bchats_chat ON crm_booking_chats(chat_id);
CREATE INDEX IF NOT EXISTS idx_autolog_at ON crm_auto_log(at);
"""

TRIGGERS = {
    "new_booking": "Новая бронь появилась в календаре",
    "before_checkin": "За N дней до заезда",
    "checkin_day": "В день заезда",
    "checkout_day": "В день выезда",
    "after_checkout": "Через N дней после выезда",
    "first_message": "Первое сообщение от нового гостя",
    "off_hours": "Сообщение в нерабочее время",
}
AUTHOR = "Автоответ"


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM crm_auto_rules").fetchone()[0] == 0:
            # examples, all switched OFF: the owner enables and edits them in CRM → Автосообщения
            now = crm._now()
            for i, (name, trig, off, at, text) in enumerate([
                ("Инструкция заезда", "before_checkin", 1, "12:00",
                 "{имя}, здравствуйте! Завтра ждём вас в Nova Home, апартаменты {объект}. Заезд с 15:00. "
                 "Адрес и код от двери пришлём утром в день заезда. Если нужна встреча — напишите время прибытия."),
                ("Напоминание о выезде", "checkout_day", 0, "09:30",
                 "{имя}, доброе утро! Сегодня выезд до 12:00. Пожалуйста, оставьте ключи на столе и напишите, когда освободите квартиру — вернём депозит."),
                ("Просьба об отзыве", "after_checkout", 1, "11:00",
                 "{имя}, спасибо, что выбрали Nova Home! Будем благодарны за отзыв — это очень помогает нам. Ждём вас снова!"),
                ("Ответ в нерабочее время", "off_hours", 0, "",
                 "Здравствуйте! Мы получили ваше сообщение и ответим с 09:00. Если вопрос срочный — позвоните по номеру в профиле."),
            ]):
                conn.execute(
                    "INSERT INTO crm_auto_rules (name, enabled, trigger, offset_days, at_time, hours_from, hours_to, channel, sources, "
                    "only_if_chat, text, sort, created_at, updated_at) VALUES (?, 0, ?, ?, ?, ?, ?, 'auto', '', 1, ?, ?, ?, ?)",
                    (name, trig, off, at, "09:00" if trig == "off_hours" else None, "22:00" if trig == "off_hours" else None,
                     text, i, now, now))


# ---------------------------------------------------------------------------
# Bookings as the hub
# ---------------------------------------------------------------------------
def _booking(conn, bid: int):
    r = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
    return crm._booking_out(dict(r)) if r else None


def booking_card(bid: int) -> dict | None:
    """Everything the team needs when clicking a booking: guest card, linked
    chats (pinned + suggested by phone), deal, auto-message history."""
    with database.get_conn() as conn:
        b = _booking(conn, bid)
        if not b:
            return None
        pinned = {r["chat_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM crm_booking_chats WHERE booking_id = ?", (bid,)).fetchall()}
        chats = []
        for cid, link in pinned.items():
            c = inbox.get_chat(cid)
            if c:
                c["pinned"] = True
                c["linked_by"] = link["linked_by"]
                chats.append(c)
        if b["phone"] and len(b["phone"]) >= 7:
            for r in conn.execute("SELECT id FROM inbox_chats WHERE phone LIKE ? ORDER BY last_at DESC",
                                  ("%" + b["phone"][-9:],)).fetchall():
                if r["id"] not in pinned:
                    c = inbox.get_chat(r["id"])
                    if c:
                        c["pinned"] = False
                        chats.append(c)
        deal = conn.execute("SELECT id, title, stage_id FROM crm_deals WHERE booking_id = ?", (bid,)).fetchone()
        auto = [dict(r) for r in conn.execute(
            "SELECT * FROM crm_auto_log WHERE booking_id = ? ORDER BY id DESC LIMIT 20", (bid,)).fetchall()]
        checklist_done = bool(conn.execute("SELECT 1 FROM crm_booking_checklist WHERE booking_id = ?", (bid,)).fetchone())
    cid = crm.ensure_client(b["phone"] or "", b["guest"] or "", b["source"]) if b["phone"] else None
    client = crm.get_client(cid) if cid else None
    b["client"] = client and {k: client[k] for k in ("id", "name", "phone", "email", "source", "notes", "fields", "status")}
    b["history"] = client_history(b["phone"]) if b["phone"] else None
    b["chats"] = chats
    crm._contact_states([b])  # noqa: SLF001
    b["deal"] = dict(deal) if deal else None
    b["auto_log"] = auto
    b["tasks"] = crm.tasks(booking_id=bid)
    b["checklist_done"] = checklist_done
    return b


def chat_bookings(chat_id: int) -> dict:
    """What the chat header offers: pinned bookings + the guest's bookings by phone."""
    chat = inbox.get_chat(chat_id)
    if not chat:
        raise ValueError("Чат не найден")
    today = date.today().isoformat()
    with database.get_conn() as conn:
        pinned_ids = [r["booking_id"] for r in conn.execute(
            "SELECT booking_id FROM crm_booking_chats WHERE chat_id = ? ORDER BY linked_at DESC", (chat_id,)).fetchall()]
        pinned = [b for b in (_booking(conn, i) for i in pinned_ids) if b]
    phone = chat.get("phone") or ""
    suggested = [b for b in (crm.client_bookings(phone) if len(phone) >= 7 else []) if b["id"] not in pinned_ids]
    suggested.sort(key=lambda b: (0 if b["checkout"] >= today else 1, b["checkin"] if b["checkout"] >= today else "0000"))
    for b in pinned + suggested:
        b["when"] = "now" if b["checkin"] <= today <= b["checkout"] else "next" if b["checkin"] > today else "past"
    return {"pinned": pinned, "suggested": suggested[:10]}


def bookings_search(q: str, limit: int = 20) -> list[dict]:
    """Search bookings by guest, phone, apartment or date (DD.MM) — for «Привязать бронь» in a chat."""
    q = (q or "").strip()
    ql, qd = q.lower(), crm.digits(q)
    today = date.today().isoformat()
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bookings WHERE COALESCE(is_delete, 0) = 0 AND end_date >= ? ORDER BY begin_date LIMIT 2000",
            ((date.today() - timedelta(days=90)).isoformat(),)).fetchall()]
    out = []
    for r in rows:
        hay = f"{r.get('client_name') or ''} {r.get('apartment_name') or ''} {crm._dm(r['begin_date'])} {crm._dm(r['end_date'])}".lower()  # noqa: SLF001
        if not q or ql in hay or (len(qd) >= 4 and qd in crm.digits(r.get("client_phone"))):
            b = crm._booking_out(r)  # noqa: SLF001
            b["when"] = "now" if b["checkin"] <= today <= b["checkout"] else "next" if b["checkin"] > today else "past"
            out.append(b)
    out.sort(key=lambda b: (0 if b["when"] == "now" else 1 if b["when"] == "next" else 2, b["checkin"] if b["when"] != "past" else "", -int(b["id"]) if b["when"] == "past" else 0))
    return out[:limit]


def link_chat(bid: int, chat_id: int, who: str) -> None:
    with database.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM bookings WHERE id = ?", (bid,)).fetchone():
            raise ValueError("Бронь не найдена")
        if not conn.execute("SELECT 1 FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone():
            raise ValueError("Чат не найден")
        conn.execute("INSERT OR REPLACE INTO crm_booking_chats (booking_id, chat_id, linked_by, linked_at) VALUES (?, ?, ?, ?)",
                     (bid, chat_id, who, crm._now()))
        # the chat's open deal is now this booking's deal
        bk = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
        for r in conn.execute("SELECT d.id FROM crm_deal_chats l JOIN crm_deals d ON d.id = l.deal_id JOIN crm_stages s ON s.id = d.stage_id "
                              "WHERE l.chat_id = ? AND d.booking_id IS NULL AND s.kind = 'open'", (chat_id,)).fetchall():
            conn.execute("UPDATE crm_deals SET booking_id = ?, apartment = ?, checkin = ?, checkout = ?, amount = ?, updated_at = ? WHERE id = ?",
                         (bid, bk["apartment_name"], bk["begin_date"], bk["end_date"], bk["amount"], crm._now(), r["id"]))
        # the chat's client card (Instagram/Telegram: no phone) merges into the
        # guest's card from the booking, or takes the booking's phone
        b = conn.execute("SELECT client_name, client_phone FROM bookings WHERE id = ?", (bid,)).fetchone()
        c = conn.execute("SELECT id, phone, notes FROM crm_clients WHERE chat_id = ?", (chat_id,)).fetchone()
        phone = crm.digits(b["client_phone"])
        if c and not c["phone"] and len(phone) >= 7:
            other = conn.execute("SELECT id, notes FROM crm_clients WHERE phone LIKE ? AND id != ?", ("%" + phone[-9:], c["id"])).fetchone()
            if other:
                notes = "\n".join(x for x in (other["notes"], c["notes"]) if x)
                conn.execute("UPDATE crm_clients SET chat_id = COALESCE(chat_id, ?), notes = ?, updated_at = ? WHERE id = ?",
                             (chat_id, notes, crm._now(), other["id"]))
                conn.execute("UPDATE crm_deals SET client_id = ? WHERE client_id = ?", (other["id"], c["id"]))
                conn.execute("UPDATE crm_tasks SET client_id = ? WHERE client_id = ?", (other["id"], c["id"]))
                conn.execute("DELETE FROM crm_clients WHERE id = ?", (c["id"],))
            else:
                conn.execute("UPDATE crm_clients SET phone = ?, name = CASE WHEN name LIKE 'Гость%' OR name = '' THEN ? ELSE name END, updated_at = ? WHERE id = ?",
                             (phone, b["client_name"] or "", crm._now(), c["id"]))
        conn.execute("UPDATE inbox_chats SET rev = (SELECT rev FROM inbox_rev WHERE id = 1) + 1 WHERE id = ?", (chat_id,))
        conn.execute("UPDATE inbox_rev SET rev = rev + 1 WHERE id = 1")
    inbox._pin_cache["at"] = 0  # noqa: SLF001 — chat header shows the booking right away


def unlink_chat(bid: int, chat_id: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_booking_chats WHERE booking_id = ? AND chat_id = ?", (bid, chat_id))
        conn.execute("UPDATE inbox_chats SET rev = (SELECT rev FROM inbox_rev WHERE id = 1) + 1 WHERE id = ?", (chat_id,))
        conn.execute("UPDATE inbox_rev SET rev = rev + 1 WHERE id = 1")
    inbox._pin_cache["at"] = 0  # noqa: SLF001


def pinned_booking_for_chat(chat_id: int) -> dict | None:
    """The booking the team pinned to a chat — current/next first, else the latest."""
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT b.* FROM crm_booking_chats l JOIN bookings b ON b.id = l.booking_id "
            "WHERE l.chat_id = ? AND COALESCE(b.is_delete, 0) = 0", (chat_id,)).fetchall()]
    if not rows:
        return None
    today = date.today().isoformat()
    cur = [r for r in rows if r["begin_date"] <= today <= r["end_date"]]
    nxt = sorted((r for r in rows if r["begin_date"] > today), key=lambda r: r["begin_date"])
    past = sorted(rows, key=lambda r: r["end_date"], reverse=True)
    b = dict((cur or nxt or past)[0])
    b["when"] = "now" if cur else "next" if nxt else "past"
    return b


def chats_for_booking(bid: int) -> list[int]:
    with database.get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT chat_id FROM crm_booking_chats WHERE booking_id = ?", (bid,)).fetchall()]


# ---------------------------------------------------------------------------
# Guest history (from the RC mirror) and Excel export
# ---------------------------------------------------------------------------
def _history_index() -> dict[str, dict]:
    """phone(last 9) -> visits, nights, amount, first/last stay, apartments."""
    idx: dict[str, dict] = {}
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM bookings WHERE COALESCE(is_delete, 0) = 0 AND client_phone IS NOT NULL "
                            "AND client_phone != '' ORDER BY begin_date").fetchall()
    today = date.today().isoformat()
    for r in rows:
        d = crm.digits(r["client_phone"])
        if len(d) < 7:
            continue
        h = idx.setdefault(d[-9:], {"visits": 0, "nights": 0, "amount": 0.0, "debt": 0.0, "first": None, "last": None,
                                    "apartments": [], "sources": [], "upcoming": 0, "names": []})
        if r["begin_date"] > today:
            h["upcoming"] += 1
        else:
            h["visits"] += 1
        h["nights"] += r["days_count"] or 0
        h["amount"] += r["amount"] or 0
        h["debt"] += r["debt"] or 0
        h["first"] = h["first"] or r["begin_date"]
        h["last"] = r["end_date"]
        if r["apartment_name"] and r["apartment_name"] not in h["apartments"]:
            h["apartments"].append(r["apartment_name"])
        src = config.SOURCE_NAMES.get(r["source_id"], "Другое")
        if src not in h["sources"]:
            h["sources"].append(src)
        if r["client_name"] and r["client_name"] not in h["names"]:
            h["names"].append(r["client_name"])
    return idx


def client_history(phone: str) -> dict | None:
    d = crm.digits(phone)
    return _history_index().get(d[-9:]) if len(d) >= 7 else None


def clients_with_history(q: str = "") -> list[dict]:
    idx = _history_index()
    out = []
    for c in crm.clients(q, limit=5000):
        h = idx.get((c["phone"] or "")[-9:]) if c["phone"] else None
        c["visits"] = h["visits"] if h else 0
        c["nights"] = h["nights"] if h else 0
        c["amount"] = round(h["amount"], 2) if h else 0
        c["last_stay"] = h["last"] if h else None
        out.append(c)
    return out


def export_clients_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    fields = crm.fields()
    idx = _history_index()
    with database.get_conn() as conn:
        chans = {}
        for r in conn.execute("SELECT id, channel FROM inbox_chats").fetchall():
            chans[r["id"]] = inbox.CHANNELS.get(r["channel"] or "wa", r["channel"])
        chats_by_phone: dict[str, set] = {}
        for r in conn.execute("SELECT phone, channel FROM inbox_chats WHERE phone != ''").fetchall():
            chats_by_phone.setdefault(r["phone"][-9:], set()).add(inbox.CHANNELS.get(r["channel"] or "wa", r["channel"]))
    wb = Workbook()
    ws = wb.active
    ws.title = "Клиенты"
    head = ["Имя", "Телефон", "Email", "Источник", "Каналы связи", "Визитов", "Будущих броней", "Ночей", "Сумма, $", "Долг, $",
            "Первый заезд", "Последний выезд", "Квартиры", "Имена в бронях", "Комментарии"] + [f["name"] for f in fields] + ["Создан"]
    ws.append(head)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for c in crm.clients(limit=100000):
        key = (c["phone"] or "")[-9:]
        h = idx.get(key) if c["phone"] else None
        ch = set(chats_by_phone.get(key, set())) if c["phone"] else set()
        if c.get("chat_id") in chans:
            ch.add(chans[c["chat_id"]])
        row = [c["name"], ("+" + c["phone"]) if c["phone"] else "", c["email"] or "", c["source"] or "", ", ".join(sorted(ch)),
               h["visits"] if h else 0, h["upcoming"] if h else 0, h["nights"] if h else 0, round(h["amount"], 2) if h else 0,
               round(h["debt"], 2) if h else 0, h["first"] if h else "", h["last"] if h else "",
               ", ".join(h["apartments"]) if h else "", ", ".join(h["names"]) if h else "", c["notes"] or ""]
        for f in fields:
            v = (c.get("fields") or {}).get(str(f["id"]))
            row.append("да" if f["type"] == "checkbox" and v else ("" if v is None or v is False else v))
        row.append((c["created_at"] or "")[:10])
        ws.append(row)
    for i, _ in enumerate(head, 1):
        ws.column_dimensions[get_column_letter(i)].width = 16 if i not in (1, 15) else 28
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    # second sheet: every stay
    ws2 = wb.create_sheet("Брони")
    ws2.append(["Квартира", "Заезд", "Выезд", "Ночей", "Гость", "Телефон", "Источник", "Сумма, $", "Долг, $", "Заметки RC"])
    for cell in ws2[1]:
        cell.font = Font(bold=True)
    with database.get_conn() as conn:
        for r in conn.execute("SELECT * FROM bookings WHERE COALESCE(is_delete, 0) = 0 ORDER BY begin_date DESC").fetchall():
            ws2.append([r["apartment_name"], r["begin_date"], r["end_date"], r["days_count"], r["client_name"],
                        ("+" + crm.digits(r["client_phone"])) if r["client_phone"] else "", config.SOURCE_NAMES.get(r["source_id"], "Другое"),
                        r["amount"], r["debt"], r["short_notes"]])
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = ws2.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Revenue by channel (RC mirror; refreshed by every sync)
# ---------------------------------------------------------------------------
def revenue(month: str) -> dict:
    """Bookings by check-in month, grouped by source; plus 12-month trend."""
    y, m = (int(x) for x in month.split("-")[:2])
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bookings WHERE COALESCE(is_delete, 0) = 0 AND begin_date >= ? AND begin_date < ?",
            (start.isoformat(), end.isoformat())).fetchall()]
        trend_rows = [dict(r) for r in conn.execute(
            "SELECT substr(begin_date, 1, 7) AS ym, source_id, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount, "
            "COALESCE(SUM(days_count), 0) AS nights FROM bookings WHERE COALESCE(is_delete, 0) = 0 AND begin_date >= ? "
            "GROUP BY ym, source_id ORDER BY ym", ((start - timedelta(days=365)).replace(day=1).isoformat(),)).fetchall()]
        apts = conn.execute("SELECT COUNT(DISTINCT apartment_name) FROM bookings WHERE COALESCE(is_delete, 0) = 0").fetchone()[0] or len(config.APARTMENTS)
    by_src: dict[str, dict] = {}
    for r in rows:
        s = config.SOURCE_NAMES.get(r["source_id"], "Другое")
        d = by_src.setdefault(s, {"source": s, "bookings": 0, "nights": 0, "amount": 0.0, "debt": 0.0})
        d["bookings"] += 1
        d["nights"] += r["days_count"] or 0
        d["amount"] += r["amount"] or 0
        d["debt"] += r["debt"] or 0
    total = {"bookings": len(rows), "nights": sum(r["days_count"] or 0 for r in rows), "amount": sum(r["amount"] or 0 for r in rows),
             "debt": sum(r["debt"] or 0 for r in rows)}
    days = (end - start).days
    total["occupancy"] = round(100 * total["nights"] / (days * apts), 1) if apts else 0
    total["adr"] = round(total["amount"] / total["nights"], 2) if total["nights"] else 0
    trend: dict[str, dict] = {}
    for r in trend_rows:
        t = trend.setdefault(r["ym"], {"month": r["ym"], "amount": 0.0, "bookings": 0, "nights": 0, "by_source": {}})
        s = config.SOURCE_NAMES.get(r["source_id"], "Другое")
        t["amount"] += r["amount"]
        t["bookings"] += r["n"]
        t["nights"] += r["nights"]
        t["by_source"][s] = round(t["by_source"].get(s, 0) + r["amount"], 2)
    by_apt: dict[str, dict] = {}
    for r in rows:
        a = by_apt.setdefault(r["apartment_name"] or "?", {"apartment": r["apartment_name"], "bookings": 0, "nights": 0, "amount": 0.0})
        a["bookings"] += 1
        a["nights"] += r["days_count"] or 0
        a["amount"] += r["amount"] or 0
    return {"month": month, "total": total, "by_source": sorted(by_src.values(), key=lambda x: -x["amount"]),
            "by_apartment": sorted(by_apt.values(), key=lambda x: -x["amount"]),
            "trend": sorted(trend.values(), key=lambda x: x["month"]), "last_sync": database.last_sync(), "apartments": apts}


# Deal stages the booking drives (matched by name, within the deal's pipeline)
FLOW_STAGES = {"unpaid": ("Ожидает оплаты",), "booked": ("Забронировано", "Бронь подтверждена"), "living": ("Заселён", "Живёт"),
               "out": ("Выехал", "Выезд"), "cancelled": ("Отменена", "Отмена брони", "Отменено")}
FLOW_DEFAULT_ACTIONS = {  # one-time defaults so the «scenario» works out of the box; editable in Воронки
    "Выехал": [("task", "Попросить отзыв и напомнить о себе: скидка на следующий визит", 1, "11:00"),
               ("task", "Повторное касание: спросить о планах, предложить бронь", 60, "11:00")],
    "Отказ": [("task", "Вернуться к гостю: уточнить, актуален ли запрос", 14, "11:00")],
    "Отменена": [("task", "Бронь отменена: узнать причину, предложить другие даты или квартиру", 1, "11:00")],
}


def ensure_flow_stages() -> None:
    """The default pipeline gets «Выехал» (won, after the stay) once; nothing else is touched."""
    with database.get_conn() as conn:
        p = conn.execute("SELECT id FROM crm_pipelines ORDER BY sort, id LIMIT 1").fetchone()
        if not p:
            return
        done = {r["key"] for r in conn.execute("SELECT key FROM crm_settings WHERE key IN ('flow_stage_v1', 'flow_actions_v1')").fetchall()}
        names = {r["name"]: r for r in conn.execute("SELECT * FROM crm_stages WHERE pipeline_id = ?", (p["id"],)).fetchall()}
        if "flow_stage_v1" not in done:
            if not any(n in names for n in FLOW_STAGES["out"]):
                lost = [r for r in names.values() if r["kind"] == "lost"]
                sort = (min(r["sort"] for r in lost) if lost else max((r["sort"] for r in names.values()), default=0) + 1)
                conn.execute("UPDATE crm_stages SET sort = sort + 1 WHERE pipeline_id = ? AND sort >= ?", (p["id"], sort))
                conn.execute("INSERT INTO crm_stages (pipeline_id, name, sort, color, kind) VALUES (?, 'Выехал', ?, '#6B7280', 'won')", (p["id"], sort))
            conn.execute("INSERT OR REPLACE INTO crm_settings (key, value) VALUES ('flow_stage_v1', '1')")
        if "flow_stage_v2" not in {r["key"] for r in conn.execute("SELECT key FROM crm_settings WHERE key = 'flow_stage_v2'").fetchall()}:
            names = {r["name"]: r for r in conn.execute("SELECT * FROM crm_stages WHERE pipeline_id = ?", (p["id"],)).fetchall()}
            if not any(n in names for n in FLOW_STAGES["cancelled"]):
                sort = max((r["sort"] for r in names.values()), default=0) + 1
                conn.execute("INSERT INTO crm_stages (pipeline_id, name, sort, color, kind) VALUES (?, 'Отменена', ?, '#F97316', 'lost')", (p["id"], sort))
            st = conn.execute("SELECT id FROM crm_stages WHERE pipeline_id = ? AND name = 'Отменена'", (p["id"],)).fetchone()
            if st and not conn.execute("SELECT 1 FROM crm_stage_actions WHERE stage_id = ?", (st["id"],)).fetchone():
                for i, (kind, text, days, at) in enumerate(FLOW_DEFAULT_ACTIONS["Отменена"]):
                    conn.execute("INSERT INTO crm_stage_actions (stage_id, kind, text, days, at_time, sort) VALUES (?, ?, ?, ?, ?, ?)",
                                 (st["id"], kind, text, days, at, i))
            conn.execute("INSERT OR REPLACE INTO crm_settings (key, value) VALUES ('flow_stage_v2', '1')")
        if "flow_actions_v1" not in done:
            for r in conn.execute("SELECT * FROM crm_stages WHERE pipeline_id = ?", (p["id"],)).fetchall():
                acts = FLOW_DEFAULT_ACTIONS.get(r["name"])
                if acts and not conn.execute("SELECT 1 FROM crm_stage_actions WHERE stage_id = ?", (r["id"],)).fetchone():
                    for i, (kind, text, days, at) in enumerate(acts):
                        conn.execute("INSERT INTO crm_stage_actions (stage_id, kind, text, days, at_time, sort) VALUES (?, ?, ?, ?, ?, ?)",
                                     (r["id"], kind, text, days, at, i))
            conn.execute("INSERT OR REPLACE INTO crm_settings (key, value) VALUES ('flow_actions_v1', '1')")


def booking_flow() -> int:
    """Deals with a booking move by themselves: unpaid before check-in → «Ожидает оплаты»,
    paid/prepaid → «Забронировано», during the stay → «Заселён», after → «Выехал».
    Stage actions (messages, tasks) fire on every move — that is the scenario.
    Deals in a «lost» stage are left alone. Off with the «booking_flow» setting."""
    if crm.get_setting("booking_flow", "1") != "1":
        return 0
    today = date.today().isoformat()
    admins = [u for u in crm.list_users() if u["role"] == "admin" and u["active"]]
    uid = admins[0]["id"] if admins else 0
    moved = 0
    with database.get_conn() as conn:
        deals = [dict(r) for r in conn.execute(
            "SELECT d.id, d.stage_id, d.pipeline_id, d.client_id, d.checkin AS d_checkin, d.checkout AS d_checkout, d.apartment AS d_apartment, d.notes, "
            "b.id AS b_id, b.begin_date, b.end_date, b.status AS b_status, b.amount, b.debt, b.prepayment, "
            "b.prepayment_progress, COALESCE(b.is_delete, 0) AS del, s.kind, s.name AS stage_name FROM crm_deals d "
            "LEFT JOIN bookings b ON b.id = d.booking_id JOIN crm_stages s ON s.id = d.stage_id WHERE d.booking_id IS NOT NULL").fetchall()]
        synced = database.last_sync()
        stages = {}
        for r in conn.execute("SELECT id, pipeline_id, name FROM crm_stages").fetchall():
            stages.setdefault(r["pipeline_id"], {})[r["name"]] = r["id"]
    cancelled_clients: set = set()
    for d in deals:
        gone = (d["b_id"] is None or d["del"] or d["b_status"] in ("cancelled", "canceled"))
        horizon = (date.today() + timedelta(days=config.SYNC_DAYS_AHEAD)).isoformat()
        if gone and synced and (d["d_checkout"] or "9999") >= today and (d["d_checkin"] or "0000") <= horizon:
            # the booking vanished from the calendar before check-out = cancelled there
            if d["kind"] == "lost":
                continue
            target = next((stages.get(d["pipeline_id"], {}).get(n) for n in FLOW_STAGES["cancelled"] if stages.get(d["pipeline_id"], {}).get(n)), None)
            if target and target != d["stage_id"]:
                try:
                    note = (d["notes"] or "").rstrip()
                    note = (note + "\n" if note else "") + f"Бронь {d['d_apartment'] or ''} {d['d_checkin'] or ''}–{d['d_checkout'] or ''} отменена в календаре {today}"
                    crm.save_deal(d["id"], {"stage_id": target, "notes": note}, uid)
                    moved += 1
                    if d["client_id"]:
                        cancelled_clients.add(d["client_id"])
                except Exception:  # noqa: BLE001
                    logger.exception("booking flow (cancel) failed for deal %s", d["id"])
            continue
        if gone or d["kind"] == "lost":
            continue
        if d["end_date"] <= today:
            key = "out"
        elif d["begin_date"] <= today:
            key = "living"
        else:
            key = "unpaid" if crm._pay_state(d) == "unpaid" else "booked"  # noqa: SLF001
        target = next((stages.get(d["pipeline_id"], {}).get(n) for n in FLOW_STAGES[key] if stages.get(d["pipeline_id"], {}).get(n)), None)
        if not target or target == d["stage_id"]:
            continue
        try:
            crm.save_deal(d["id"], {"stage_id": target}, uid)
            moved += 1
            if key == "out" and d["client_id"]:  # a returning guest becomes «Постоянник»
                c = crm.get_client(d["client_id"])
                if c and len(c.get("bookings") or []) >= 2 and (c.get("status") or "") not in ("VIP", "Не беспокоить"):
                    crm.set_client_status(d["client_id"], "Постоянник")
        except Exception:  # noqa: BLE001
            logger.exception("booking flow failed for deal %s", d["id"])
    try:
        crm.refresh_client_statuses(cancelled_clients)
    except Exception:  # noqa: BLE001
        logger.exception("client statuses refresh failed")
    return moved


def sync_deals_from_bookings() -> int:
    """Deals created from bookings follow the booking: amount, dates, apartment
    change in RealtyCalendar → the deal updates on the next sync."""
    n = 0
    with database.get_conn() as conn:
        for d in conn.execute("SELECT d.id, d.amount, d.checkin, d.checkout, d.apartment, b.amount AS b_amount, b.begin_date, "
                              "b.end_date, b.apartment_name, COALESCE(b.is_delete, 0) AS del FROM crm_deals d "
                              "JOIN bookings b ON b.id = d.booking_id").fetchall():
            if (d["amount"], d["checkin"], d["checkout"], d["apartment"]) != (d["b_amount"], d["begin_date"], d["end_date"], d["apartment_name"]):
                conn.execute("UPDATE crm_deals SET amount = ?, checkin = ?, checkout = ?, apartment = ?, updated_at = ? WHERE id = ?",
                             (d["b_amount"], d["begin_date"], d["end_date"], d["apartment_name"], crm._now(), d["id"]))
                n += 1
    return n


# ---------------------------------------------------------------------------
# Auto-message rules
# ---------------------------------------------------------------------------
def rules() -> list[dict]:
    with database.get_conn() as conn:
        out = []
        for r in conn.execute("SELECT * FROM crm_auto_rules ORDER BY sort, id").fetchall():
            d = dict(r)
            d["enabled"] = bool(d["enabled"])
            d["only_if_chat"] = bool(d["only_if_chat"])
            d["trigger_name"] = TRIGGERS.get(d["trigger"], d["trigger"])
            d["sent"] = conn.execute("SELECT COUNT(*) FROM crm_auto_log WHERE rule_id = ? AND status = 'sent'", (d["id"],)).fetchone()[0]
            out.append(d)
        return out


def save_rule(rid: int | None, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    trig = data.get("trigger")
    text = (data.get("text") or "").strip()
    if not name or trig not in TRIGGERS or not text:
        raise ValueError("Название, событие и текст обязательны")
    at_time = (data.get("at_time") or "10:00")[:5]
    if not re.fullmatch(r"\d\d:\d\d", at_time):
        at_time = "10:00"
    vals = (name, 1 if data.get("enabled") else 0, trig, int(data.get("offset_days") or 0), at_time,
            (data.get("hours_from") or None), (data.get("hours_to") or None),
            data.get("channel") if data.get("channel") in ("auto", "wa", "wac", "tg", "tgbot", "ig") else "auto",
            ",".join(s.strip() for s in (data.get("sources") or "").split(",") if s.strip()),
            1 if data.get("only_if_chat", True) else 0, text, crm._now())
    with database.get_conn() as conn:
        if rid:
            conn.execute("UPDATE crm_auto_rules SET name=?, enabled=?, trigger=?, offset_days=?, at_time=?, hours_from=?, hours_to=?, "
                         "channel=?, sources=?, only_if_chat=?, text=?, updated_at=? WHERE id=?", (*vals, rid))
        else:
            rid = conn.execute("INSERT INTO crm_auto_rules (name, enabled, trigger, offset_days, at_time, hours_from, hours_to, channel, "
                               "sources, only_if_chat, text, updated_at, sort, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
                               "(SELECT COALESCE(MAX(sort), 0) + 1 FROM crm_auto_rules), ?)", (*vals, crm._now())).lastrowid
    return next(r for r in rules() if r["id"] == rid)


def toggle_rule(rid: int, enabled: bool) -> None:
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_auto_rules SET enabled = ?, updated_at = ? WHERE id = ?", (1 if enabled else 0, crm._now(), rid))


def delete_rule(rid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_auto_rules WHERE id = ?", (rid,))


def auto_log(limit: int = 100) -> list[dict]:
    with database.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM crm_auto_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def fill(text: str, booking: dict | None, chat: dict | None) -> str:
    def d(s):
        if not s:
            return ""
        dt = date.fromisoformat(s[:10])
        return f"{dt.day} {RU_MONTHS[dt.month - 1]}"
    name = ""
    if booking and booking.get("guest"):
        name = booking["guest"]
    elif chat:
        name = chat.get("name") or chat.get("push_name") or ""
    name = re.sub(r"^\+?\d+$", "", name).split(" ")[0]
    b = booking or {}
    t_in = (b.get("arrival_time") or "14:00")[:5]
    t_out = (b.get("departure_time") or "11:00")[:5]
    vals = {"имя": name, "объект": b.get("apartment") or "", "заезд": d(b.get("checkin")), "выезд": d(b.get("checkout")),
            "дата заезда": d(b.get("checkin")), "дата выезда": d(b.get("checkout")),
            "время заезда": t_in, "время выезда": t_out, "заезд время": f"{d(b.get('checkin'))} в {t_in}" if b.get("checkin") else "",
            "выезд время": f"{d(b.get('checkout'))} до {t_out}" if b.get("checkout") else "",
            "ночей": str(b.get("nights") or ""), "сумма": f"{b.get('amount') or 0:g}", "долг": f"{b.get('debt') or 0:g}",
            "оплачено": f"{b.get('paid') or 0:g}", "телефон": ("+" + b["phone"]) if b.get("phone") else ""}
    return re.sub(r"\{(имя|объект|заезд|выезд|время заезда|время выезда|заезд время|выезд время|дата заезда|дата выезда|ночей|сумма|долг|оплачено|телефон)\}", lambda m: vals.get(m.group(1), m.group(0)), text)


def _log(conn, rule, booking_id, chat, text, status, error, dedupe) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO crm_auto_log (rule_id, rule_name, booking_id, chat_id, chat_title, text, status, error, at, dedupe) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rule["id"], rule["name"], booking_id, chat and chat["id"], chat and chat.get("title"), text, status, error, crm._now(), dedupe))
    return cur.rowcount > 0


def _chat_for_booking(b: dict, rule: dict) -> tuple[dict | None, str]:
    """Pinned chat first, then any chat with the guest's phone (preferring the
    rule's channel), else — if allowed — a fresh WhatsApp chat."""
    want = rule["channel"]
    cands = []
    for cid in chats_for_booking(b["id"]):
        c = inbox.get_chat(cid)
        if c:
            cands.append(c)
    if b.get("phone") and len(b["phone"]) >= 7:
        with database.get_conn() as conn:
            for r in conn.execute("SELECT id FROM inbox_chats WHERE phone LIKE ? ORDER BY last_at DESC", ("%" + b["phone"][-9:],)).fetchall():
                if all(c["id"] != r["id"] for c in cands):
                    c = inbox.get_chat(r["id"])
                    if c:
                        cands.append(c)
    if want != "auto":
        pref = [c for c in cands if c["channel"] == want or (want in ("wa", "wac") and c["channel"] in ("wa", "wac"))]
        cands = pref or ([] if rule["only_if_chat"] else cands)
    if cands:
        return cands[0], ""
    if rule["only_if_chat"] or want in ("ig", "tg", "tgbot"):
        return None, "нет чата с гостем"
    if not b.get("phone"):
        return None, "у брони нет телефона"
    try:
        if want == "wac" or (want == "auto" and inbox.bridge_status().get("status") != "connected" and config.WA_CLOUD_TOKEN):
            with database.get_conn() as conn:
                cid = inbox._ensure_chat(conn, f"{b['phone']}@s.whatsapp.net", channel="wac")  # noqa: SLF001
                conn.execute("UPDATE inbox_chats SET last_at = COALESCE(last_at, ?), name = COALESCE(name, ?) WHERE id = ?",
                             (crm._now(), b.get("guest") or None, cid))
            inbox._flush_new_clients()  # noqa: SLF001
            return inbox.get_chat(cid), ""
        return inbox.start_chat(b["phone"], b.get("guest") or ""), ""
    except inbox.BridgeError as exc:
        return None, str(exc)


def _sources_ok(rule: dict, b: dict) -> bool:
    if not rule["sources"]:
        return True
    return b.get("source", "").lower() in [s.lower() for s in rule["sources"].split(",")]


def _due(rule: dict, b: dict, now: datetime) -> bool:
    """Booking-based triggers fire once, at `at_time` on the computed day;
    a rule enabled later than that moment (same day) still catches up."""
    t = rule["trigger"]
    if t == "new_booking":
        synced = b.get("synced_at") or b.get("created_at") or ""
        return bool(synced) and (now - datetime.fromisoformat(synced[:19])).total_seconds() < 6 * 3600
    base = {"before_checkin": b["checkin"], "checkin_day": b["checkin"], "checkout_day": b["checkout"], "after_checkout": b["checkout"]}.get(t)
    if not base:
        return False
    sign = -1 if t == "before_checkin" else 1 if t == "after_checkout" else 0
    day = date.fromisoformat(base) + timedelta(days=sign * abs(int(rule["offset_days"] or 0)))
    h, m = (int(x) for x in (rule["at_time"] or "10:00").split(":"))
    moment = datetime(day.year, day.month, day.day, h, m)
    return moment <= now < moment + timedelta(hours=12)


def run_auto_rules(dry: bool = False) -> list[dict]:
    """Scheduler entry: every enabled booking rule × every booking in range.
    dry=True returns what WOULD be sent without sending or logging."""
    now = datetime.now()
    results = []
    active = [r for r in rules() if r["enabled"]]
    booking_rules = [r for r in active if r["trigger"] not in ("first_message", "off_hours")]
    if not booking_rules:
        return results
    lo = (now - timedelta(days=15)).date().isoformat()
    hi = (now + timedelta(days=15)).date().isoformat()
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bookings WHERE COALESCE(is_delete, 0) = 0 AND end_date >= ? AND begin_date <= ?", (lo, hi)).fetchall()]
        done = {r[0] for r in conn.execute("SELECT dedupe FROM crm_auto_log").fetchall()}
    for raw in rows:
        if raw.get("status") in ("cancelled", "canceled"):
            continue
        b = crm._booking_out(raw)
        b["synced_at"] = raw.get("synced_at")
        b["created_at"] = raw.get("created_at")
        for rule in booking_rules:
            key = f"{rule['id']}:b{b['id']}"
            if key in done or not _sources_ok(rule, b) or not _due(rule, b, now):
                continue
            if dry:
                chat, why = _chat_for_booking(b, {**rule, "only_if_chat": True})
                results.append({"rule": rule["name"], "booking_id": b["id"], "apartment": b["apartment"], "guest": b["guest"],
                                "chat": chat and chat["title"], "channel": chat and chat["channel_name"],
                                "text": fill(rule["text"], b, chat), "note": "" if chat else (why + (" — создам WhatsApp-чат" if not rule["only_if_chat"] and b.get("phone") else ""))})
                continue
            results.append(_send_rule(rule, b, key, now))
    return results


def _send_rule(rule: dict, b: dict | None, key: str, now: datetime, chat: dict | None = None) -> dict:
    with database.get_conn() as conn:
        if not _log(conn, rule, b and b["id"], None, "", "pending", None, key):
            return {"rule": rule["name"], "status": "skipped"}
    why = ""
    if chat is None and b is not None:
        chat, why = _chat_for_booking(b, rule)
    text = fill(rule["text"], b, chat)
    status, error = "sent", None
    if not chat:
        status, error = "skipped", why or "нет чата"
    else:
        try:
            inbox.send(chat["id"], AUTHOR, text)
        except inbox.BridgeError as exc:
            status, error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto rule send failed")
            status, error = "failed", str(exc)
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_auto_log SET chat_id = ?, chat_title = ?, text = ?, status = ?, error = ?, at = ? WHERE dedupe = ?",
                     (chat and chat["id"], chat and chat.get("title"), text, status, error, crm._now(), key))
    logger.info("auto rule %s → %s: %s %s", rule["name"], chat and chat.get("title"), status, error or "")
    return {"rule": rule["name"], "booking_id": b and b["id"], "chat": chat and chat["title"], "status": status, "error": error}


def _in_hours(now: datetime, frm: str | None, to: str | None) -> bool:
    try:
        fh, fm = (int(x) for x in (frm or "09:00").split(":"))
        th, tm = (int(x) for x in (to or "22:00").split(":"))
    except ValueError:
        return True
    cur = now.hour * 60 + now.minute
    a, b = fh * 60 + fm, th * 60 + tm
    return a <= cur < b if a <= b else (cur >= a or cur < b)


def on_incoming(chat_id: int, is_first: bool) -> None:
    """Message-based rules (called by the inbox, in a thread)."""
    try:
        active = [r for r in rules() if r["enabled"] and r["trigger"] in ("first_message", "off_hours")]
        if not active:
            return
        chat = inbox.get_chat(chat_id)
        if not chat:
            return
        now = datetime.now()
        b = pinned_booking_for_chat(chat_id) or (inbox.booking_for(chat["phone"]) if chat.get("phone") else None)
        bo = crm._booking_out(b) if b else None
        for rule in active:
            if rule["channel"] != "auto" and chat["channel"] != rule["channel"] and not (rule["channel"] in ("wa", "wac") and chat["channel"] in ("wa", "wac")):
                continue
            if rule["trigger"] == "first_message":
                if not is_first:
                    continue
                key = f"{rule['id']}:c{chat_id}"
            else:
                if _in_hours(now, rule["hours_from"], rule["hours_to"]):
                    continue
                key = f"{rule['id']}:c{chat_id}:{now.date().isoformat()}"
            _send_rule(rule, bo, key, now, chat)
    except Exception:  # noqa: BLE001
        logger.exception("incoming auto rules failed")


# ---------------------------------------------------------------------------
# Stage actions («digital pipeline»): run when a deal enters a stage
# ---------------------------------------------------------------------------
def _deal_vars(d: dict, conn) -> dict:
    """A booking-like dict for fill(): the deal's own fields, or its booking."""
    b = None
    if d.get("booking_id"):
        r = conn.execute("SELECT * FROM bookings WHERE id = ?", (d["booking_id"],)).fetchone()
        b = crm._booking_out(dict(r)) if r else None  # noqa: SLF001
    c = conn.execute("SELECT name FROM crm_clients WHERE id = ?", (d["client_id"],)).fetchone() if d.get("client_id") else None
    return {"guest": (c["name"] if c else "") or (b or {}).get("guest") or "", "apartment": d.get("apartment") or (b or {}).get("apartment"),
            "checkin": d.get("checkin") or (b or {}).get("checkin"), "checkout": d.get("checkout") or (b or {}).get("checkout"),
            "nights": (b or {}).get("nights"), "amount": d.get("amount") if d.get("amount") is not None else (b or {}).get("amount"),
            "debt": (b or {}).get("debt"), "id": d.get("booking_id")}


def _deal_chat(d: dict, conn) -> dict | None:
    """Chat to message the guest: linked to the deal, else by the client's phone."""
    ids = [r[0] for r in conn.execute("SELECT chat_id FROM crm_deal_chats WHERE deal_id = ? ORDER BY linked_at DESC", (d["id"],)).fetchall()]
    if not ids and d.get("client_id"):
        c = conn.execute("SELECT phone, chat_id FROM crm_clients WHERE id = ?", (d["client_id"],)).fetchone()
        if c and c["chat_id"]:
            ids.append(c["chat_id"])
        if c and c["phone"] and len(c["phone"]) >= 7:
            ids += [r[0] for r in conn.execute("SELECT id FROM inbox_chats WHERE phone LIKE ? ORDER BY last_at DESC",
                                               ("%" + c["phone"][-9:],)).fetchall()]
    for cid in ids:
        ch = inbox.get_chat(cid)
        if ch:
            return ch
    return None


def run_stage_actions(deal_id: int, stage_id: int, uid: int) -> list[dict]:
    with database.get_conn() as conn:
        actions = [dict(r) for r in conn.execute("SELECT * FROM crm_stage_actions WHERE stage_id = ? ORDER BY sort, id", (stage_id,)).fetchall()]
        if not actions:
            return []
        d = dict(conn.execute("SELECT * FROM crm_deals WHERE id = ?", (deal_id,)).fetchone())
        stage = conn.execute("SELECT name FROM crm_stages WHERE id = ?", (stage_id,)).fetchone()
        vars_ = _deal_vars(d, conn)
        chat = _deal_chat(d, conn)
    stamp = crm._now()
    out = []
    for a in actions:
        rule = {"id": 0, "name": f"Этап «{stage['name'] if stage else stage_id}»"}
        key = f"stage:{a['id']}:d{deal_id}:{stamp}"
        text = fill(a["text"], vars_, chat)
        status, error = "sent", None
        try:
            if a["kind"] == "message":
                if not chat:
                    status, error = "skipped", "у сделки нет чата с гостем"
                else:
                    inbox.send(chat["id"], AUTHOR, text)
            elif a["kind"] == "task":
                from datetime import datetime as _dt
                h, m = (int(x) for x in (a["at_time"] or "10:00").split(":"))
                due = (_dt.now() + timedelta(days=int(a["days"] or 0))).replace(hour=h, minute=m, second=0, microsecond=0)
                crm.save_task(None, {"title": text, "due": due.isoformat(timespec="minutes"), "assignee_uid": d.get("owner_uid") or uid,
                                     "deal_id": deal_id, "client_id": d.get("client_id")}, uid)
            elif a["kind"] == "notify":
                targets = config.inbox_notify_targets()
                if not targets or not config.BOT_TOKEN:
                    status, error = "skipped", "Telegram-уведомления не настроены"
                else:
                    inbox._tg_send_all(targets, f"⚡ {rule['name']}: {d['title']}\n{text}")  # noqa: SLF001
        except inbox.BridgeError as exc:
            status, error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("stage action failed")
            status, error = "failed", str(exc)
        with database.get_conn() as conn:
            _log(conn, rule, d.get("booking_id"), chat if a["kind"] == "message" else None,
                 f"[{ {'message': 'сообщение', 'task': 'задача', 'notify': 'уведомление'}[a['kind']] }] {text}", status, error, key)
        out.append({"kind": a["kind"], "status": status, "error": error})
    return out
