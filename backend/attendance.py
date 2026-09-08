"""Staff attendance via live location.

A staff member shares a *live* location with the bot when they reach the work
zone (Tashkent City). The first live point that falls inside the geofence marks
their arrival for the day; static locations are ignored (they can be faked).
"""
from datetime import datetime
from math import asin, cos, radians, sin, sqrt

from . import config, database


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * 6371000 * asin(sqrt(a))


def in_zone(lat: float, lng: float) -> bool:
    return haversine_m(lat, lng, config.WORK_LAT, config.WORK_LNG) <= config.WORK_RADIUS_M


def _lateness(now: datetime):
    try:
        hh, mm = (int(x) for x in config.SHIFT_START.split(":")[:2])
    except ValueError:
        hh, mm = 9, 0
    shift = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    late = int((now - shift).total_seconds() // 60)
    on_time = late <= config.SHIFT_GRACE_MIN
    return on_time, max(0, late)


def check_arrival(staff_id, staff_name, lat, lng, now=None) -> dict:
    """Evaluate a live-location point and record the arrival if it qualifies.

    Always returns a dict so the bot can give the user feedback on every case
    (instead of silently ignoring points, which is indistinguishable from a dead
    bot). Keys:
      status  — "recorded" | "already" | "outside"
      distance — metres from the work point
      reply   — message to send back to the person who shared the location
      notify  — (only on "recorded") broadcast text for owner/team group
    """
    now = now or datetime.now()
    hm = (now.hour, now.minute)
    if hm < config.ATTEND_EARLIEST_T:
        eh, em = config.ATTEND_EARLIEST_T
        return {
            "status": "too_early",
            "distance": 0,
            "reply": f"⏳ Приход отмечается с {eh:02d}:{em:02d} — пришлите live-локацию позже.",
        }
    if hm >= config.ATTEND_DEADLINE_T:  # the roll call runs at exactly this minute
        dh, dm = config.ATTEND_DEADLINE_T
        return {
            "status": "too_late",
            "distance": 0,
            "reply": (
                f"⛔ Окно отметки закрыто (до {dh:02d}:{dm:02d}) — приход не засчитан. "
                f"Свяжитесь с руководителем."
            ),
        }
    dist = haversine_m(lat, lng, config.WORK_LAT, config.WORK_LNG)
    if dist > config.WORK_RADIUS_M:
        return {
            "status": "outside",
            "distance": dist,
            "reply": (
                f"❌ Вы вне рабочей зоны — до точки ~{int(dist)} м "
                f"(нужно ≤ {config.WORK_RADIUS_M} м). Приход не засчитан."
            ),
        }
    on_time, late = _lateness(now)
    fresh = database.record_arrival(
        staff_id, staff_name, now.date().isoformat(),
        now.isoformat(timespec="seconds"), lat, lng, on_time, late,
    )
    if not fresh:
        return {
            "status": "already",
            "distance": dist,
            "reply": "Вы уже отмечены сегодня ✅",
        }
    status = "вовремя ✅" if on_time else f"с опозданием {late} мин ⚠️"
    return {
        "status": "recorded",
        "distance": dist,
        "reply": "✅ Приход отмечен, спасибо!",
        "notify": (
            f"🟢 {staff_name} пришёл(ла) на работу в {now.strftime('%H:%M')} — {status}"
        ),
    }


def register_arrival(staff_id, staff_name, lat, lng, now=None):
    """Backwards-compatible wrapper: returns the broadcast text on a fresh
    arrival, else None."""
    res = check_arrival(staff_id, staff_name, lat, lng, now)
    return res.get("notify") if res.get("status") == "recorded" else None
