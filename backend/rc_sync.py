"""Synchronisation with the Realty Calendar API.

In DEMO_MODE (no RC token configured) a realistic dataset is generated relative
to today's date so the dashboard is fully populated for development and demos.
"""
import logging
import random
import re
from datetime import date, datetime, timedelta

import requests

from . import config, database

logger = logging.getLogger("nova.rc")


def _short_code(title: str) -> str:
    """Normalize an RC apartment title to the short code used everywhere in
    the dashboard: 'NEST B-003' -> 'B-003'. Falls back to the full title when
    no code pattern is found."""
    m = re.search(r"\b([A-Za-zА-Яа-я])\s*-?\s*(\d{3})\b", title)
    if not m:
        return title
    letter = m.group(1).upper().translate(str.maketrans({"А": "A", "В": "B", "Б": "B", "С": "C", "Е": "E"}))
    return f"{letter}-{m.group(2)}"


def refresh_apartments() -> None:
    """Auto-discover the apartment list from Realty Calendar, so units added
    there show up in the dashboard and cleaning lists without code changes.
    Keeps the current (static) map on any error, so sync never breaks."""
    try:
        resp = requests.get(
            f"{config.RC_BASE_URL}/v2/apartments", headers=_headers(), timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "apartment auto-discovery failed (%s) — keeping known list of %d",
            exc, len(config.APARTMENTS),
        )
        return
    if isinstance(data, list):
        items = data
    else:
        items = data.get("apartments") or data.get("items") or data.get("data") or []
    found: dict[int, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        aid = it.get("id") or it.get("apartment_id")
        title = str(it.get("title") or it.get("name") or it.get("short_title") or "").strip()
        if isinstance(aid, int) and title:
            found[aid] = _short_code(title)
    if not found:
        logger.warning(
            "apartment auto-discovery returned nothing usable — keeping %d known",
            len(config.APARTMENTS),
        )
        return
    added = set(found) - set(config.APARTMENTS)
    config.APARTMENTS.clear()
    config.APARTMENTS.update(found)
    config.TOTAL_APARTMENTS = len(found)
    if added:
        logger.info(
            "RC apartments: %d total, NEW: %s",
            len(found), ", ".join(found[a] for a in sorted(added)),
        )
    else:
        logger.info("RC apartments: %d total", len(found))


def _headers() -> dict:
    return {
        "Accept": "application/json",
        "x-user-token": config.RC_TOKEN,
        "x-locale": "ru",
        "Cookie": "; ".join(f"{k}={v}" for k, v in config.RC_COOKIES.items()),
        "Referer": "https://realtycalendar.ru/chessmate/",
    }


def fetch_bookings(date_from: date, date_to: date) -> list[dict]:
    """Fetch all bookings from Realty Calendar for the date range.

    Dates are sent as DD.MM.YYYY and apartment_ids as a comma-separated string,
    exactly as the RC API expects.
    """
    apt_ids = ",".join(str(i) for i in config.APARTMENTS.keys())
    params = {
        "apartment_ids": apt_ids,
        "begin_date": date_from.strftime("%d.%m.%Y"),
        "end_date": date_to.strftime("%d.%m.%Y"),
        "statuses[]": ["booked", "paid", "confirmed", "prepaid", "not_confirmed"],
    }
    resp = requests.get(
        f"{config.RC_BASE_URL}/v2/event_calendars",
        params=params,
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()

    bookings: list[dict] = []
    for item in resp.json().get("items", []):
        apt_id = item.get("apartment_id")
        apt_name = config.APARTMENTS.get(apt_id, str(apt_id))
        for event in item.get("events", []):
            if event.get("is_delete"):
                continue
            client = event.get("client") or {}
            bookings.append({
                **event,
                "apartment_id": apt_id,
                "apartment_name": apt_name,
                "client_name": client.get("fio"),
                "client_phone": client.get("phone"),
            })
    return bookings


def sync_to_db() -> int:
    """Main sync entry point — called on startup and every N minutes.

    Both the demo seeder and the live RC fetch return the *complete* set of
    bookings for the working window, so we always do a full refresh. This keeps
    the table an exact mirror of the source: bookings cancelled in RC disappear,
    and switching from DEMO_MODE to live data never leaves stale rows behind.
    """
    today = date.today()
    try:
        if config.DEMO_MODE:
            bookings = generate_demo_bookings(today)
        else:
            refresh_apartments()  # pick up units newly added in RC
            bookings = fetch_bookings(today - timedelta(days=1), today + timedelta(days=30))
        database.replace_all_bookings(bookings)
        database.log_sync(len(bookings), True)
        return len(bookings)
    except Exception:  # noqa: BLE001 - log failure, keep the app alive
        database.log_sync(0, False)
        raise


# ---------------------------------------------------------------------------
# Demo data generator
# ---------------------------------------------------------------------------
_DEMO_GUESTS = [
    ("Иванов Иван", "+998901234567"), ("Петров Петр", "+998901112233"),
    ("Сидоров Алексей", "+998907778899"), ("John Smith", "+441234567890"),
    ("Talgat Ergeshov", "+998935556677"), ("Igor Hasanov", None),
    ("Moldir Kadyrova", "+998977654321"), ("Anna Lee", "+821012345678"),
    ("Дмитрий Волков", "+998901010101"), ("Sofia Rossi", "+390612345678"),
    ("Ахмед Каримов", "+998935553311"), ("Emma Brown", "+15551234567"),
    ("Нурбек Асанов", "+996555112233"), ("Chen Wei", "+8613800138000"),
    ("Ольга Смирнова", "+998901239876"), ("David Kim", None),
]


def generate_demo_bookings(today: date) -> list[dict]:
    """Build a deterministic-ish but varied dataset around `today`."""
    rng = random.Random(today.toordinal())  # stable within a day
    bookings: list[dict] = []
    bid = 172428000
    sources = [None, 2, 3]

    apt_items = list(config.APARTMENTS.items())
    # ~70% occupancy target: place staggered stays across the week window.
    for apt_id, apt_name in apt_items:
        cursor = today - timedelta(days=6)
        end_window = today + timedelta(days=10)
        while cursor < end_window:
            if rng.random() < 0.35:  # gap between stays
                cursor += timedelta(days=rng.randint(1, 3))
                continue
            nights = rng.randint(2, 5)
            begin = cursor
            end = begin + timedelta(days=nights)
            source = rng.choice(sources)
            amount = round(nights * rng.choice([60, 75, 90, 110]), 1)
            # Guests currently in-house and today's arrivals may owe money.
            if source is None or rng.random() < 0.4:
                debt = amount if rng.random() < 0.45 else 0
            else:
                debt = 0
            guest = rng.choice(_DEMO_GUESTS)
            bookings.append({
                "id": bid,
                "apartment_id": apt_id,
                "apartment_name": apt_name,
                "begin_date": begin.isoformat(),
                "end_date": end.isoformat(),
                "status": rng.choice(["booked", "paid", "confirmed", "prepaid"]),
                "days_count": nights,
                "amount": amount,
                "price": amount,
                "debt": debt,
                "prepayment": 0 if debt else amount,
                "prepayment_progress": 0.0 if debt else 100.0,
                "is_external": source is not None,
                "source_id": source,
                "client_name": guest[0],
                "client_phone": guest[1],
                "arrival_time": rng.choice(["13:00", "14:00", "15:00", "16:00"]),
                "departure_time": rng.choice(["11:00", "12:00"]),
                "short_notes": rng.choice(["", "Гостей 2", "Гостей 3", "Поздний заезд"]),
                "is_delete": False,
            })
            bid += 1
            cursor = end  # next stay starts at checkout (turnover)

    # A couple of brand-new bookings within the last 24h (future stays).
    for _ in range(2):
        apt_id, apt_name = rng.choice(apt_items)
        begin = today + timedelta(days=rng.randint(5, 12))
        nights = rng.randint(2, 4)
        guest = rng.choice(_DEMO_GUESTS)
        amount = round(nights * 90, 1)
        b = {
            "id": bid,
            "apartment_id": apt_id,
            "apartment_name": apt_name,
            "begin_date": begin.isoformat(),
            "end_date": (begin + timedelta(days=nights)).isoformat(),
            "status": "booked",
            "days_count": nights,
            "amount": amount,
            "price": amount,
            "debt": amount,
            "prepayment": 0,
            "prepayment_progress": 0.0,
            "is_external": True,
            "source_id": 2,
            "client_name": guest[0],
            "client_phone": guest[1],
            "arrival_time": "16:00",
            "departure_time": "11:00",
            "short_notes": "Новая бронь",
            "is_delete": False,
            "_created_recently": True,
        }
        bookings.append(b)
        bid += 1

    # Mark ~half the bookings as recently-created for the "new in 24h" feed.
    now = datetime.now()
    for b in bookings:
        if b.pop("_created_recently", False):
            b["created_at"] = now.isoformat(timespec="seconds")
    return bookings
