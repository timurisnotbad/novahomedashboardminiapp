"""Background sync scheduler (APScheduler)."""
import logging
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, notify, rc_sync, reminders, services

logger = logging.getLogger("nova.scheduler")

_scheduler: BackgroundScheduler | None = None


def _evening_summary():
    try:
        text = services.build_tomorrow_schedule_text(date.today() + timedelta(days=1))
        notify.send(text)
        logger.info("Evening summary sent")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Evening summary failed: %s", exc)


def _job():
    try:
        count = rc_sync.sync_to_db()
        logger.info("Sync OK: %s bookings", count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sync failed: %s", exc)
        return
    try:
        reminders.check_booking_changes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Booking-change check failed: %s", exc)


def _reminder_job():
    try:
        reminders.check_due_tasks()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reminder check failed: %s", exc)


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _job,
        "interval",
        minutes=config.SYNC_INTERVAL_MINUTES,
        id="rc_sync",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _reminder_job,
        "interval",
        minutes=10,
        id="task_reminders",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _evening_summary,
        "cron",
        hour=22,
        minute=0,
        id="evening_summary",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started (sync %s min, reminders 10 min, summary 22:00)",
                config.SYNC_INTERVAL_MINUTES)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
