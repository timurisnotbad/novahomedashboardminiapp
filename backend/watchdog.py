"""Outage alerts (runs inside the server every minute).

1. Local watch: the bot writes logs/bot-heartbeat.txt every minute. When the
   mark gets older than BOT_DOWN_AFTER_MIN the owners get a Telegram message
   ("бот не отвечает с 03:12"), and another one when it is back. Sending goes
   straight to the Bot API over HTTPS, so it works while the bot process is
   dead — only the server and the internet must be alive.
2. Outside watch: ping HEARTBEAT_URL_SERVER (healthchecks.io). When the PC is
   off or offline the pings stop and healthchecks.io alerts you instead.
"""
import logging
from datetime import datetime
from pathlib import Path

import requests

from . import config, logsetup, notify

logger = logging.getLogger("nova.watchdog")

HEARTBEAT_PATH: Path = logsetup.LOG_DIR / "bot-heartbeat.txt"

_state = {"bot_down_since": None, "alerted": False, "ping_fail_logged": False}


def read_heartbeat():
    """(timestamp, polling_ok) from the bot's mark, or (None, None)."""
    try:
        txt = HEARTBEAT_PATH.read_text(encoding="utf-8").strip()
        ts = datetime.fromisoformat(txt.split()[0])  # "2026-09-19T03:19:30 polling=ok"
        return ts, "polling=ok" in txt
    except Exception:  # noqa: BLE001
        return None, None


def ping(url: str) -> bool:
    if not url:
        return False
    try:
        requests.get(url, timeout=10)
        _state["ping_fail_logged"] = False
        return True
    except requests.RequestException as exc:
        if not _state["ping_fail_logged"]:
            logger.warning("heartbeat ping failed: %s", exc)
            _state["ping_fail_logged"] = True
        return False


def check_bot(now: datetime | None = None) -> str | None:
    """Return the alert text sent this round (for tests), else None."""
    now = now or datetime.now()
    ts, _ok = read_heartbeat()
    if ts is None:
        return None  # the bot never ran on this version yet — nothing to compare
    age_min = (now - ts).total_seconds() / 60
    owners = config.OWNER_TELEGRAM_IDS or None
    if age_min >= config.BOT_DOWN_AFTER_MIN:
        if not _state["alerted"]:
            _state["alerted"] = True
            _state["bot_down_since"] = ts
            text = (f"🔴 Бот не отвечает с {ts:%H:%M} ({int(age_min)} мин). Сервер и компьютер работают.\n"
                    f"Что делать: на компьютере запустить restart_all.bat. "
                    f"Если окно «Nova Bot» открыто — посмотреть, что в нём написано.")
            notify.send(text, targets=owners)
            logger.warning("bot heartbeat is %d min old — owners alerted", age_min)
            return text
        return None
    if _state["alerted"]:
        _state["alerted"] = False
        since = _state["bot_down_since"]
        gap = f" (не работал с {since:%H:%M})" if since else ""
        text = f"🟢 Бот снова в сети{gap}."
        notify.send(text, targets=owners)
        logger.info("bot heartbeat back — owners told")
        return text
    return None


def tick() -> None:
    """Scheduler entry point: every minute."""
    try:
        check_bot()
    except Exception:  # noqa: BLE001
        logger.exception("bot watch failed")
    if config.HEARTBEAT_URL_SERVER:
        ping(config.HEARTBEAT_URL_SERVER)
