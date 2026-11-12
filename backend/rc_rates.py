"""Nightly prices from the RealtyCalendar chessmate — the number the calendar
shows under every day of every apartment. They are mirrored into ``rc_rates``
(one row per apartment per day) so the AI assistant and the CRM quote the
same prices the owner keeps in the calendar.

The calendar has no documented price endpoint for the token the CRM uses, so
the first sync probes a short list of likely requests, keeps the first one
whose answer contains dated prices, and remembers it (setting
``rc_rates_endpoint``). The probe log is kept for the Integrations page.
"""
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

import requests

from . import config, database

logger = logging.getLogger(__name__)

DAYS_AHEAD = 180          # how far ahead prices are mirrored
REFRESH_MIN = 60          # minutes between refreshes once an endpoint is known
PROBE_EVERY_MIN = 6 * 60  # retry the probe this often while no endpoint works
_state: dict = {"last": 0.0, "probe_at": 0.0}

# (name, path, per_apartment). Dates go as DD.MM.YYYY like the rest of the RC API.
CANDIDATES = [
    ("event_calendars", "/v2/event_calendars", False),      # prices may ride along with the bookings
    ("price_calendars", "/v2/price_calendars", False),
    ("prices", "/v2/prices", False),
    ("apartment_prices", "/v2/apartment_prices", False),
    ("rates", "/v2/rates", False),
    ("tariffs", "/v2/tariffs", False),
    ("calendar_prices", "/v2/calendar_prices", False),
    ("apartments/{id}/prices", "/v2/apartments/{id}/prices", True),
    ("apartments/{id}/price_calendar", "/v2/apartments/{id}/price_calendar", True),
    ("apartments/{id}/rates", "/v2/apartments/{id}/rates", True),
]
_DATE_KEYS = ("date", "day", "begin_date", "start_date", "start", "on", "at")
_PRICE_KEYS = ("price", "amount", "cost", "rate", "value", "min_price", "base_price", "daily_price", "price_per_night", "sum")
_MIN_KEYS = ("min_nights", "min_stay", "minimum_stay", "min_days", "minstay", "min_period", "min_length")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DMY = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})")


# ---------------------------------------------------------------------------
# Parsing: find dated prices anywhere in an answer, whatever its shape
# ---------------------------------------------------------------------------
def _day(v) -> str | None:
    if not isinstance(v, str):
        return None
    m = _ISO.match(v)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _DMY.match(v)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(" ", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _first(d: dict, keys) -> object:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def extract(data, apartment_id: int | None = None) -> list[tuple[int, str, float, int | None]]:
    """(apartment_id, YYYY-MM-DD, price, min_nights) for every dated price in ``data``.
    Handles lists of {date, price}, {"2026-10-15": 120} maps, {"2026-10-15": {price, min_stay}}
    maps, and the apartment id from any enclosing {"apartment_id": …} object."""
    out: list[tuple[int, str, float, int | None]] = []

    def walk(node, apt):
        if isinstance(node, dict):
            aid = node.get("apartment_id") or (node.get("id") if isinstance(node.get("events"), list) else None)
            if isinstance(aid, int) and aid in config.APARTMENTS:
                apt = aid
            d = _day(_first(node, _DATE_KEYS))
            p = _num(_first(node, _PRICE_KEYS))
            if d and p is not None and apt is not None and not node.get("client") and node.get("status") is None:
                mn = _num(_first(node, _MIN_KEYS))
                out.append((apt, d, p, int(mn) if mn is not None else None))
            for k, v in node.items():
                kd = _day(k)
                if kd and apt is not None:
                    if _num(v) is not None:
                        out.append((apt, kd, _num(v), None))
                        continue
                    if isinstance(v, dict):
                        pv = _num(_first(v, _PRICE_KEYS))
                        if pv is not None:
                            mn = _num(_first(v, _MIN_KEYS))
                            out.append((apt, kd, pv, int(mn) if mn is not None else None))
                            continue
                if k != "events":  # bookings are not prices
                    walk(v, apt)
        elif isinstance(node, list):
            for v in node:
                walk(v, apt)

    walk(data, apartment_id)
    good = [(a, d, p, m) for a, d, p, m in out if 0 < p < 100000]
    seen: dict[tuple[int, str], tuple] = {}
    for row in good:
        seen[(row[0], row[1])] = row
    return list(seen.values())


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _get(path: str, params: dict) -> tuple[int, object, str]:
    from . import rc_sync
    resp = requests.get(f"{config.RC_BASE_URL}{path}", params=params, headers=rc_sync._headers(), timeout=30)  # noqa: SLF001
    body = resp.text or ""
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data, body[:200]


def _fetch(cand: tuple, d0: date, d1: date) -> tuple[list, dict]:
    """Rows and a diagnostic record for one candidate request."""
    name, path, per_apt = cand
    base = {"begin_date": d0.strftime("%d.%m.%Y"), "end_date": d1.strftime("%d.%m.%Y")}
    rows: list = []
    diag = {"name": name, "status": None, "rows": 0, "note": ""}
    try:
        if per_apt:
            for aid in config.APARTMENTS:
                code, data, note = _get(path.replace("{id}", str(aid)), base)
                diag["status"] = code
                if code != 200 or data is None:
                    diag["note"] = note
                    break
                rows += extract(data, aid)
                if not rows:
                    diag["note"] = note
                    break
        else:
            params = {**base, "apartment_ids": ",".join(str(i) for i in config.APARTMENTS)}
            code, data, note = _get(path, params)
            diag["status"] = code
            if code == 200 and data is not None:
                rows = extract(data)
                if not rows:
                    diag["note"] = note
            else:
                diag["note"] = note
    except Exception as exc:  # noqa: BLE001
        diag["note"] = str(exc)[:200]
    diag["rows"] = len(rows)
    return rows, diag


def _demo_rows(d0: date, d1: date) -> list:
    rows = []
    d = d0
    while d <= d1:
        for aid, name in config.APARTMENTS.items():
            base = 120 if name.startswith("A") else 100
            if d.weekday() >= 5:
                base += 10
            rows.append((aid, d.isoformat(), float(base), 1))
        d += timedelta(days=1)
    return rows


def _store(rows: list, d0: date, d1: date) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with database.get_conn() as conn:
        conn.executemany(
            "INSERT INTO rc_rates (apartment_id, apartment_name, day, price, min_nights, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(apartment_id, day) DO UPDATE SET price = excluded.price, min_nights = excluded.min_nights, apartment_name = excluded.apartment_name, updated_at = excluded.updated_at",
            [(a, config.APARTMENTS.get(a, str(a)), d, p, m, now) for a, d, p, m in rows])
        conn.execute("DELETE FROM rc_rates WHERE day < ?", ((date.today() - timedelta(days=7)).isoformat(),))


def sync(force: bool = False) -> dict:
    """Mirror the next DAYS_AHEAD days of prices. Cheap when nothing is due; the
    probe runs only until an endpoint is found, then at most every 6 h on failure."""
    from . import crm
    now = time.time()
    if not force and now - _state["last"] < REFRESH_MIN * 60:
        return {"ok": True, "skipped": True}
    d0, d1 = date.today(), date.today() + timedelta(days=DAYS_AHEAD)
    if config.DEMO_MODE:
        _store(_demo_rows(d0, d1), d0, d1)
        _state["last"] = now
        crm.set_setting("rc_rates_synced", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return {"ok": True, "rows": len(config.APARTMENTS) * (DAYS_AHEAD + 1), "endpoint": "demo"}
    known = crm.get_setting("rc_rates_endpoint", "")
    order = [c for c in CANDIDATES if c[0] == known] + [c for c in CANDIDATES if c[0] != known]
    if known and not force:
        order = order[:1]
    elif not force and now - _state["probe_at"] < PROBE_EVERY_MIN * 60:
        return {"ok": False, "skipped": True, "error": "тарифы: запрос ещё не найден, следующая попытка позже"}
    diags = []
    for cand in order:
        rows, diag = _fetch(cand, d0, d1)
        diags.append(diag)
        if rows:
            _store(rows, d0, d1)
            _state["last"] = now
            if cand[0] != known:
                crm.set_setting("rc_rates_endpoint", cand[0])
            crm.set_setting("rc_rates_synced", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            crm.set_setting("rc_rates_probe", json.dumps(diags, ensure_ascii=False)[:4000])
            logger.info("rates: %d rows via %s", len(rows), cand[0])
            return {"ok": True, "rows": len(rows), "endpoint": cand[0], "probe": diags}
    _state["probe_at"] = now
    if known:  # the remembered request stopped working: forget it, next run probes again
        crm.set_setting("rc_rates_endpoint", "")
    crm.set_setting("rc_rates_probe", json.dumps(diags, ensure_ascii=False)[:4000])
    logger.warning("rates: no candidate returned prices (%s)", "; ".join(f"{d['name']}:{d['status']}" for d in diags))
    return {"ok": False, "rows": 0, "probe": diags, "error": "ни один запрос к календарю не вернул цены"}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def status() -> dict:
    from . import crm
    with database.get_conn() as conn:
        r = conn.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT apartment_id) AS apts, MIN(day) AS d0, MAX(day) AS d1 FROM rc_rates WHERE day >= date('now')").fetchone()
    probe = crm.get_setting("rc_rates_probe", "")
    try:
        probe = json.loads(probe) if probe else []
    except ValueError:
        probe = []
    return {"rows": r["n"], "apartments": r["apts"], "from": r["d0"], "to": r["d1"], "synced": crm.get_setting("rc_rates_synced", "") or None,
            "endpoint": "demo" if config.DEMO_MODE else (crm.get_setting("rc_rates_endpoint", "") or None), "probe": probe}


def prices(apartment: str, checkin: str, checkout: str) -> dict:
    """Per-night prices of one apartment for [checkin, checkout): total, nights
    priced, min_nights across the stay. ``missing`` lists nights without a price."""
    d0, d1 = date.fromisoformat(checkin[:10]), date.fromisoformat(checkout[:10])
    with database.get_conn() as conn:
        rows = conn.execute("SELECT day, price, min_nights FROM rc_rates WHERE apartment_name = ? AND day >= ? AND day < ? ORDER BY day",
                            (apartment, d0.isoformat(), d1.isoformat())).fetchall()
    by = {r["day"]: r for r in rows}
    nights, total, missing, mins, per = (d1 - d0).days, 0.0, [], [], []
    d = d0
    while d < d1:
        r = by.get(d.isoformat())
        if r is None:
            missing.append(d.isoformat())
        else:
            total += float(r["price"] or 0)
            per.append(float(r["price"] or 0))
            if r["min_nights"]:
                mins.append(int(r["min_nights"]))
        d += timedelta(days=1)
    return {"apartment": apartment, "nights": nights, "priced": len(per), "total": round(total, 2) if per else None,
            "per_night_min": min(per) if per else None, "per_night_max": max(per) if per else None,
            "min_nights": max(mins) if mins else None, "missing": missing}
