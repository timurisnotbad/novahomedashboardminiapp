"""Cash receipts («Оплаты»): what the team received from guests, written
straight into the owner's Google Sheet («Операции») in his own column format.

One *payment* = what one guest handed over at once: one or more apartments,
one or more money lines (amount + account; the account fixes the currency).
Every line becomes one sheet row on the apartment whose cash box physically
holds the money (the receiver). A payment for several apartments is «joint»:
the other apartments get their share only inside the CRM, and the payment
waits in the owner's review list — bills are never split in the sheet, the
sheet mirrors real cash per apartment.

Accounts and exchange rates come from the sheet («Валюты»), cached for an
hour; a fallback list covers a sheet that is not reachable. Nothing is ever
deleted: edits and cancellations are kept in cash_audit with who/when/
before/after, and the owner is told about each one in Telegram.
"""
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

import requests

from . import config, database, util

logger = logging.getLogger(__name__)

ARTICLE = "Бронь"
EDIT_MINUTES = 30
SHEET_NAME = "Операции"
DEBT_SHEET = "Долг между Аппартами"
DEFAULT_METHODS = [("Нал USD", "USD"), ("Нал UZS", "UZS"), ("Нал EUR", "EUR"), ("Карта Хумо 4980", "UZS"), ("В приложении", "USD"),
                   ("Карта Visa 9362", "USD"), ("RUB", "RUB"), ("Click Biz", "UZS"), ("Payme Biz", "UZS"), ("Uzum Biz", "UZS")]
DEFAULT_RATES = {"UZS": 12000.0, "EUR": 0.90, "RUB": 84.0}  # units of currency per 1 $
QUICK_METHODS = ["Нал USD", "Нал UZS", "Карта Хумо 4980"]  # the three big buttons; the rest under «ещё»
_meta_cache: dict = {"at": 0.0, "data": None}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS cash_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, updated_at TEXT, day TEXT,
            who_uid INTEGER, who_name TEXT, source TEXT,
            apartments TEXT, receiver TEXT, stay TEXT, note TEXT,
            status TEXT DEFAULT 'ok', cancel_reason TEXT, joint INTEGER DEFAULT 0, shares TEXT,
            booking_ids TEXT, raw_text TEXT, channel_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS cash_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL,
            amount REAL, currency TEXT, method TEXT, usd REAL,
            sheet_row INTEGER, sheet_status TEXT DEFAULT 'pending', sheet_error TEXT, sheet_at TEXT, void INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_cash_lines_payment ON cash_lines(payment_id);
        CREATE INDEX IF NOT EXISTS idx_cash_payments_day ON cash_payments(day);
        CREATE TABLE IF NOT EXISTS cash_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER, at TEXT, who_uid INTEGER, who_name TEXT, action TEXT, details TEXT
        );
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cash_lines)").fetchall()}
        if "void" not in cols:
            conn.execute("ALTER TABLE cash_lines ADD COLUMN void INTEGER DEFAULT 0")


# ---------------------------------------------------------------------------
# Accounts and rates (from the sheet, cached)
# ---------------------------------------------------------------------------
def _sheet_call(action: str, payload: dict | None = None, timeout: int = 40) -> dict:
    if not config.FINANCE_ENABLED:
        raise RuntimeError("Таблица не подключена: SHEET_API_URL / SHEET_API_TOKEN в .env")
    body = {"action": action, "token": config.SHEET_API_TOKEN, **(payload or {})}
    resp = requests.post(config.SHEET_API_URL, json=body, timeout=timeout, allow_redirects=True,
                         headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError("скрипт таблицы ответил не JSON — проверьте доступ веб-приложения («все»)") from exc
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"])[:200])
    return data if isinstance(data, dict) else {"data": data}


def meta(force: bool = False) -> dict:
    """{"methods": [{"name","currency"}], "rates": {"UZS":…}, "source": "sheet"|"default"}."""
    now = time.time()
    if not force and _meta_cache["data"] and now - _meta_cache["at"] < 3600:
        return _meta_cache["data"]
    out = {"methods": [{"name": n, "currency": c} for n, c in DEFAULT_METHODS], "rates": dict(DEFAULT_RATES), "source": "default"}
    from . import crm
    saved = crm.get_setting("cash_meta", "")
    if saved:
        try:
            out = {**out, **json.loads(saved), "source": "saved"}
        except ValueError:
            pass
    if config.FINANCE_ENABLED:
        try:
            d = _sheet_call("cash_meta", timeout=25)
            methods = [{"name": str(m.get("name") or "").strip(), "currency": str(m.get("currency") or "").strip().upper()}
                       for m in (d.get("methods") or []) if m.get("name")]
            rates = {str(k).upper(): float(v) for k, v in (d.get("rates") or {}).items() if v}
            if methods:
                out = {"methods": methods, "rates": {**DEFAULT_RATES, **rates}, "source": "sheet", "at": util.now()}
                crm.set_setting("cash_meta", json.dumps({"methods": methods, "rates": out["rates"], "at": out["at"]}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cash meta from sheet failed: %s", exc)
            out["error"] = config.redact(str(exc))[:200]
    out["quick"] = [m for m in QUICK_METHODS if any(x["name"] == m for x in out["methods"])] or [x["name"] for x in out["methods"][:3]]
    _meta_cache.update(at=now, data=out)
    return out


def currency_of(method: str) -> str:
    for m in meta()["methods"]:
        if m["name"].lower() == (method or "").lower():
            return m["currency"]
    return "USD" if re.search(r"USD|\$|visa|прилож", method or "", re.I) else "UZS"


def to_usd(amount: float, currency: str) -> float:
    currency = (currency or "USD").upper()
    if currency == "USD":
        return round(float(amount), 2)
    rate = float(meta()["rates"].get(currency) or 0)
    return round(float(amount) / rate, 2) if rate else 0.0


# ---------------------------------------------------------------------------
# Helpers: apartments, stay dates, sheet row
# ---------------------------------------------------------------------------
def apartment_list() -> list[dict]:
    """[{name:'B-005', num:5, label:'5'}] in the owner's order (block A first, then B)."""
    out = []
    for n in sorted(set(config.APARTMENTS.values())):
        num = int(re.sub(r"\D", "", n) or 0)
        out.append({"name": n, "num": num, "label": str(num)})
    return out


def apartment_by_num(num) -> str | None:
    try:
        n = int(str(num).strip().lstrip("0") or 0)
    except ValueError:
        return None
    hits = [a["name"] for a in apartment_list() if a["num"] == n]
    return hits[0] if len(hits) == 1 else None


def norm_apartment(v: str) -> str | None:
    v = (v or "").strip().upper().replace("В", "B").replace("А", "A")
    m = re.match(r"^([AB])\s?-?\s?(\d{1,3})$", v)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):03d}"
    if v.isdigit():
        return apartment_by_num(v)
    return None


_STAY_RE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*[-–—]\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")
_STAY_SHORT = re.compile(r"^(\d{1,2})\s*[-–—]\s*(\d{1,2})[./](\d{1,2})$")


def parse_stay(text: str, ref: date | None = None) -> dict | None:
    """«19.10-23.10», «19.10 – 23.10.2026», «30.09-1.10», «19-23.10» → {checkin, checkout, label}; label is the owner's
    sheet style: «19-23.10», «30.12-02.01»."""
    ref = ref or date.today()
    t = (text or "").strip()
    m = _STAY_RE.search(t)
    if m:
        d1, m1, y1, d2, m2, y2 = m.groups()
        d1, m1, d2, m2 = int(d1), int(m1), int(d2), int(m2)
    else:
        m = _STAY_SHORT.match(t)
        if not m:
            return None
        d1, d2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        m1, y1, y2 = m2 if d1 <= d2 else (m2 - 1 or 12), None, None
    def year(mo, y):
        if y:
            y = int(y)
            return y + 2000 if y < 100 else y
        if mo - ref.month > 6:
            return ref.year - 1
        if ref.month - mo > 6:
            return ref.year + 1
        return ref.year
    try:
        ci = date(year(m1, y1), m1, d1)
        co = date(year(m2, y2), m2, d2)
    except ValueError:
        return None
    if co < ci:
        co = date(co.year + 1, co.month, co.day) if co.month < ci.month else co
    return {"checkin": ci.isoformat(), "checkout": co.isoformat(), "label": f"{d1}-{d2}.{m2:02d}" if m1 == m2 else f"{d1:02d}.{m1:02d}-{d2:02d}.{m2:02d}"}


def _apt_num(name: str) -> int:
    return int(re.sub(r"\D", "", name or "") or 0)


def sheet_row(p: dict, line: dict) -> list:
    """The ten columns of «Операции»: Год, мес, Дата, Апартамент, Статья, Сумма, Сумма $, Метод, Валюта, Комментарий."""
    d = date.fromisoformat(p["day"][:10])
    comment = (p.get("stay_label") or p.get("stay") or "").strip()
    if p.get("note"):
        comment = (comment + " · " if comment else "") + p["note"].strip()
    if p.get("joint"):
        apts = "+".join(str(_apt_num(a)) for a in p["apartments"])
        comment = (comment + " · " if comment else "") + f"платёж №{p['id']} за кв. {apts}"
    return [d.year, util.RU_MONTHS_GEN[d.month - 1], d.strftime("%d.%m.%Y"), _apt_num(p["receiver"]), ARTICLE,
            round(float(line["amount"]), 2), round(float(line["usd"]), 2), line["method"], line["currency"], comment]


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
def _load(conn, pid: int) -> dict | None:
    r = conn.execute("SELECT * FROM cash_payments WHERE id = ?", (pid,)).fetchone()
    if not r:
        return None
    p = dict(r)
    for k in ("apartments", "shares", "booking_ids"):
        try:
            p[k] = json.loads(p[k] or "[]" if k != "shares" else p[k] or "{}")
        except ValueError:
            p[k] = [] if k != "shares" else {}
    p["lines"] = [dict(x) for x in conn.execute("SELECT * FROM cash_lines WHERE payment_id = ? ORDER BY id", (pid,)).fetchall()]
    p["total_usd"] = round(sum(float(x["usd"] or 0) for x in p["lines"] if not x.get("void")), 2)
    st = parse_stay(p.get("stay") or "", date.fromisoformat(p["day"][:10]) if p.get("day") else None)
    p["stay_label"] = st["label"] if st else (p.get("stay") or "")
    p["sheet"] = ("ok" if p["lines"] and all(x["sheet_status"] == "ok" for x in p["lines"]) else
                  "error" if any(x["sheet_status"] == "error" for x in p["lines"]) else "pending")
    return p


def get(pid: int) -> dict | None:
    with database.get_conn() as conn:
        return _load(conn, pid)


def by_ref(channel_ref: str) -> dict | None:
    """The payment made from a channel post (chat_id:message_id), if any."""
    with database.get_conn() as conn:
        r = conn.execute("SELECT id FROM cash_payments WHERE channel_ref = ? ORDER BY id DESC LIMIT 1", (channel_ref,)).fetchone()
        return _load(conn, r[0]) if r else None


def _audit(conn, pid: int, who: dict, action: str, details: dict) -> None:
    conn.execute("INSERT INTO cash_audit (payment_id, at, who_uid, who_name, action, details) VALUES (?, ?, ?, ?, ?, ?)",
                 (pid, util.now(), who.get("uid"), who.get("name"), action, json.dumps(details, ensure_ascii=False)[:4000]))


def _bookings_for(apartments: list[str], stay: dict | None) -> dict:
    """{apartment: {id, amount}} — the booking of each apartment around the stay (or today)."""
    out = {}
    ci = stay["checkin"] if stay else date.today().isoformat()
    for a in apartments:
        try:
            rows = [b for b in database.get_bookings(ci, ci, apartment=a) if (b.get("status") or "") not in ("cancelled", "canceled")]
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            b = rows[0]
            out[a] = {"id": b["id"], "amount": float(b.get("amount") or 0), "begin": b.get("begin_date"), "end": b.get("end_date")}
    return out


def _shares(apartments: list[str], total_usd: float, bookings: dict) -> dict:
    """USD share per apartment: proportional to booking amounts, else equal; rounding goes to the last one."""
    weights = {a: (bookings.get(a) or {}).get("amount") or 0 for a in apartments}
    if not all(weights.values()):
        weights = {a: 1.0 for a in apartments}
    s = sum(weights.values())
    out, acc = {}, 0.0
    for i, a in enumerate(apartments):
        v = round(total_usd * weights[a] / s, 2) if i < len(apartments) - 1 else round(total_usd - acc, 2)
        out[a] = v
        acc += v
    return out


def _clean_lines(lines: list) -> list[dict]:
    out = []
    for ln in lines or []:
        try:
            amount = float(str(ln.get("amount", "")).replace(" ", "").replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        if amount <= 0:
            continue
        method = str(ln.get("method") or "").strip()
        if not method:
            raise ValueError("У суммы не выбран счёт")
        cur = currency_of(method)
        out.append({"amount": round(amount, 2), "currency": cur, "method": method, "usd": to_usd(amount, cur)})
    if not out:
        raise ValueError("Нет ни одной суммы")
    return out


def add(data: dict, who: dict, source: str = "crm", push: bool = True) -> dict:
    """data: {apartments:[...], receiver?, lines:[{amount, method}], stay?, note?, day?, raw_text?, channel_ref?}."""
    apts = []
    for a in data.get("apartments") or []:
        n = norm_apartment(str(a))
        if n and n not in apts:
            apts.append(n)
    if not apts:
        raise ValueError("Не выбрана квартира")
    receiver = norm_apartment(str(data.get("receiver") or "")) or apts[0]
    apts = [receiver] + [a for a in apts if a != receiver]  # the receiver (whose cash box holds the money) goes first
    lines = _clean_lines(data.get("lines") or [])
    day = (data.get("day") or date.today().isoformat())[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ValueError("Неверная дата") from None
    stay_text = (data.get("stay") or "").strip()[:40]
    stay = parse_stay(stay_text, date.fromisoformat(day)) if stay_text else None
    if stay_text and not stay:
        raise ValueError("Даты проживания в виде 19.10-23.10")
    joint = len(apts) > 1
    bookings = _bookings_for(apts, stay)
    total = round(sum(x["usd"] for x in lines), 2)
    shares = _shares(apts, total, bookings) if joint else {apts[0]: total}
    now = util.now()
    with database.get_conn() as conn:
        pid = conn.execute(
            "INSERT INTO cash_payments (created_at, updated_at, day, who_uid, who_name, source, apartments, receiver, stay, note, status, joint, shares, booking_ids, raw_text, channel_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, now, day, who.get("uid"), who.get("name"), source, json.dumps(apts), receiver, stay_text, (data.get("note") or "").strip()[:300],
             "review" if joint else "ok", 1 if joint else 0, json.dumps(shares), json.dumps({a: b["id"] for a, b in bookings.items()}),
             (data.get("raw_text") or "")[:1000], data.get("channel_ref"))).lastrowid
        for ln in lines:
            conn.execute("INSERT INTO cash_lines (payment_id, amount, currency, method, usd) VALUES (?, ?, ?, ?, ?)",
                         (pid, ln["amount"], ln["currency"], ln["method"], ln["usd"]))
        _audit(conn, pid, who, "add", {"apartments": apts, "receiver": receiver, "lines": lines, "stay": stay_text, "day": day, "source": source})
    if push:
        push_payment(pid)
    p = get(pid)
    _notify(p, who, "add")
    return p


def push_payment(pid: int) -> dict:
    """Write the payment's lines into «Операции» (rows that are not there yet)."""
    p = get(pid)
    if not p or p["status"] == "cancelled":
        return p or {}
    todo = [ln for ln in p["lines"] if ln["sheet_status"] != "ok"]
    if not todo:
        return p
    rows = [sheet_row(p, ln) for ln in todo]
    try:
        res = _sheet_call("cash_add", {"sheet": SHEET_NAME, "rows": rows})
        nums = res.get("rows") or []
        with database.get_conn() as conn:
            for ln, n in zip(todo, nums):
                conn.execute("UPDATE cash_lines SET sheet_row = ?, sheet_status = 'ok', sheet_error = NULL, sheet_at = ? WHERE id = ?", (int(n), util.now(), ln["id"]))
            for ln in todo[len(nums):]:
                conn.execute("UPDATE cash_lines SET sheet_status = 'error', sheet_error = 'таблица не вернула номер строки' WHERE id = ?", (ln["id"],))
    except Exception as exc:  # noqa: BLE001
        err = config.redact(str(exc))[:200]
        logger.warning("cash push failed for %s: %s", pid, err)
        with database.get_conn() as conn:
            for ln in todo:
                conn.execute("UPDATE cash_lines SET sheet_status = 'error', sheet_error = ? WHERE id = ?", (err, ln["id"]))
    return get(pid)


def retry_pending(limit: int = 50) -> int:
    """Scheduler: rows the sheet did not take yet (unreachable, quota) — try again."""
    if not config.FINANCE_ENABLED:
        return 0
    with database.get_conn() as conn:
        ids = [r[0] for r in conn.execute("SELECT DISTINCT p.id FROM cash_payments p JOIN cash_lines l ON l.payment_id = p.id "
                                          "WHERE p.status != 'cancelled' AND l.sheet_status != 'ok' ORDER BY p.id LIMIT ?", (limit,)).fetchall()]
    n = 0
    for pid in ids:
        p = push_payment(pid)
        n += p.get("sheet") == "ok"
    return n


def may_edit(p: dict, who: dict) -> bool:
    if who.get("owner"):
        return True
    if p.get("who_uid") != who.get("uid"):
        return False
    try:
        return datetime.now() - datetime.fromisoformat(p["created_at"]) <= timedelta(minutes=EDIT_MINUTES)
    except ValueError:
        return False


def edit(pid: int, data: dict, who: dict) -> dict:
    """Change the payment; the sheet rows are rewritten in place (same row numbers)."""
    p = get(pid)
    if not p:
        raise ValueError("Платёж не найден")
    if p["status"] == "cancelled":
        raise ValueError("Платёж отменён")
    if not may_edit(p, who):
        raise PermissionError(f"Править можно {EDIT_MINUTES} минут после записи; потом — только владелец")
    before = {"apartments": p["apartments"], "receiver": p["receiver"], "stay": p["stay"], "note": p["note"], "day": p["day"],
              "lines": [{"amount": x["amount"], "method": x["method"]} for x in p["lines"] if not x.get("void")]}
    apts = []
    for a in data.get("apartments") or p["apartments"]:
        n = norm_apartment(str(a))
        if n and n not in apts:
            apts.append(n)
    if not apts:
        raise ValueError("Не выбрана квартира")
    receiver = norm_apartment(str(data.get("receiver") or "")) or (p["receiver"] if p["receiver"] in apts else apts[0])
    apts = [receiver] + [a for a in apts if a != receiver]
    lines = _clean_lines(data.get("lines") or before["lines"])
    day = (data.get("day") or p["day"])[:10]
    stay_text = (data.get("stay") if data.get("stay") is not None else p["stay"] or "").strip()[:40]
    stay = parse_stay(stay_text, date.fromisoformat(day)) if stay_text else None
    if stay_text and not stay:
        raise ValueError("Даты проживания в виде 19.10-23.10")
    joint = len(apts) > 1
    total = round(sum(x["usd"] for x in lines), 2)
    shares = _shares(apts, total, _bookings_for(apts, stay)) if joint else {apts[0]: total}
    old_lines = p["lines"]
    with database.get_conn() as conn:
        conn.execute("UPDATE cash_payments SET updated_at = ?, day = ?, apartments = ?, receiver = ?, stay = ?, note = ?, joint = ?, shares = ?, "
                     "status = CASE WHEN status = 'review' OR ? = 1 THEN 'review' ELSE 'ok' END WHERE id = ?",
                     (util.now(), day, json.dumps(apts), receiver, stay_text, (data.get("note") if data.get("note") is not None else p["note"] or "")[:300],
                      1 if joint else 0, json.dumps(shares), 1 if joint else 0, pid))
        # keep row numbers: line i rewrites old row i; extra old rows are zeroed, extra new lines are appended later
        for i, ln in enumerate(lines):
            if i < len(old_lines):
                conn.execute("UPDATE cash_lines SET amount = ?, currency = ?, method = ?, usd = ?, void = 0, sheet_status = CASE WHEN sheet_row IS NULL THEN 'pending' ELSE 'dirty' END WHERE id = ?",
                             (ln["amount"], ln["currency"], ln["method"], ln["usd"], old_lines[i]["id"]))
            else:
                conn.execute("INSERT INTO cash_lines (payment_id, amount, currency, method, usd) VALUES (?, ?, ?, ?, ?)",
                             (pid, ln["amount"], ln["currency"], ln["method"], ln["usd"]))
        for old in old_lines[len(lines):]:
            if old["sheet_row"]:
                conn.execute("UPDATE cash_lines SET void = 1, sheet_status = 'dirty' WHERE id = ?", (old["id"],))
            else:
                conn.execute("DELETE FROM cash_lines WHERE id = ?", (old["id"],))
        _audit(conn, pid, who, "edit", {"before": before, "after": {"apartments": apts, "receiver": receiver, "stay": stay_text, "day": day,
                                                                      "lines": [{"amount": x["amount"], "method": x["method"]} for x in lines]}})
    _sync_dirty(pid)
    p = get(pid)
    _notify(p, who, "edit", before)
    return p


def _sync_dirty(pid: int) -> None:
    """Rows already in the sheet whose values changed: rewrite them; new lines: append."""
    p = get(pid)
    if not p:
        return
    for ln in p["lines"]:
        if ln["sheet_status"] == "dirty" and ln["sheet_row"]:
            values = sheet_row(p, ln)
            if p["status"] == "cancelled" or ln.get("void"):
                values[5], values[6] = 0, 0
                values[9] = f"ОТМЕНА ({p.get('cancel_reason') or 'правка'}) · было {ln['amount']} {ln['currency']} · " + str(values[9])
            try:
                _sheet_call("cash_update", {"sheet": SHEET_NAME, "row": int(ln["sheet_row"]), "values": values})
                with database.get_conn() as conn:
                    conn.execute("UPDATE cash_lines SET sheet_status = 'ok', sheet_error = NULL, sheet_at = ? WHERE id = ?", (util.now(), ln["id"]))
            except Exception as exc:  # noqa: BLE001
                with database.get_conn() as conn:
                    conn.execute("UPDATE cash_lines SET sheet_status = 'error', sheet_error = ? WHERE id = ?", (config.redact(str(exc))[:200], ln["id"]))
    push_payment(pid)


def cancel(pid: int, reason: str, who: dict) -> dict:
    """Nothing is deleted: the payment is marked cancelled, its sheet rows get amount 0 and a note."""
    p = get(pid)
    if not p:
        raise ValueError("Платёж не найден")
    if p["status"] == "cancelled":
        return p
    if not may_edit(p, who):
        raise PermissionError(f"Отменить можно {EDIT_MINUTES} минут после записи; потом — только владелец")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Укажите причину отмены")
    with database.get_conn() as conn:
        conn.execute("UPDATE cash_payments SET status = 'cancelled', cancel_reason = ?, updated_at = ? WHERE id = ?", (reason[:300], util.now(), pid))
        conn.execute("UPDATE cash_lines SET sheet_status = CASE WHEN sheet_row IS NULL THEN 'ok' ELSE 'dirty' END WHERE payment_id = ?", (pid,))
        _audit(conn, pid, who, "cancel", {"reason": reason, "lines": [{"amount": x["amount"], "method": x["method"]} for x in p["lines"]]})
    p2 = get(pid)
    for ln in p2["lines"]:
        if ln["sheet_status"] == "dirty" and ln["sheet_row"]:
            values = sheet_row(p2, ln)
            values[5], values[6] = 0, 0
            values[9] = f"ОТМЕНА ({reason}) · было {ln['amount']} {ln['currency']} · " + str(values[9])
            try:
                _sheet_call("cash_update", {"sheet": SHEET_NAME, "row": int(ln["sheet_row"]), "values": values})
                with database.get_conn() as conn:
                    conn.execute("UPDATE cash_lines SET sheet_status = 'ok', sheet_at = ? WHERE id = ?", (util.now(), ln["id"]))
            except Exception as exc:  # noqa: BLE001
                with database.get_conn() as conn:
                    conn.execute("UPDATE cash_lines SET sheet_status = 'error', sheet_error = ? WHERE id = ?", (config.redact(str(exc))[:200], ln["id"]))
    p3 = get(pid)
    _notify(p3, who, "cancel")
    return p3


def resolve_review(pid: int, who: dict, as_debt: bool) -> dict:
    """Owner's decision on a joint payment: leave it as is, or write the shares into «Долг между Аппартами»."""
    if not who.get("owner"):
        raise PermissionError("Только владелец")
    p = get(pid)
    if not p:
        raise ValueError("Платёж не найден")
    written = []
    if as_debt:
        for a, usd in (p["shares"] or {}).items():
            if a == p["receiver"] or not usd:
                continue
            row = [date.fromisoformat(p["day"]).strftime("%d.%m.%Y"), _apt_num(p["receiver"]), _apt_num(a), round(float(usd), 2),
                   f"платёж №{pid} · {p.get('stay_label') or ''}".strip(" ·")]
            _sheet_call("cash_debt", {"sheet": DEBT_SHEET, "row": row})
            written.append(row)
    with database.get_conn() as conn:
        conn.execute("UPDATE cash_payments SET status = 'ok', updated_at = ? WHERE id = ?", (util.now(), pid))
        _audit(conn, pid, who, "debt" if as_debt else "reviewed", {"rows": written})
    return get(pid)


# ---------------------------------------------------------------------------
# Lists and stats
# ---------------------------------------------------------------------------
def list_payments(day: str | None = None, month: str | None = None, who_uid: int | None = None, limit: int = 300) -> list[dict]:
    where, args = [], []
    if day:
        where.append("day = ?"); args.append(day[:10])
    if month:
        where.append("day LIKE ?"); args.append(month[:7] + "%")
    if who_uid is not None:
        where.append("who_uid = ?"); args.append(who_uid)
    sql = "SELECT id FROM cash_payments" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(limit)
    with database.get_conn() as conn:
        ids = [r[0] for r in conn.execute(sql, args).fetchall()]
        return [_load(conn, i) for i in ids]


def review_list() -> list[dict]:
    with database.get_conn() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM cash_payments WHERE status = 'review' ORDER BY id DESC LIMIT 200").fetchall()]
        return [_load(conn, i) for i in ids]


def stats(month: str) -> dict:
    month = (month or date.today().isoformat())[:7]
    rows = [p for p in list_payments(month=month, limit=5000) if p["status"] != "cancelled"]
    by_method: dict[str, dict] = {}
    by_day: dict[str, float] = {}
    for p in rows:
        by_day[p["day"]] = round(by_day.get(p["day"], 0) + p["total_usd"], 2)
        for ln in p["lines"]:
            if ln.get("void"):
                continue
            m = by_method.setdefault(ln["method"], {"method": ln["method"], "currency": ln["currency"], "amount": 0.0, "usd": 0.0, "n": 0})
            m["amount"] = round(m["amount"] + float(ln["amount"] or 0), 2)
            m["usd"] = round(m["usd"] + float(ln["usd"] or 0), 2)
            m["n"] += 1
    with database.get_conn() as conn:
        pending = conn.execute("SELECT COUNT(DISTINCT p.id) FROM cash_payments p JOIN cash_lines l ON l.payment_id = p.id "
                               "WHERE p.status != 'cancelled' AND l.sheet_status != 'ok' AND p.day LIKE ?", (month + "%",)).fetchone()[0]
        review = conn.execute("SELECT COUNT(*) FROM cash_payments WHERE status = 'review'").fetchone()[0]
        cancelled = conn.execute("SELECT COUNT(*) FROM cash_payments WHERE status = 'cancelled' AND day LIKE ?", (month + "%",)).fetchone()[0]
    return {"month": month, "total_usd": round(sum(p["total_usd"] for p in rows), 2), "count": len(rows), "pending": pending, "review": review,
            "cancelled": cancelled, "by_method": sorted(by_method.values(), key=lambda x: -x["usd"]), "by_day": by_day}


def audit(limit: int = 100, payment_id: int | None = None) -> list[dict]:
    with database.get_conn() as conn:
        if payment_id:
            rows = conn.execute("SELECT * FROM cash_audit WHERE payment_id = ? ORDER BY id DESC LIMIT ?", (payment_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cash_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = json.loads(d["details"] or "{}")
        except ValueError:
            d["details"] = {}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Quick text («5 100$ 21.10-22.10», channel captions)
# ---------------------------------------------------------------------------
_AMOUNT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[  .]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s*(\$|usd|долл\w*|€|eur|евро|₽|rub|руб\w*|сум\w*|uzs|so'?m|сўм)?", re.I)


def parse_quick(text: str, ref: date | None = None) -> dict:
    """Free text as the team writes it: apartment number(s), amounts with a currency sign, stay dates, account words.
    Returns {apartments, lines:[{amount, currency, method}], stay, missing:[…]}."""
    t = (text or "").replace(" ", " ")
    out: dict = {"apartments": [], "lines": [], "stay": "", "missing": []}
    st_m = _STAY_RE.search(t) or _STAY_SHORT.search(t)
    if st_m:
        out["stay"] = st_m.group(0)
        t = t.replace(st_m.group(0), " ")
    low = t.lower()
    hint = None
    for kw, name in (("visa", "Карта Visa 9362"), ("хумо", "Карта Хумо 4980"), ("humo", "Карта Хумо 4980"), ("uzcard", "Карта Хумо 4980"), ("узкард", "Карта Хумо 4980"),
                     ("click", "Click Biz"), ("клик", "Click Biz"), ("payme", "Payme Biz"), ("пейми", "Payme Biz"), ("uzum", "Uzum Biz"), ("прилож", "В приложении")):
        if kw in low:
            hint = name
            break
    cash = bool(re.search(r"\bнал|cash|наличн", low))
    # apartments: tokens that are apartment codes or bare numbers matching an apartment, taken before amounts
    for m in re.finditer(r"\b([ABАВ]\s?-?\s?\d{1,3})\b", t, re.I):
        n = norm_apartment(m.group(1))
        if n and n not in out["apartments"]:
            out["apartments"].append(n)
            t = t.replace(m.group(0), " ")
    # the team starts with the apartment: «5 100$», «110 / 400$ …» — a leading number that is an apartment and is
    # followed by another number (not a «000» thousands group) is the apartment, not part of the amount
    lead = re.match(r"\s*(\d{1,3})\s*(?:[/,;:]\s*|\s)(?=[1-9])", t)
    if lead and not out["apartments"]:
        apt = apartment_by_num(lead.group(1))
        if apt:
            out["apartments"].append(apt)
            t = t[lead.end():]
    for m in _AMOUNT_RE.finditer(t):
        raw, frac, cur = m.group(1), m.group(2), (m.group(3) or "").lower()
        digits = int(re.sub(r"\D", "", raw))
        value = float(f"{digits}.{frac}") if frac else float(digits)
        if cur in ("$", "usd") or cur.startswith("долл"):
            c = "USD"
        elif cur in ("€", "eur", "евро"):
            c = "EUR"
        elif cur in ("₽", "rub") or cur.startswith("руб"):
            c = "RUB"
        elif cur:
            c = "UZS"
        elif digits >= 10000 or " " in raw or "." in raw:
            c = "UZS"
        else:
            apt = apartment_by_num(digits) if digits <= 999 else None
            if apt and apt not in out["apartments"] and not frac:
                out["apartments"].append(apt)
                continue
            c = "USD" if digits <= 2000 else "UZS"
        method = hint if hint and currency_of(hint) == c else ("Нал " + c if c in ("USD", "UZS", "EUR") else "RUB")
        if hint and currency_of(hint) != c and not cash:
            method = hint if c == "UZS" and currency_of(hint) == "UZS" else method
        out["lines"].append({"amount": value, "currency": c, "method": method})
    if not out["apartments"]:
        out["missing"].append("квартира")
    if not out["lines"]:
        out["missing"].append("сумма")
    return out


def summary(p: dict) -> str:
    apts = ", ".join(str(_apt_num(a)) for a in p["apartments"])
    lines = " + ".join(f"{_fmt(x['amount'], x['currency'])} ({x['method']})" for x in p["lines"] if not x.get("void"))
    bits = [f"кв. {apts}", lines]
    if p.get("stay_label"):
        bits.append(p["stay_label"])
    if p.get("joint"):
        bits.append(f"деньги в кассе кв. {_apt_num(p['receiver'])}; доли: " + ", ".join(f"{_apt_num(a)}: {v} $" for a, v in (p.get("shares") or {}).items()))
    return " · ".join(bits)


def _fmt(amount, currency) -> str:
    amount = float(amount or 0)
    s = f"{amount:,.2f}".replace(",", " ").replace(".00", "") if currency == "USD" else f"{int(round(amount)):,}".replace(",", " ")
    return f"{s} {'$' if currency == 'USD' else '€' if currency == 'EUR' else '₽' if currency == 'RUB' else 'сум'}"


def _notify(p: dict, who: dict, action: str, before: dict | None = None) -> None:
    """Owners hear about every payment, edit and cancellation (cash is sensitive)."""
    try:
        from . import notify
        head = {"add": "💵 Оплата принята", "edit": "✏️ Оплата изменена", "cancel": "🚫 Оплата отменена"}[action]
        text = f"{head} · {who.get('name') or 'сотрудник'}\n{summary(p)}"
        if action == "edit" and before:
            text += "\nБыло: " + " + ".join(f"{x['amount']} {x['method']}" for x in before.get("lines") or []) + f" · кв. {', '.join(str(_apt_num(a)) for a in before.get('apartments') or [])}"
        if action == "cancel":
            text += f"\nПричина: {p.get('cancel_reason') or ''}"
        if p.get("sheet") == "ok":
            text += "\nВ таблице: строки " + ", ".join(str(x["sheet_row"]) for x in p["lines"] if x.get("sheet_row"))
        elif p["status"] != "cancelled":
            text += "\n⏳ В таблицу пока не записано (повторю автоматически)"
        if p.get("joint") and p["status"] == "review":
            text += "\n⚠️ Совместный платёж за несколько квартир — ждёт вашего решения в CRM → Оплаты"
        targets = list(config.OWNER_TELEGRAM_IDS) if getattr(config, "OWNER_TELEGRAM_IDS", None) else None
        if targets:
            notify.send(text, targets=targets)
    except Exception:  # noqa: BLE001
        logger.exception("cash notify failed")
