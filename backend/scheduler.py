"""Background sync scheduler (APScheduler)."""
import logging
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, notify, rc_sync, reminders, services, watchdog

logger = logging.getLogger("nova.scheduler")

_scheduler: BackgroundScheduler | None = None


def _evening_summary():
    try:
        target = date.today() + timedelta(days=1)
        text = services.build_tomorrow_schedule_text(target)
        # one plan message per day: after-hours booking changes edit this very
        # message instead of posting another copy (see reminders.py)
        notify.send_replacing(f"plan:{target.isoformat()}", text, topic="cleaning", dedupe=text)
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
    try:
        from . import crm, crm_amo, crm_ext
        crm.import_clients()  # every guest from the calendar and the chats has a card
        crm_amo.backfill_deals()  # every chat is a lead on the board
        n = crm_ext.sync_deals_from_bookings()  # deals follow their bookings
        crm_ext.ensure_flow_stages()
        crm_ext.booking_flow()  # ...and move along the stay: ожидает оплаты → забронировано → заселён → выехал
        if n:
            logger.info("CRM deals refreshed from bookings: %s", n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CRM deal sync failed: %s", exc)


def _auto_messages_job():
    try:
        from . import crm_ext
        crm_ext.run_auto_rules()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auto messages failed: %s", exc)


def _reminder_job():
    try:
        reminders.check_due_tasks()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reminder check failed: %s", exc)
    try:
        from . import crm
        crm.auto_tasks()
        crm.auto_checklists()
    except Exception as exc:  # noqa: BLE001
        logger.warning("CRM auto tasks failed: %s", exc)


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
    _scheduler.add_job(
        _auto_messages_job,
        "interval",
        minutes=5,
        id="auto_messages",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        watchdog.tick,
        "interval",
        minutes=1,
        id="watchdog",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started (sync %s min, reminders 10 min, summary 22:00, watchdog 1 min)",
                config.SYNC_INTERVAL_MINUTES)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
