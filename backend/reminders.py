"""Deadline reminders and new-booking notifications (driven by the scheduler)."""
import logging
from datetime import date, datetime, timedelta

from . import config, database, notify, services

logger = logging.getLogger("nova.reminders")

# hours-before-deadline → notification kind (for de-dup)
_THRESHOLDS = [(24, "due_24h"), (8, "due_8h"), (3, "due_3h"), (1, "due_1h")]


def _deadline_dt(task: dict):
    d = task.get("deadline")
    if not d:
        return None
    try:
        dd = date.fromisoformat(d)
    except (ValueError, TypeError):
        return None
    t = task.get("deadline_time")
    if t:
        try:
            hh, mm = (int(x) for x in str(t).split(":")[:2])
            return datetime(dd.year, dd.month, dd.day, hh, mm)
        except (ValueError, TypeError):
            pass
    return None  # date-only → handled separately


def _label(task: dict) -> str:
    apt = task.get("apartment_name")
    return (f"{apt}: " if apt else "") + (task.get("title") or "")


def check_due_tasks(now: datetime | None = None) -> None:
    now = now or datetime.now()
    for task in database.open_tasks_with_deadline():
        dt = _deadline_dt(task)
        if dt is not None:
            # After downtime several thresholds may be due at once: send only
            # the nearest one, mark the others as handled (no 4-message burst).
            due = [(hours, kind) for hours, kind in _THRESHOLDS
                   if dt - timedelta(hours=hours) <= now < dt
                   and not database.task_notif_sent(task["id"], kind)]
            if not due:
                continue
            hours, kind = min(due)
            for _h, k in due:
                database.mark_task_notif(task["id"], k)
            left = "1 час" if hours == 1 else f"{hours} ч"
            notify.send(
                f"⏰ Через {left} — дедлайн задачи:\n{_label(task)}\n"
                f"Срок: {dt.strftime('%d.%m %H:%M')}"
            )
        else:
            try:
                dd = date.fromisoformat(task["deadline"])
            except (ValueError, TypeError):
                continue
            if now.date() == dd and now.hour >= 8 and database.mark_task_notif(task["id"], "due_day"):
                notify.send(f"📌 Сегодня дедлайн задачи:\n{_label(task)}")


def prime_task(task_id) -> None:
    """Called right after a task is created: suppress reminder thresholds whose
    window has already passed, so a task set with e.g. <24h left won't fire the
    24-hour reminder."""
    task = database.get_task(task_id)
    if not task:
        return
    dt = _deadline_dt(task)
    if dt is None:
        return
    now = datetime.now()
    for hours, kind in _THRESHOLDS:
        if dt - timedelta(hours=hours) <= now:
            database.mark_task_notif(task_id, kind)


def _new_booking_msg(b: dict) -> str:
    return (
        f"🆕 Новая бронь · {b.get('apartment_name') or ''}\n"
        f"👤 {b.get('client_name') or 'Гость'}\n"
        f"🌐 Источник: {services.source_name(b.get('source_id'))}\n"
        f"💵 Сумма: ${round(b.get('amount') or 0)}\n"
        f"📞 Телефон: {b.get('client_phone') or '—'}\n"
        f"📅 Заезд {b.get('begin_date')} → выезд {b.get('end_date')}"
    )


def _booking_diffs(old: dict, b: dict) -> list:
    diffs = []
    if (old.get("apartment_name") or "") != (b.get("apartment_name") or ""):
        diffs.append(f"🏠 Квартира: {old.get('apartment_name')} → {b.get('apartment_name')}")
    if old.get("begin_date") != b.get("begin_date") or old.get("end_date") != b.get("end_date"):
        diffs.append(f"📅 Даты: {old.get('begin_date')}–{old.get('end_date')} → "
                     f"{b.get('begin_date')}–{b.get('end_date')}")
    if round(old.get("amount") or 0) != round(b.get("amount") or 0):
        diffs.append(f"💵 Сумма: ${round(old.get('amount') or 0)} → ${round(b.get('amount') or 0)}")
    if (old.get("status") or "") != (b.get("status") or ""):
        diffs.append(f"Статус: {old.get('status')} → {b.get('status')}")
    if (old.get("client_name") or "") != (b.get("client_name") or ""):
        diffs.append(f"👤 Гость: {old.get('client_name')} → {b.get('client_name')}")
    if (old.get("client_phone") or "") != (b.get("client_phone") or ""):
        diffs.append(f"📞 Телефон: {old.get('client_phone')} → {b.get('client_phone')}")
    return diffs


def _after_hours_target(now: datetime):
    """During the window between the 22:00 team report and the next morning shift
    start, return the date the team is preparing cleanings for — so an overnight
    booking (e.g. one that lands at 02:00) triggers a fresh schedule. Outside that
    window return None (daytime changes are covered by the next 22:00 report)."""
    try:
        sh, sm = (int(x) for x in str(config.SHIFT_START).split(":")[:2])
    except (ValueError, TypeError):
        sh, sm = 11, 0
    if now.hour >= 22:
        return now.date() + timedelta(days=1)   # evening → tomorrow
    if (now.hour, now.minute) < (sh, sm):
        return now.date()                       # after midnight → today (was "tomorrow" at 22:00)
    return None


def _touches(target, booking: dict) -> bool:
    """True if the booking's stay covers the target date (so its check-in,
    check-out or a mid-stay cleaning falls on that day)."""
    try:
        bd = date.fromisoformat(booking.get("begin_date"))
        ed = date.fromisoformat(booking.get("end_date"))
    except (ValueError, TypeError):
        return False
    return bd <= target <= ed


def check_booking_changes() -> None:
    """Notify (owner's private chat only, to keep the group clean) about new and
    edited bookings. First run just records a baseline snapshot — no spam.

    Additionally, if a change happens after the 22:00 team report (until the next
    morning shift) and it affects the upcoming work day, re-send the updated
    cleaning schedule to the team so the plan always reflects the latest bookings."""
    bookings = database.all_active_bookings()
    snaps = database.get_booking_snapshots()
    owners = config.OWNER_TELEGRAM_IDS or None
    if not snaps:
        database.upsert_booking_snapshots(bookings)
        return
    target = _after_hours_target(datetime.now())
    schedule_dirty = False
    for b in bookings:
        bid = b.get("id")
        old = snaps.get(bid)
        if old is None:
            notify.send(_new_booking_msg(b), targets=owners)
            if target and _touches(target, b):
                schedule_dirty = True
        else:
            diffs = _booking_diffs(old, b)
            if diffs:
                notify.send(
                    f"🔁 Бронь изменена · {b.get('apartment_name') or ''}\n"
                    f"👤 {b.get('client_name') or 'Гость'}\n" + "\n".join(diffs),
                    targets=owners,
                )
                if target and (_touches(target, b) or _touches(target, old)):
                    schedule_dirty = True
    database.upsert_booking_snapshots(bookings)
    try:  # snapshots of stays that ended over a year ago are dead weight
        database.prune_booking_snapshots((date.today() - timedelta(days=365)).isoformat())
    except Exception:  # noqa: BLE001
        logger.exception("snapshot prune failed")
    if target and schedule_dirty:
        try:
            text = services.build_tomorrow_schedule_text(target)
            # Same key as the 22:00 plan: the plan message is edited in place
            # (or replaced), and an unchanged schedule sends nothing — no more
            # "🔄 График изменился" every sync cycle.
            stamp = datetime.now().strftime("%H:%M")
            notify.send_replacing(
                f"plan:{target.isoformat()}",
                f"🔄 График изменился после вечернего отчёта — актуальная версия "
                f"(обновлено {stamp}):\n\n" + text,
                topic="cleaning",
                dedupe=text,
            )
        except Exception:  # noqa: BLE001
            logger.exception("After-hours schedule refresh failed")
