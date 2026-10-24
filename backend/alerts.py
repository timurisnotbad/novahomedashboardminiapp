"""Personal Telegram notifications for CRM users — the «app notifications»
until there is a native app. A user links the work bot once (/link CODE from
CRM → «Telegram-уведомления») and chooses which kinds to receive; every
notification the app raises (web push, the owner's chats) is also sent to
the people who asked for that kind."""
import json
import logging
from datetime import datetime, timedelta

from . import config, database

logger = logging.getLogger(__name__)

KINDS = [
    ("messages", "Сообщения гостей", "сообщение ждёт ответа дольше заданных минут, эскалация без ответа", False),
    ("tasks", "Задачи", "мои задачи с наступающим сроком и просроченные", False),
    ("bookings", "Брони", "новая, изменённая, отменённая бронь; изменение времени приезда", False),
    ("ai", "Ассистент", "передал чат человеку, завёл заявку", False),
    ("cash", "Оплаты", "каждая записанная, исправленная, отменённая оплата", True),
    ("system", "Система", "сбои каналов, мост WhatsApp, сессия Booking", True),
]
ADMIN_ONLY = {k for k, _, _, adm in KINDS if adm}


def prefs(uid: int) -> dict:
    from . import crm
    out = {k: True for k, *_ in KINDS}
    raw = crm.get_setting(f"tg_prefs:{uid}", "")
    if raw:
        try:
            out.update({k: bool(v) for k, v in json.loads(raw).items() if k in out})
        except ValueError:
            pass
    return out


def set_prefs(uid: int, data: dict) -> dict:
    from . import crm
    cur = prefs(uid)
    cur.update({k: bool(data[k]) for k in cur if k in data})
    crm.set_setting(f"tg_prefs:{uid}", json.dumps(cur))
    return cur


def targets(kind: str, uids=None) -> list[int]:
    """Telegram ids of the linked, active users who want this kind (admins only for admin kinds)."""
    from . import crm
    out = []
    for u in crm.list_users():
        if not u["active"] or not u.get("tg_id"):
            continue
        if uids is not None and u["id"] not in uids:
            continue
        if kind in ADMIN_ONLY and u["role"] != "admin":
            continue
        if not prefs(u["id"]).get(kind, True):
            continue
        out.append(int(u["tg_id"]))
    return out


def broadcast(kind: str, text: str, base=None, uids=None, topic: str | None = None) -> None:
    """Send to the usual targets (base: the owner's chats by default) plus the users who chose this kind."""
    from . import notify
    base_list = list(base) if base is not None else list(config.notify_targets())
    ids = list(dict.fromkeys(base_list + targets(kind, uids)))
    if ids:
        notify.send(text, targets=ids, topic=topic)


def task_reminders() -> int:
    """Every 5 minutes: a CRM task due within 30 minutes (or overdue) → its assignee
    (everyone with «tasks» on when unassigned), once per task."""
    from . import crm
    now = datetime.now()
    soon = (now + timedelta(minutes=30)).isoformat(timespec="minutes")
    floor = (now - timedelta(hours=12)).isoformat(timespec="minutes")
    n = 0
    with database.get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS crm_task_alerts (task_id INTEGER PRIMARY KEY, at TEXT)")
        rows = [dict(r) for r in conn.execute(
            "SELECT t.* FROM crm_tasks t LEFT JOIN crm_task_alerts a ON a.task_id = t.id "
            "WHERE t.status = 'open' AND t.due IS NOT NULL AND t.due <= ? AND t.due >= ? AND a.task_id IS NULL "
            "AND (t.auto_text IS NULL OR t.auto_text = '') ORDER BY t.due LIMIT 30", (soon, floor)).fetchall()]
    link = f"{config.WEBAPP_URL.rstrip('/')}/crm/#tasks" if config.WEBAPP_URL else ""
    for t in rows:
        try:
            due = datetime.fromisoformat(t["due"])
            late = due <= now
            head = "⚠️ Просрочена задача" if late else f"⏰ Через {max(1, int((due - now).total_seconds() // 60))} мин"
            text = f"{head}: {t['title']}\nСрок: {due.strftime('%d.%m %H:%M')}" + (f"\n🔗 {link}" if link else "")
            ids = targets("tasks", [t["assignee_uid"]] if t.get("assignee_uid") else None)
            if ids:
                from . import notify
                notify.send(text, targets=ids)
            with database.get_conn() as conn:
                conn.execute("INSERT OR REPLACE INTO crm_task_alerts (task_id, at) VALUES (?, ?)", (t["id"], now.isoformat(timespec="seconds")))
            n += bool(ids)
        except Exception:  # noqa: BLE001
            logger.exception("task alert %s failed", t.get("id"))
    return n


def status(uid: int) -> dict:
    from . import crm
    u = crm.get_user(uid) or {}
    return {"linked": bool(u.get("tg_id")), "bot": getattr(config, "BOT_USERNAME", "") or "", "configured": bool(config.BOT_TOKEN),
            "prefs": prefs(uid), "kinds": [{"key": k, "name": n, "hint": h, "admin": a} for k, n, h, a in KINDS], "admin": u.get("role") == "admin"}
