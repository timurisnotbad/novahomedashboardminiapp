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
          "departure_time": "departure_time", "notes": "short_notes", "checkin": "begin_date", "checkout": "end_date"}

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
        elif k == "amount":
            ev["amount"] = float(str(v).replace(",", ".").replace(" ", "")) if v not in (None, "") else 0
        elif k in ("checkin", "checkout"):
            ev[FIELDS[k]] = _dmy(v)
        elif k in FIELDS:
            ev[FIELDS[k]] = (v or "").strip() if isinstance(v, str) else v
    if client:
        ev["client"] = client
        ev["client_attributes"] = dict(client)  # Rails-style nested attributes, in case RC wants that spelling
    return ev


def _log(bid: int, changes: dict, status: str, http: int | None, response: str, who: str) -> None:
    with database.get_conn() as conn:
        conn.execute("INSERT INTO crm_rc_log (booking_id, changes, status, http, response, by_user, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (bid, json.dumps(changes, ensure_ascii=False), status, http, (response or "")[:2000], who,
                      datetime.now().isoformat(timespec="seconds")))
        conn.execute("DELETE FROM crm_rc_log WHERE id NOT IN (SELECT id FROM crm_rc_log ORDER BY id DESC LIMIT 200)")


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
    body = {"event_calendar": _payload(changes)}
    url = f"{config.RC_BASE_URL}/v2/event_calendars/{bid}"
    headers = {**rc_sync._headers(), "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}  # noqa: SLF001
    try:
        resp = requests.put(url, json=body, headers=headers, timeout=30)
        if resp.status_code in (404, 405):  # some RC builds take PATCH
            resp = requests.patch(url, json=body, headers=headers, timeout=30)
    except requests.RequestException as exc:
        _log(bid, changes, "error", None, str(exc), who)
        raise RCError(f"RealtyCalendar недоступен: {exc}") from exc
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
    # confirm: re-read the calendar and compare what RC now returns
    try:
        rc_sync.sync_to_db()
    except Exception as exc:  # noqa: BLE001
        _log(bid, changes, "ok", resp.status_code, f"saved; resync failed: {exc}", who)
        raise RCError(f"Отправлено, но перечитать календарь не удалось: {exc}") from exc
    with database.get_conn() as conn:
        fresh = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
    mismatch = []
    if fresh:
        got = {"guest": fresh["client_name"], "phone": re.sub(r"\D", "", fresh["client_phone"] or ""), "amount": fresh["amount"],
               "arrival_time": fresh["arrival_time"], "departure_time": fresh["departure_time"], "notes": fresh["short_notes"],
               "checkin": fresh["begin_date"], "checkout": fresh["end_date"]}
        for k, v in changes.items():
            want = re.sub(r"\D", "", str(v)) if k == "phone" else (float(str(v).replace(",", ".") or 0) if k == "amount" else (v or ""))
            have = got.get(k)
            if k == "amount":
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
