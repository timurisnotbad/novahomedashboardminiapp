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


def _morning_report():
    """Setting report_time (default 07:00): today's arrivals with the times the
    guests told us → the work chats. Checked every minute, sent once a day."""
    try:
        from . import crm
        if crm.get_setting("report_auto", "1") != "1":
            return
        now = date.today().isoformat()
        hhmm = crm._hm(crm.get_setting("report_time", "09:00"), "09:00")  # noqa: SLF001
        from datetime import datetime as _dt
        if _dt.now().strftime("%H:%M") < hhmm:
            return
        if crm.get_setting("report_last_sent", "").startswith(now):
            return
        crm.send_arrivals_report(now, "")
        logger.info("Morning arrivals report sent")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Morning report failed: %s", exc)


def _job():
    try:
        count = rc_sync.sync_to_db()
        logger.info("Sync OK: %s bookings", count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sync failed: %s", exc)
        return
    try:
        from . import rc_rates
        rc_rates.sync()  # nightly prices for the assistant and the CRM (hourly; cheap otherwise)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rates sync failed: %s", exc)
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
        _prune_auto_log()
    except Exception as exc:  # noqa: BLE001
        logger.warning("CRM deal sync failed: %s", exc)


def _prune_auto_log() -> None:
    """The auto-message log is read on every run for its dedupe keys; rows
    older than 90 days can never match a booking in the ±15-day window."""
    from datetime import datetime as _dt, timedelta as _td
    from . import database
    try:
        with database.get_conn() as conn:
            conn.execute("DELETE FROM crm_auto_log WHERE at < ?", ((_dt.now() - _td(days=90)).isoformat(timespec="seconds"),))
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto log prune failed: %s", exc)


def _cash_retry():
    try:
        from . import cash
        n = cash.retry_pending()
        if n:
            logger.info("cash: %s payments written to the sheet on retry", n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cash retry failed: %s", exc)


def _ai_followups():
    try:
        from . import ai
        n = ai.followups()
        if n:
            logger.info("AI follow-ups sent: %s", n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI follow-ups failed: %s", exc)


def _housekeeping():
    """Once a day: rows nobody will read again (reply map of Telegram alerts,
    «seen» previews of the Booking.com extension, finished extension outbox,
    stale access attempts) — the DB stops growing without bound."""
    from datetime import datetime as _dt, timedelta as _td
    from . import database
    old30 = (_dt.now() - _td(days=30)).isoformat(timespec="seconds")
    old180 = (_dt.now() - _td(days=180)).isoformat(timespec="seconds")
    stmts = [
        ("DELETE FROM inbox_tg_map WHERE created_at < ?", (old30,)),
        ("DELETE FROM inbox_ext_seen WHERE at < ? AND preview != '*full*'", (old180,)),
        ("DELETE FROM inbox_ext_outbox WHERE status IN ('done', 'failed') AND created_at < ?", (old30,)),
    ]
    with database.get_conn() as conn:
        for sql, args in stmts:
            try:
                conn.execute(sql, args)
            except Exception as exc:  # noqa: BLE001 — a table that this install never created
                logger.debug("housekeeping skipped: %s (%s)", sql[:40], exc)
    _prune_auto_log()
    logger.info("Housekeeping done")


def _notify_job():
    try:
        from . import inbox
        inbox.notify_pending()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Inbox notifications failed: %s", exc)


def _auto_messages_job():
    try:
        from . import crm_ext
        crm_ext.run_auto_rules()
        crm_ext.run_task_messages()
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
    _scheduler.add_job(_morning_report, "interval", minutes=1, id="morning_report", max_instances=1, coalesce=True)
    _scheduler.add_job(
        _notify_job,
        "interval",
        minutes=1,
        id="inbox_notify",
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
    _scheduler.add_job(_housekeeping, "cron", hour=4, minute=17, id="housekeeping", max_instances=1, coalesce=True)
    _scheduler.add_job(_ai_followups, "interval", minutes=10, id="ai_followups", max_instances=1, coalesce=True)
    _scheduler.add_job(_cash_retry, "interval", minutes=10, id="cash_retry", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("Scheduler started (sync %s min, reminders 10 min, summary 22:00, watchdog 1 min)",
                config.SYNC_INTERVAL_MINUTES)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
