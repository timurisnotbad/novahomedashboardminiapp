"""Why was the server down? A black box for the office computer.

The server writes logs/server-heartbeat.txt every minute. At start it compares
the last mark with the clock: a gap means the server (or the whole computer)
was off. The gap is stored in server_outages together with what the Windows
System event log says about that time — blue screen (BugCheck 1001), power
loss / hard hang (Kernel-Power 41, EventLog 6008), a planned shutdown or
restart and who asked for it (User32 1074: Windows Update, a user), sleep
(Kernel-Power 42) — and the owners get one Telegram message with the reason.
Short gaps are plain restarts of the server process; the last crash from
logs/server-error.log is attached to them."""
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

from . import database, logsetup

logger = logging.getLogger("nova.uptime")

HEARTBEAT = logsetup.LOG_DIR / "server-heartbeat.txt"
OUTAGE_MIN = 3          # a longer gap = the computer was off / asleep / offline, not a restart
EVENT_IDS = (41, 42, 107, 1001, 1074, 6006, 6008)
_started_at = datetime.now()


def init_db() -> None:
    with database.get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS server_outages (id INTEGER PRIMARY KEY AUTOINCREMENT, down_from TEXT, up_at TEXT, "
                     "minutes INTEGER, kind TEXT, reason TEXT, details TEXT)")


def beat(now: datetime | None = None) -> None:
    try:
        logsetup.LOG_DIR.mkdir(exist_ok=True)
        HEARTBEAT.write_text((now or datetime.now()).isoformat(timespec="seconds"), encoding="utf-8")
    except OSError as exc:
        logger.warning("heartbeat write failed: %s", exc)


def last_beat() -> datetime | None:
    try:
        return datetime.fromisoformat(HEARTBEAT.read_text(encoding="utf-8").strip()[:19])
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Windows System event log
# ---------------------------------------------------------------------------
def parse_events(text: str) -> list[dict]:
    """`wevtutil qe System /f:text` output → [{id, date, source, text}] (labels are English, descriptions localized)."""
    out = []
    for block in re.split(r"(?m)^Event\[\d+\]:?\s*$", text or ""):
        m = re.search(r"Event ID:\s*(\d+)", block)
        if not m:
            continue
        d = re.search(r"Date:\s*(\S+)", block)
        s = re.search(r"Source:\s*(.+)", block)
        desc = block.split("Description:", 1)[1].strip() if "Description:" in block else ""
        out.append({"id": int(m.group(1)), "date": (d.group(1) if d else "")[:19], "source": (s.group(1).strip() if s else ""), "text": re.sub(r"\s+", " ", desc)[:400]})
    return out


def explain(events: list[dict]) -> tuple[str, str]:
    """(kind, human reason) from the events around the gap; the most telling one wins."""
    by = {}
    for e in events:
        by.setdefault(e["id"], e)
    if 1001 in by:  # in the System log 1001 is the bug check report
        code = re.search(r"0x[0-9a-fA-F]{8}", by[1001]["text"])
        return "bsod", f"синий экран Windows (BSOD){', код ' + code.group(0) if code else ''} — сбой драйвера, памяти или перегрев"
    if 1074 in by:
        t = by[1074]["text"]
        who = "Центр обновления Windows" if re.search(r"update|обновлен|TrustedInstaller|MoUsoCoreWorker|wuauclt", t, re.I) else "пользователь или программа"
        what = "перезагрузка" if re.search(r"restart|перезагруз", t, re.I) else "выключение"
        return "planned", f"плановое {what}: {who}. {t[:200]}"
    if 41 in by or 6008 in by:
        return "power", "компьютер выключился аварийно, без завершения работы: пропало питание или сел аккумулятор, зависание, перегрев либо долгое нажатие кнопки питания"
    if 42 in by or 107 in by:
        return "sleep", "компьютер уходил в сон (закрыта крышка, питание от батареи или настройки электропитания)"
    if 6006 in by:
        return "planned", "обычное завершение работы Windows"
    return "unknown", ""


def windows_events(since: datetime, until: datetime | None = None) -> list[dict]:
    if os.name != "nt":
        return []
    lo = (since - timedelta(minutes=10)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ids = " or ".join(f"EventID={i}" for i in EVENT_IDS)
    q = f"*[System[({ids}) and TimeCreated[@SystemTime>='{lo}']]]"
    try:
        r = subprocess.run(["wevtutil", "qe", "System", f"/q:{q}", "/f:text", "/c:40", "/rd:true"], capture_output=True, timeout=25)
        text = r.stdout.decode("utf-8", "replace") if r.stdout else ""
        if "Event ID" not in text and r.stdout:
            text = r.stdout.decode("cp866", "replace")
        return parse_events(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("event log query failed: %s", exc)
        return []


def _last_crash(since: datetime) -> str:
    """The newest traceback tail from logs/server-error.log written around the stop."""
    p = logsetup.LOG_DIR / "server-error.log"
    try:
        if not p.exists() or datetime.fromtimestamp(p.stat().st_mtime) < since - timedelta(minutes=5):
            return ""
        lines = [x for x in p.read_text(encoding="utf-8", errors="replace").strip().splitlines() if x.strip()]
        return lines[-1][:300] if lines else ""
    except OSError:
        return ""


def _human(minutes: int) -> str:
    return f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч {minutes % 60:02d} мин" if minutes < 2880 else f"{minutes // 1440} дн {minutes % 1440 // 60} ч"


def on_start(now: datetime | None = None, notify: bool = True) -> dict | None:
    """Called once when the server starts: record the gap since the last heartbeat (if any) and tell the owners."""
    now = now or datetime.now()
    last = last_beat()
    beat(now)
    if not last or now <= last:
        return None
    minutes = int((now - last).total_seconds() // 60)
    if minutes < OUTAGE_MIN:
        crash = _last_crash(last)
        if not crash:
            return None  # an ordinary restart (update, restart_all.bat)
        row = {"down_from": last.isoformat(timespec="seconds"), "up_at": now.isoformat(timespec="seconds"), "minutes": minutes, "kind": "crash",
               "reason": "сервер упал с ошибкой и перезапустился сам", "details": crash}
    else:
        events = windows_events(last, now)
        kind, reason = explain(events)
        if kind == "unknown":
            reason = "в журнале Windows причины нет: компьютер был выключен, спал или окно сервера было закрыто" if os.name == "nt" else "сервер был остановлен"
        row = {"down_from": last.isoformat(timespec="seconds"), "up_at": now.isoformat(timespec="seconds"), "minutes": minutes, "kind": kind, "reason": reason,
               "details": " | ".join(f"{e['date']} {e['source']} {e['id']}: {e['text'][:160]}" for e in events[:6])}
    with database.get_conn() as conn:
        row["id"] = conn.execute("INSERT INTO server_outages (down_from, up_at, minutes, kind, reason, details) VALUES (?, ?, ?, ?, ?, ?)",
                                 (row["down_from"], row["up_at"], row["minutes"], row["kind"], row["reason"], row["details"])).lastrowid
    logger.warning("server was down %s → %s (%s): %s", row["down_from"], row["up_at"], row["kind"], row["reason"])
    if notify:
        try:
            from . import alerts
            icon = {"bsod": "💥", "power": "🔌", "planned": "🔄", "sleep": "😴", "crash": "🐞"}.get(row["kind"], "⚠️")
            alerts.broadcast("system", f"{icon} Сервер Nova Home не работал с {last.strftime('%d.%m %H:%M')} до {now.strftime('%d.%m %H:%M')} ({_human(minutes)}).\n"
                                       f"Причина: {row['reason']}." + (f"\n{row['details'][:300]}" if row["kind"] == "crash" else ""))
        except Exception:  # noqa: BLE001
            logger.exception("outage notice failed")
    return row


def status(limit: int = 30) -> dict:
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM server_outages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        month_ago = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        agg = conn.execute("SELECT COUNT(*), COALESCE(SUM(minutes), 0) FROM server_outages WHERE up_at >= ?", (month_ago,)).fetchone()
    up = int((datetime.now() - _started_at).total_seconds() // 60)
    return {"started_at": _started_at.isoformat(timespec="seconds"), "uptime_min": up, "uptime": _human(up), "outages": rows,
            "month": {"count": agg[0], "minutes": agg[1], "human": _human(int(agg[1])), "availability": round(100 - agg[1] * 100 / (30 * 1440), 2)},
            "windows": os.name == "nt"}
