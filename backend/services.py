"""Business logic: turns raw bookings into the aggregates the frontend needs."""
from datetime import date, datetime, timedelta

from . import config, database

RU_MONTHS = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
RU_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _iso(d: date) -> str:
    return d.isoformat()


def source_name(source_id) -> str:
    return config.SOURCE_NAMES.get(source_id, "Другое")


def _fmt_ru_date(d: date) -> str:
    return f"{d.day} {RU_MONTHS[d.month]}, {RU_WEEKDAYS[d.weekday()]}"


def apartment_names() -> list[str]:
    """All known units: the static map (refreshed from RC inside the backend
    process) plus names seen in synced bookings — so the bot process, which
    never sees the in-memory refresh, still counts every unit."""
    names = set(config.APARTMENTS.values())
    try:
        names.update(database.distinct_apartments())
    except Exception:  # noqa: BLE001
        pass
    return sorted(n for n in names if n)


def _booking_view(b: dict) -> dict:
    return {
        "id": b.get("id"),
        "apartment": b.get("apartment_name"),
        "client_name": b.get("client_name"),
        "client_phone": b.get("client_phone"),
        "arrival_time": b.get("arrival_time"),
        "departure_time": b.get("departure_time"),
        "days": b.get("days_count"),
        "amount_usd": round(b.get("amount") or 0, 2),
        "debt_usd": round(b.get("debt") or 0, 2),
        "is_paid": (b.get("debt") or 0) <= 0,
        "source": source_name(b.get("source_id")),
        "source_id": b.get("source_id"),
        "status": b.get("status"),
        "short_notes": b.get("short_notes"),
        "checkin": b.get("begin_date"),
        "checkout": b.get("end_date"),
    }


def _cleaning_for(apt: str, cleaning_date: str, bookings: list[dict]) -> dict:
    """Compute the cleaning entry for an apartment on the checkout date."""
    # find the next check-in for this apartment strictly after cleaning_date
    next_checkin = None
    next_arrival = None
    for b in sorted(bookings, key=lambda x: x["begin_date"]):
        if b["apartment_name"] != apt:
            continue
        if b["begin_date"] >= cleaning_date:
            next_checkin = b["begin_date"]
            next_arrival = b.get("arrival_time")
            break
    reason = "turnover" if next_checkin == cleaning_date else "checkout"
    status = database.get_cleaning_status(apt, cleaning_date)
    window_hours = None
    entry = {
        "apartment": apt,
        "cleaning_date": cleaning_date,
        "reason": reason,
        "next_checkin": next_checkin,
        "next_arrival_time": next_arrival,
        "status": status,
        "window_hours": window_hours,
    }
    # who is cleaning / cleaned it and how long it took (from the до/после reports)
    try:
        s = database.session_for(apt, cleaning_date)
    except Exception:  # noqa: BLE001
        s = None
    if s:
        forced = bool(s.get("forced"))
        entry["cleaner"] = s.get("staff_name") or ""
        entry["started_at"] = (s.get("started_at") or "")[11:16] or None
        entry["finished_at"] = None if forced else ((s.get("finished_at") or "")[11:16] or None)
        entry["duration_min"] = None if forced else s.get("duration_min")
        entry["forced"] = forced           # closed without a «после» report
        entry["no_before"] = bool(s.get("no_before"))
    return entry


def _window_hours(departure_time: str | None, next_arrival: str | None, same_day: bool) -> int | None:
    if not departure_time:
        return None
    try:
        dep_h, dep_m = (int(x) for x in departure_time.split(":"))
    except (ValueError, AttributeError):
        return None
    if not next_arrival or not same_day:
        return None
    try:
        arr_h, arr_m = (int(x) for x in next_arrival.split(":"))
    except (ValueError, AttributeError):
        return None
    minutes = (arr_h * 60 + arr_m) - (dep_h * 60 + dep_m)
    return max(0, round(minutes / 60)) if minutes > 0 else None


def build_day(target: date) -> dict:
    """Compute the full "today"/"tomorrow" dashboard payload for a date."""
    today_str = _iso(target)
    bookings = database.all_active_bookings()

    checkins = [b for b in bookings if b["begin_date"] == today_str]
    checkouts = [b for b in bookings if b["end_date"] == today_str]
    current = [b for b in bookings if b["begin_date"] <= today_str < b["end_date"]]
    debtors = [b for b in current if (b.get("debt") or 0) > 0]

    # cleanings happen on checkout days
    cleanings = []
    for b in checkouts:
        c = _cleaning_for(b["apartment_name"], today_str, bookings)
        c["departure_time"] = b.get("departure_time")
        c["window_hours"] = _window_hours(
            b.get("departure_time"), c.get("next_arrival_time"),
            same_day=(c.get("next_checkin") == today_str),
        )
        cleanings.append(c)

    expected_today = sum((b.get("debt") or 0) for b in checkins if (b.get("debt") or 0) > 0)
    debtors_total = sum((b.get("debt") or 0) for b in debtors)

    # count distinct apartments in-house, capped at the real inventory, so a
    # stray overlapping request in RC can never push occupancy above 100%.
    names = apartment_names()
    total = len(names) or config.TOTAL_APARTMENTS
    occupied = len({b["apartment_name"] for b in current} & set(names))
    occupancy_pct = round(occupied / total * 100, 1) if total else 0

    # new bookings in the last 24h
    cutoff = datetime.now() - timedelta(hours=24)
    new_bookings = []
    for b in bookings:
        created = b.get("created_at")
        if not created:
            continue
        try:
            created_dt = datetime.fromisoformat(created)
        except (ValueError, TypeError):
            continue
        if created_dt.tzinfo is not None:
            # RC may send an offset ("+05:00"); compare in local naive time
            created_dt = created_dt.astimezone().replace(tzinfo=None)
        if created_dt >= cutoff:
            v = _booking_view(b)
            new_bookings.append({
                "apartment": v["apartment"],
                "client_name": v["client_name"],
                "checkin": v["checkin"],
                "checkout": v["checkout"],
                "days": v["days"],
                "amount_usd": v["amount_usd"],
                "source": v["source"],
            })

    payload = {
        "date": today_str,
        "date_human": _fmt_ru_date(target),
        "stats": {
            "checkins_today": len(checkins),
            "checkouts_today": len(checkouts),
            "cleanings_today": len(cleanings),
            "occupied": occupied,
            "total": total,
            "occupancy_pct": occupancy_pct,
        },
        "finance": {
            "expected_today_usd": round(expected_today, 2),
            "debtors_count": len(debtors),
            "debtors_total_usd": round(debtors_total, 2),
        },
        "checkins": [_booking_view(b) for b in sorted(checkins, key=lambda x: x["apartment_name"])],
        "checkouts": [
            {
                "apartment": b["apartment_name"],
                "client_name": b.get("client_name"),
                "departure_time": b.get("departure_time"),
                "cleaning_status": database.get_cleaning_status(b["apartment_name"], today_str),
            }
            for b in sorted(checkouts, key=lambda x: x["apartment_name"])
        ],
        "cleanings": cleanings,
        "debtors": [
            {
                "apartment": b["apartment_name"],
                "client_name": b.get("client_name"),
                "client_phone": b.get("client_phone"),
                "checkin_date": b["begin_date"],
                "checkout_date": b["end_date"],
                "debt_usd": round(b.get("debt") or 0, 2),
                "days_staying": _days_staying(b, target),
            }
            for b in sorted(debtors, key=lambda x: x["apartment_name"])
        ],
        "new_bookings_24h": new_bookings,
        "last_sync": database.last_sync(),
    }
    return payload


def _days_staying(b: dict, target: date) -> int:
    try:
        begin = date.fromisoformat(b["begin_date"])
    except (ValueError, KeyError):
        return 0
    return max(0, (target - begin).days)


def build_cleaning(target: date, days: int = 2) -> dict:
    """Cleaning list for `days` days starting at target (checkout-driven)."""
    bookings = database.all_active_bookings()
    result = []
    for offset in range(max(1, days)):
        day = target + timedelta(days=offset)
        day_str = _iso(day)
        for b in sorted(bookings, key=lambda x: x["apartment_name"]):
            if b["end_date"] != day_str:
                continue
            c = _cleaning_for(b["apartment_name"], day_str, bookings)
            c["departure_time"] = b.get("departure_time")
            c["window_hours"] = _window_hours(
                b.get("departure_time"), c.get("next_arrival_time"),
                same_day=(c.get("next_checkin") == day_str),
            )
            result.append(c)
    return {"cleanings": result}


def build_guests(target: date) -> dict:
    today_str = _iso(target)
    bookings = database.all_active_bookings()
    current = [b for b in bookings if b["begin_date"] <= today_str < b["end_date"]]
    current.sort(key=lambda x: x["apartment_name"])
    debtors = [b for b in current if (b.get("debt") or 0) > 0]
    return {
        "date": today_str,
        "current_count": len(current),
        "debtors": [
            {
                "apartment": b["apartment_name"],
                "client_name": b.get("client_name"),
                "client_phone": b.get("client_phone"),
                "checkin_date": b["begin_date"],
                "checkout_date": b["end_date"],
                "debt_usd": round(b.get("debt") or 0, 2),
            }
            for b in debtors
        ],
        "guests": [
            {
                "apartment": b["apartment_name"],
                "client_name": b.get("client_name"),
                "client_phone": b.get("client_phone"),
                "checkout_date": b["end_date"],
                "is_paid": (b.get("debt") or 0) <= 0,
                "debt_usd": round(b.get("debt") or 0, 2),
            }
            for b in current
        ],
    }


def build_occupancy(target: date, span: int = 7) -> dict:
    bookings = database.all_active_bookings()
    dates = [target + timedelta(days=i) for i in range(span)]
    date_strs = [_iso(d) for d in dates]

    apartments = []
    names = apartment_names()
    for apt_name in names:
        days = []
        for ds in date_strs:
            occupant = None
            for b in bookings:
                if b["apartment_name"] == apt_name and b["begin_date"] <= ds < b["end_date"]:
                    occupant = b
                    break
            days.append({
                "date": ds,
                "status": "occupied" if occupant else "free",
                "client": occupant.get("client_name") if occupant else None,
                "amount_usd": round(occupant.get("amount") or 0, 2) if occupant else None,
                "checkin": occupant.get("begin_date") if occupant else None,
                "checkout": occupant.get("end_date") if occupant else None,
            })
        apartments.append({"name": apt_name, "days": days})

    total = len(names)
    total_cells = span * total
    occupied_cells = sum(
        1 for a in apartments for d in a["days"] if d["status"] == "occupied"
    )
    pct = round(occupied_cells / total_cells * 100) if total_cells else 0
    # per-day figures: how many units are taken on each date (day-by-day view)
    per_day = []
    for i, ds in enumerate(date_strs):
        occ = sum(1 for a in apartments if a["days"][i]["status"] == "occupied")
        per_day.append({"date": ds, "occupied": occ, "total": total,
                        "pct": round(occ / total * 100) if total else 0})
    today_iso = _iso(date.today())
    today_row = next((p for p in per_day if p["date"] == today_iso), per_day[0] if per_day else None)
    return {
        "dates": date_strs,
        "weekdays": [RU_WEEKDAYS[d.weekday()] for d in dates],
        "day_numbers": [d.day for d in dates],
        "occupancy_pct": pct,
        "per_day": per_day,
        "today": today_row,
        "apartments": apartments,
    }


def _fmt_short(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    return f"{d.day} {RU_MONTHS[d.month]}"


def _task_view(t: dict, today: date) -> dict:
    deadline = t.get("deadline")
    dtime = t.get("deadline_time")
    today_str = _iso(today)
    overdue = bool(deadline and t.get("status") == "open" and deadline < today_str)
    due_today = bool(deadline and t.get("status") == "open" and deadline == today_str)
    human = _fmt_short(deadline)
    if human and dtime:
        human = f"{human} {dtime}"
    return {
        "id": t.get("id"),
        "apartment": t.get("apartment_name"),
        "title": t.get("title"),
        "deadline": deadline,
        "deadline_time": dtime,
        "deadline_human": human,
        "status": t.get("status"),
        "overdue": overdue,
        "due_today": due_today,
        "done_at": t.get("done_at"),
    }


def build_tasks(target: date) -> dict:
    """All tasks (formatted) plus the apartment list for the add form."""
    tasks = [_task_view(t, target) for t in database.list_tasks()]
    open_tasks = [t for t in tasks if t["status"] == "open"]
    due_soon = sum(1 for t in open_tasks if t["overdue"] or t["due_today"])
    return {
        "today": _iso(target),
        "tasks": tasks,
        "apartments": apartment_names(),
        "counts": {
            "open": len(open_tasks),
            "due_soon": due_soon,
            "done": sum(1 for t in tasks if t["status"] == "done"),
        },
    }


def build_tomorrow_schedule_text(target: date) -> str:
    """Nice evening (22:00) preview of tomorrow for the team group."""
    d = build_day(target)
    s = d["stats"]
    lines = [
        f"📋 План на завтра — {d['date_human']}",
        "",
        f"🔑 Заезды: {s['checkins_today']}   🚪 Выезды: {s['checkouts_today']}   🧹 Уборки: {s['cleanings_today']}",
    ]
    if d["checkins"]:
        lines.append("")
        lines.append("🔑 Заезды:")
        for b in sorted(d["checkins"], key=lambda x: x.get("arrival_time") or "99"):
            t = b.get("arrival_time") or "время —"
            lines.append(f"  • {b['apartment']} — {b.get('client_name') or 'Гость'}, {t}")
    if d["cleanings"]:
        lines.append("")
        lines.append("🧹 Уборки:")
        for c in d["cleanings"]:
            if c.get("next_checkin") == c.get("cleaning_date"):
                nxt = f"заезд {c.get('next_arrival_time') or '—'}"
            elif c.get("next_checkin"):
                nxt = f"заезд {_fmt_short(c['next_checkin'])}"
            else:
                nxt = "без заезда"
            dep = c.get("departure_time") or "—"
            lines.append(f"  • {c['apartment']}: выезд {dep} → {nxt}")
    if not d["checkins"] and not d["cleanings"]:
        lines.append("")
        lines.append("Заездов и уборок на завтра нет — спокойный день 🙂")
    return "\n".join(lines)


def build_text_summary(target: date) -> str:
    """Plain-text daily summary used by the Telegram bot /today command."""
    d = build_day(target)
    s = d["stats"]
    f = d["finance"]

    def names(items, key="apartment"):
        return ", ".join(i[key] for i in items) or "—"

    lines = [
        f"📅 Nova Home — {d['date_human']}",
        "",
        f"🔑 Заезды ({s['checkins_today']}): {names(d['checkins'])}",
        f"🚪 Выезды ({s['checkouts_today']}): {names(d['checkouts'])}",
    ]
    if d["cleanings"]:
        cl = ", ".join(
            f"{c['apartment']}" + (f" до {c['next_arrival_time']}" if c.get('next_arrival_time') else "")
            for c in d["cleanings"]
        )
        lines.append(f"🧹 Уборки ({s['cleanings_today']}): {cl}")
    else:
        lines.append(f"🧹 Уборки ({s['cleanings_today']}): —")
    lines.append(f"🏠 Занятость: {s['occupied']}/{s['total']} ({int(s['occupancy_pct'])}%)")
    lines.append(f"💰 Ожидается: ${int(f['expected_today_usd'])}")
    if f["debtors_count"]:
        lines.append(
            f"⚠️ Должники ({f['debtors_count']}): ${int(f['debtors_total_usd'])} — "
            + names(d["debtors"])
        )
    if d["new_bookings_24h"]:
        nb = ", ".join(b["apartment"] for b in d["new_bookings_24h"])
        lines.append(f"📬 Новые брони ({len(d['new_bookings_24h'])}): {nb}")
    return "\n".join(lines)
