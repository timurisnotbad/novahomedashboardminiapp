"""Manager analytics: who answers guests, how fast, when — from the inbox messages.

A «response» is the first outgoing message after one or more guest messages in a chat;
its time is counted from the first unanswered guest message of that burst. Automatic
authors (assistant, auto-replies) are excluded from the people table but shown in totals."""
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import database

SYSTEM_AUTHORS = {"ИИ-ассистент", "Автоответ"}
PHONE_AUTHOR = "Телефон"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:19])


def _pct(part: int, total: int) -> int:
    return round(part * 100 / total) if total else 0


def manager_stats(days: int = 30, start: str | None = None, end: str | None = None) -> dict:
    today = date.today()
    d1 = date.fromisoformat(end) if end else today
    d0 = date.fromisoformat(start) if start else d1 - timedelta(days=max(1, days) - 1)
    lo, hi = d0.isoformat() + "T00:00:00", d1.isoformat() + "T23:59:59"
    with database.get_conn() as conn:
        rows = conn.execute("SELECT chat_id, direction, author, at, kind FROM inbox_messages WHERE at >= ? AND at <= ? AND (deleted_at IS NULL) "
                            "AND kind != 'call' ORDER BY chat_id, at, id", (lo, hi)).fetchall()
        unanswered_now = conn.execute("SELECT COUNT(*) FROM inbox_chats WHERE unread > 0 AND last_dir = 'in'").fetchone()[0]
        names = {r["name"] for r in conn.execute("SELECT name FROM crm_users WHERE active = 1").fetchall()}
    per: dict[str, dict] = {}
    def bucket(name):
        if name not in per:
            per[name] = {"name": name, "replies": 0, "chats": set(), "responses": [], "hours": [0] * 24, "days": set(),
                         "first_by_day": {}, "last_by_day": {}, "is_user": name in names, "system": name in SYSTEM_AUTHORS}
        return per[name]
    guest_msgs = 0
    bursts_total = 0
    bursts_answered = 0
    all_responses = []
    open_since: dict[int, datetime] = {}  # chat → first unanswered guest message of the current burst
    last_chat = None
    for r in rows:
        if r["chat_id"] != last_chat:
            last_chat = r["chat_id"]
        t = _parse(r["at"])
        if r["direction"] == "in":
            guest_msgs += 1
            if r["chat_id"] not in open_since:
                open_since[r["chat_id"]] = t
                bursts_total += 1
            continue
        author = (r["author"] or "").strip() or "—"
        b = bucket(author)
        b["replies"] += 1
        b["chats"].add(r["chat_id"])
        b["hours"][t.hour] += 1
        dkey = t.date().isoformat()
        b["days"].add(dkey)
        b["first_by_day"][dkey] = min(b["first_by_day"].get(dkey, t), t)
        b["last_by_day"][dkey] = max(b["last_by_day"].get(dkey, t), t)
        if r["chat_id"] in open_since:
            mins = (t - open_since.pop(r["chat_id"])).total_seconds() / 60
            if mins >= 0:
                b["responses"].append(mins)
                all_responses.append(mins)
                bursts_answered += 1
    def summarize(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0, "median": None, "avg": None, "p90": None, "within5": 0, "within15": 0, "within60": 0, "over60": 0}
        s = sorted(vals)
        return {"n": len(s), "median": round(statistics.median(s), 1), "avg": round(sum(s) / len(s), 1), "p90": round(s[min(len(s) - 1, int(len(s) * 0.9))], 1),
                "within5": _pct(sum(1 for v in s if v <= 5), len(s)), "within15": _pct(sum(1 for v in s if v <= 15), len(s)),
                "within60": _pct(sum(1 for v in s if v <= 60), len(s)), "over60": sum(1 for v in s if v > 60)}
    def avg_time(d: dict) -> str | None:
        if not d:
            return None
        mins = [v.hour * 60 + v.minute for v in d.values()]
        m = int(sum(mins) / len(mins))
        return f"{m // 60:02d}:{m % 60:02d}"
    managers = []
    for b in per.values():
        st = summarize(b["responses"])
        managers.append({"name": b["name"], "label": "С телефона" if b["name"] == PHONE_AUTHOR else b["name"], "system": b["system"], "is_user": b["is_user"],
                         "replies": b["replies"], "chats": len(b["chats"]), "days_active": len(b["days"]), "per_day": round(b["replies"] / max(1, len(b["days"])), 1),
                         "first_reply_avg": avg_time(b["first_by_day"]), "last_reply_avg": avg_time(b["last_by_day"]),
                         "hours": b["hours"], **st})
    managers.sort(key=lambda m: (m["system"], -m["replies"]))
    people = [m for m in managers if not m["system"]]
    total_replies = sum(m["replies"] for m in managers) or 1
    for m in managers:
        m["share"] = _pct(m["replies"], total_replies)
    return {"from": d0.isoformat(), "to": d1.isoformat(), "days": (d1 - d0).days + 1,
            "totals": {"guest_messages": guest_msgs, "replies": sum(m["replies"] for m in managers), "people_replies": sum(m["replies"] for m in people),
                       "bursts": bursts_total, "answered": bursts_answered, "answered_pct": _pct(bursts_answered, bursts_total),
                       "unanswered_now": unanswered_now, **summarize(all_responses)},
            "managers": managers}
