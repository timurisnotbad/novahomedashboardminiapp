"""Writing booking changes back to RealtyCalendar.

The dashboard reads RC through the same web API its own calendar page uses
(/v2/event_calendars, x-user-token). This module sends edits the same way:
    PUT {RC_BASE_URL}/v2/event_calendars/{id}   {"event_calendar": {...}}

What goes to RC (everything RC itself stores on a booking): guest name and
phone, amount, arrival/departure time, notes, check-in/out dates. Everything
RC has no field for — guest notes, custom fields, deals, tasks — lives only
in the CRM. After every push the booking is re-read from RC, so the CRM
shows what RC actually accepted; the request and RC's answer are kept in
crm_rc_log («Интеграции» → RealtyCalendar → последние записи).
"""
import json
import logging
import re
from datetime import date, datetime

import requests

from . import config, database, rc_sync

logger = logging.getLogger("nova.rc.push")

# CRM field -> RC field. Dates go as DD.MM.YYYY like the read API expects them.
FIELDS = {"guest": "fio", "phone": "phone", "amount": "amount", "arrival_time": "arrival_time",
          "departure_time": "departure_time", "notes": "short_notes", "checkin": "begin_date", "checkout": "end_date",
          "status": "status", "prepayment": "prepayment", "email": "email", "phone2": "additional_phone"}
CRM_MARK_START, CRM_MARK_END = "--- CRM ---", "--- /CRM ---"
STATUSES = ("booked", "prepaid", "paid", "confirmed", "not_confirmed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_rc_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER,
    changes TEXT,
    status TEXT,          -- ok | rejected | error
    http INTEGER,
    response TEXT,
    by_user TEXT,
    at TEXT
);
"""


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)


class RCError(Exception):
    pass


def _dmy(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return d.strftime("%d.%m.%Y")


def _payload(changes: dict) -> dict:
    ev: dict = {}
    client: dict = {}
    for k, v in changes.items():
        if k == "guest":
            client["fio"] = (v or "").strip()
        elif k == "phone":
            client["phone"] = ("+" + re.sub(r"\D", "", str(v))) if v else ""
        elif k == "email":
            client["email"] = (v or "").strip()
        elif k == "phone2":
            client["additional_phone"] = ("+" + re.sub(r"\D", "", str(v))) if v else ""
            client["phone2"] = client["additional_phone"]
        elif k in ("amount", "prepayment"):
            ev[k] = float(str(v).replace(",", ".").replace(" ", "")) if v not in (None, "") else 0
        elif k == "status":
            if v not in STATUSES:
                raise RCError("Неизвестный статус оплаты")
            ev["status"] = v
        elif k in ("checkin", "checkout"):
            ev[FIELDS[k]] = _dmy(v)
        elif k in FIELDS:
            ev[FIELDS[k]] = (v or "").strip() if isinstance(v, str) else v
    if client:
        ev["client"] = client
        ev["client_attributes"] = dict(client)  # Rails-style nested attributes, in case RC wants that spelling
    return ev


# write-schema names RC may require, from what we know about the booking
_ALIASES = {
    "prepaid_amount": lambda raw, cur: raw.get("prepayment", cur.get("prepayment")) or 0,
    "prepayment": lambda raw, cur: raw.get("prepayment", cur.get("prepayment")) or 0,
    "amount": lambda raw, cur: raw.get("amount", cur.get("amount")) or 0,
    "price": lambda raw, cur: raw.get("price", raw.get("amount", cur.get("amount"))) or 0,
    "notes": lambda raw, cur: raw.get("short_notes", cur.get("short_notes")) or "",
    "short_notes": lambda raw, cur: raw.get("short_notes", cur.get("short_notes")) or "",
    "begin_date": lambda raw, cur: _dmy(cur["begin_date"]),
    "end_date": lambda raw, cur: _dmy(cur["end_date"]),
    "apartment_id": lambda raw, cur: raw.get("apartment_id", cur.get("apartment_id")),
    "status": lambda raw, cur: raw.get("status", cur.get("status")) or "booked",
    "source_id": lambda raw, cur: raw.get("source_id", cur.get("source_id")),
    "arrival_time": lambda raw, cur: raw.get("arrival_time", cur.get("arrival_time")) or "",
    "departure_time": lambda raw, cur: raw.get("departure_time", cur.get("departure_time")) or "",
    "days_count": lambda raw, cur: raw.get("days_count", cur.get("days_count")) or 0,
    "guests_count": lambda raw, cur: raw.get("guests_count", 1),
    "is_external": lambda raw, cur: bool(raw.get("is_external", cur.get("is_external"))),
}


def _guess_value(name: str, raw: dict, cur: dict):
    if name in _ALIASES:
        return _ALIASES[name](raw, cur)
    if name in raw:
        return raw[name]
    n = name.lower()
    if n.endswith(("amount", "price", "count", "_id", "sum", "progress", "deposit", "commission")):
        return 0
    if n.startswith("is_") or n.startswith("has_") or n.endswith("_enabled"):
        return False
    return ""


def _full_event(raw: dict, cur: dict, changes: dict) -> dict:
    """The calendar's own event + our changes (+ the names its write schema is known to want)."""
    ev = dict(raw)
    ev.pop("id", None)
    client = dict(raw.get("client") or {})
    ev["client"] = client
    payload = _payload(changes)
    for k, v in payload.items():
        if k == "client":
            client.update(v)
        elif k == "client_attributes":
            continue
        else:
            ev[k] = v
    if "fio" not in client and cur.get("client_name"):
        client["fio"] = cur["client_name"]
    if "phone" not in client and cur.get("client_phone"):
        client["phone"] = cur["client_phone"]
    ev["client_attributes"] = dict(client)
    # names the write schema asked for before: send them from the start
    for name in ("prepaid_amount", "begin_date", "end_date", "apartment_id", "status", "amount", "notes"):
        if name not in ev:
            ev[name] = _guess_value(name, raw, cur)
    for k in ("begin_date", "end_date"):  # the calendar reads ISO but writes DD.MM.YYYY
        v = ev.get(k)
        if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v):
            ev[k] = _dmy(v)
    if "prepayment" in payload:
        ev["prepaid_amount"] = payload["prepayment"]
    if "short_notes" in payload:
        ev["notes"] = payload["short_notes"]
    return ev


def _log(bid: int, changes: dict, status: str, http: int | None, response: str, who: str) -> None:
    with database.get_conn() as conn:
        conn.execute("INSERT INTO crm_rc_log (booking_id, changes, status, http, response, by_user, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (bid, json.dumps(changes, ensure_ascii=False), status, http, (response or "")[:2000], who,
                      datetime.now().isoformat(timespec="seconds")))
        conn.execute("DELETE FROM crm_rc_log WHERE id NOT IN (SELECT id FROM crm_rc_log ORDER BY id DESC LIMIT 200)")


def crm_block(client: dict) -> str:
    """The CRM-only guest data as a block for the booking note in RealtyCalendar."""
    lines = []
    for key, label in (("status", "Статус"), ("lang", "Язык"), ("instagram", "Instagram"), ("telegram", "Telegram"),
                       ("city", "Город"), ("birthday", "Дата рождения"), ("passport", "Паспорт")):
        if client.get(key):
            lines.append(f"{label}: {client[key]}")
    if client.get("notes"):
        lines.append("Особенности: " + " ".join(str(client["notes"]).split()))
    return (CRM_MARK_START + "\n" + "\n".join(lines) + "\n" + CRM_MARK_END) if lines else ""


def merge_notes(notes: str, block: str) -> str:
    """Replace (or append) the CRM block inside the booking note, keeping what people wrote by hand."""
    notes = notes or ""
    if CRM_MARK_START in notes and CRM_MARK_END in notes:
        pre = notes.split(CRM_MARK_START)[0].rstrip()
        post = notes.split(CRM_MARK_END, 1)[1].lstrip()
        base = (pre + ("\n" + post if post else "")).strip()
    else:
        base = notes.strip()
    return (base + "\n\n" + block).strip() if block else base


def push_client(cid: int, who: str = "", only_booking: int | None = None) -> dict:
    """Guest card → RealtyCalendar: name, phone, email, second phone go to the
    booking's guest; language, socials, notes go into the booking note as a
    «--- CRM ---» block. Applied to the guest's current and future bookings."""
    from . import crm
    if crm.get_setting("rc_sync_clients", "1") != "1":
        return {"bookings": 0, "ok": 0, "errors": ["выключено в Интеграциях"]}
    c = crm.get_client(cid)
    if not c:
        return {"bookings": 0, "ok": 0, "errors": ["клиент не найден"]}
    today = date.today().isoformat()
    targets = [b for b in (c.get("bookings") or []) if b.get("checkout", "") >= today and (not only_booking or b["id"] == only_booking)][:10]
    if only_booking and not targets:
        targets = [{"id": only_booking}]
    block = crm_block(c)
    ok, errors = 0, []
    for b in targets:
        with database.get_conn() as conn:
            cur = conn.execute("SELECT * FROM bookings WHERE id = ?", (b["id"],)).fetchone()
        if not cur:
            continue
        changes: dict = {}
        if c.get("name") and not c["name"].startswith("+") and c["name"] != (cur["client_name"] or ""):
            changes["guest"] = c["name"]
        if c.get("phone") and re.sub(r"\D", "", cur["client_phone"] or "") != c["phone"]:
            changes["phone"] = c["phone"]
        if c.get("email") and (cur["client_email"] or "") != c["email"]:
            changes["email"] = c["email"]
        if c.get("phone2") and re.sub(r"\D", "", cur["client_phone2"] or "") != re.sub(r"\D", "", c["phone2"]):
            changes["phone2"] = c["phone2"]
        new_notes = merge_notes(cur["short_notes"] or "", block)
        if new_notes != (cur["short_notes"] or "").strip():
            changes["notes"] = new_notes
        if not changes:
            continue
        try:
            update_booking(b["id"], changes, who or "CRM")
            ok += 1
        except RCError as exc:
            errors.append(f"#{b['id']}: {exc}")
    return {"bookings": len(targets), "ok": ok, "errors": errors}


def recent_log(limit: int = 20) -> list[dict]:
    with database.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM crm_rc_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def update_booking(bid: int, changes: dict, who: str = "") -> dict:
    """Push `changes` (CRM field names) to RC, re-sync, return the fresh booking.
    Raises RCError with RC's own words when the change was not accepted."""
    changes = {k: v for k, v in changes.items() if k in FIELDS}
    if not changes:
        raise RCError("Нечего отправлять")
    if config.DEMO_MODE:
        raise RCError("Демо-режим: RC_TOKEN не задан, в календарь ничего не отправляется")
    with database.get_conn() as conn:
        cur = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
    if not cur:
        raise RCError("Бронь не найдена")
    if "checkin" in changes or "checkout" in changes:
        ci = changes.get("checkin") or cur["begin_date"]
        co = changes.get("checkout") or cur["end_date"]
        if co <= ci:
            raise RCError("Дата выезда должна быть позже заезда")
    # RC validates the whole object on PUT («required property …»): send its own
    # full event with our changes merged in, and fill whatever it still asks for.
    try:
        raw = rc_sync.fetch_raw_event(bid) or {}
    except Exception:  # noqa: BLE001
        raw = {}
    ev = _full_event(raw, dict(cur), changes)
    url = f"{config.RC_BASE_URL}/v2/event_calendars/{bid}"
    headers = {**rc_sync._headers(), "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}  # noqa: SLF001
    resp = None
    for attempt in range(4):
        # RC's schema error names the root ('#/'): the fields go at the top level
        # of the body; the nested form is kept for builds that read it there.
        body = {**ev, "event_calendar": ev}
        try:
            resp = requests.put(url, json=body, headers=headers, timeout=25)
            if resp.status_code in (404, 405):  # some RC builds take PATCH
                resp = requests.patch(url, json=body, headers=headers, timeout=25)
        except requests.RequestException as exc:
            _log(bid, changes, "error", None, str(exc), who)
            raise RCError(f"RealtyCalendar недоступен: {exc}") from exc
        if resp.status_code < 400:
            break
        missing = re.findall(r"required property of '([A-Za-z0-9_]+)'", resp.text or "")
        if not missing or attempt == 3:
            break
        added = False
        for name in missing:
            if name not in ev:
                ev[name] = _guess_value(name, raw, dict(cur))
                added = True
        if not added:
            break
    text = resp.text or ""
    if resp.status_code >= 400:
        msg = text[:300]
        try:
            j = resp.json()
            msg = j.get("error") or j.get("errors") or j.get("message") or msg
            if isinstance(msg, (dict, list)):
                msg = json.dumps(msg, ensure_ascii=False)[:300]
        except ValueError:
            pass
        _log(bid, changes, "rejected", resp.status_code, text, who)
        raise RCError(f"RealtyCalendar не принял изменение ({resp.status_code}): {msg}")
    # confirm: re-read this apartment from the calendar and compare what RC now returns
    try:
        rc_sync.refresh_booking(bid)
    except Exception as exc:  # noqa: BLE001
        _log(bid, changes, "ok", resp.status_code, f"saved; resync failed: {exc}", who)
        raise RCError(f"Отправлено, но перечитать календарь не удалось: {exc}") from exc
    with database.get_conn() as conn:
        fresh = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
    mismatch = []
    if fresh:
        got = {"guest": fresh["client_name"], "phone": re.sub(r"\D", "", fresh["client_phone"] or ""), "amount": fresh["amount"],
               "arrival_time": fresh["arrival_time"], "departure_time": fresh["departure_time"], "notes": fresh["short_notes"],
               "checkin": fresh["begin_date"], "checkout": fresh["end_date"], "status": fresh["status"], "prepayment": fresh["prepayment"]}
        for k, v in changes.items():
            if k in ("email", "phone2"):  # RC may not echo these back on the calendar feed: accept its 2xx
                continue
            want = re.sub(r"\D", "", str(v)) if k == "phone" else (float(str(v).replace(",", ".") or 0) if k in ("amount", "prepayment") else (v or ""))
            have = got.get(k)
            if k in ("amount", "prepayment"):
                have = float(have or 0)
            if (have or "") != (want or ""):
                mismatch.append(k)
    if mismatch:
        _log(bid, changes, "rejected", resp.status_code, f"RC answered {resp.status_code} but kept old values for: {', '.join(mismatch)}. Body: {text[:600]}", who)
        raise RCError("RealtyCalendar ответил «ок», но в календаре значения не изменились: " + ", ".join(mismatch)
                      + ". Подробности — Интеграции → RealtyCalendar")
    _log(bid, changes, "ok", resp.status_code, text[:600], who)
    from . import crm
    return crm._booking_out(dict(fresh)) if fresh else {}  # noqa: SLF001
