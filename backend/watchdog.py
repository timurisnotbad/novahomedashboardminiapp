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

_state = {"bot_down_since": None, "alerted": False, "ping_fail_logged": False,
          "revived_at": None, "revives": 0, "second_alert": False}
_STARTED = datetime.now()
START_GRACE_MIN = 4      # after the server starts the bot needs a moment to come up
REVIVE_EVERY_MIN = 10    # one restart attempt per this many minutes while the bot stays silent
BASE_DIR: Path = config.BASE_DIR


def read_heartbeat():
    """(timestamp, state) from the bot's mark — state is ok / NO_NETWORK /
    CONFLICT / STALLED / DEAD — or (None, None) when there is no mark."""
    try:
        txt = HEARTBEAT_PATH.read_text(encoding="utf-8").strip()
        ts = datetime.fromisoformat(txt.split()[0])  # "2026-09-19T03:19:30 polling=ok last_poll=4s"
        state = "ok"
        for part in txt.split()[1:]:
            if part.startswith("polling="):
                state = part.split("=", 1)[1]
        return ts, state
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


def _ps(cmd: str, timeout: int = 25) -> str:
    import os
    import subprocess
    if os.name != "nt":
        return ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("powershell failed: %s", exc)
        return ""


def log_tail(limit: int = 500) -> str:
    """Why the bot is down, in its own words: the newest crash from
    logs/bot-error.log, else the last error lines of logs/bot.log."""
    from datetime import timedelta
    out = ""
    try:
        p = logsetup.LOG_DIR / "bot-error.log"
        if p.exists() and datetime.fromtimestamp(p.stat().st_mtime) > datetime.now() - timedelta(hours=24):
            lines = [x.rstrip() for x in p.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
            out = "\n".join(lines[-4:])
        if not out:
            p = logsetup.LOG_DIR / "bot.log"
            if p.exists():
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
                bad = [x.rstrip() for x in lines if " ERROR " in x or " CRITICAL " in x or "Traceback" in x or "Error:" in x]
                out = "\n".join(bad[-4:])
    except Exception as exc:  # noqa: BLE001
        out = f"(лог не прочитался: {exc})"
    return config.redact(out)[-limit:]


def revive_bot() -> str:
    """Windows: stop the stuck bot process and make sure a «Nova Bot» window
    is running it again. Returns what was done, in Russian, for the alert."""
    import os
    import subprocess
    if os.name != "nt":
        return _revive_posix()
    out = _ps(
        "$b = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bot.py*' -and $_.Name -like 'python*' }); "
        "$w = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*start_bot.bat*' -and $_.Name -eq 'cmd.exe' }); "
        "$b | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        # a window with no python inside has stopped its loop (config error, closed by hand): replace it
        "if ($b.Count -eq 0) { $w | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }; "
        "Write-Output ('bots=' + $b.Count + ' windows=' + $w.Count)"
    )
    bots = windows = -1
    for part in out.split():
        if part.startswith("bots="):
            bots = int(part[5:] or 0)
        if part.startswith("windows="):
            windows = int(part[8:] or 0)
    if windows > 0 and bots > 0:
        # the window's loop starts python again 15 s after it exits
        return f"остановил зависший процесс бота ({max(bots, 0)} шт.), окно «Nova Bot» запустит его заново через 15 секунд"
    try:
        bat = BASE_DIR / "start_bot.bat"
        subprocess.Popen(f'start "Nova Bot" cmd /k "{bat}"', shell=True, cwd=str(BASE_DIR))
        return "окна «Nova Bot» не было — открыл его заново" + (f" (старых процессов снято: {bots})" if bots > 0 else "")
    except Exception as exc:  # noqa: BLE001
        return f"не смог открыть окно бота: {exc}"


def _revive_posix() -> str:
    """Linux server (cloud): the entrypoint keeps the bot in a restart loop, so
    killing the stuck process is all it takes."""
    import os
    import signal
    killed = 0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if "bot.py" in cmd and "python" in cmd:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
    except OSError as exc:
        return f"не смог найти процесс бота: {exc}"
    if killed:
        return f"остановил зависший процесс бота ({killed} шт.), он запустится заново через 15 секунд"
    return "процесса бота не было — он запускается сам, раз в 15 секунд (если не стартует, причина в логе ниже)"


def check_update_result() -> str | None:
    """Cloud: «/update» from the bot → the host rebuilds and leaves the outcome
    in data/update.result; tell the owners once."""
    p = BASE_DIR / "data" / "update.result"
    try:
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        p.unlink()
    except OSError:
        return None
    from . import alerts
    if text.startswith("ok"):
        msg = f"✅ Сервер обновлён до версии {text[2:].strip() or config.APP_VERSION}."
    elif text.startswith("same"):
        msg = f"👌 Обновлять нечего: уже стоит последняя версия ({text[4:].strip() or config.APP_VERSION})."
    else:
        lines = [x for x in text.splitlines()[1:] if x.strip()]
        note = lines[0] if lines else "причина в логе"
        msg = f"❌ Обновление не установлено: {config.redact(note)}."
        if len(lines) > 1:
            msg += "\n\nКонец лога:\n" + config.redact("\n".join(lines[1:]))[-600:]
    alerts.broadcast("system", msg, base=config.OWNER_TELEGRAM_IDS or None)
    return msg


_STATE_TEXT = {
    "NO_NETWORK": "бот не может достучаться до Telegram (нет интернета или Telegram заблокирован у провайдера)",
    "CONFLICT": "токен бота опрашивает ещё одна программа — запущена вторая копия (второе окно «Nova Bot» или старый компьютер, "
                "с которого переносили проект). Выключите вторую копию: пока она работает, сообщения теряются",
    "STALLED": "бот перестал получать сообщения от Telegram, хотя интернет есть",
    "DEAD": "опрос Telegram остановился",
}


def check_bot(now: datetime | None = None) -> str | None:
    """Every minute. The bot is «down» when its mark is old (process dead or
    frozen), missing (never started), or says it is not receiving. The server
    then restarts it by itself and tells the owners what happened and why.
    Returns the alert text sent this round (for tests), else None."""
    from . import alerts
    now = now or datetime.now()
    owners = config.OWNER_TELEGRAM_IDS or None
    if (now - _STARTED).total_seconds() < START_GRACE_MIN * 60:
        return None
    ts, state = read_heartbeat()
    if ts is None:
        ts, state = _STARTED, "MISSING"  # the bot has not written a single mark since the server started
    age_min = (now - ts).total_seconds() / 60
    silent = age_min >= config.BOT_DOWN_AFTER_MIN
    deaf = not silent and state not in ("ok", None)

    if not silent and not deaf:
        if _state["alerted"]:
            since = _state["bot_down_since"]
            _state.update(alerted=False, revived_at=None, revives=0, second_alert=False)
            text = "🟢 Бот снова в сети" + (f" (не работал с {since:%H:%M})." if since else ".")
            alerts.broadcast("system", text, base=owners)
            logger.info("bot is back — owners told")
            return text
        return None

    if deaf and state in ("NO_NETWORK", "CONFLICT"):
        # restarting does not cure these; say it once
        if _state["alerted"]:
            return None
        _state.update(alerted=True, bot_down_since=now)
        text = f"🔴 Бот не принимает сообщения: {_STATE_TEXT[state]}."
        alerts.broadcast("system", text, base=owners)
        logger.warning("bot deaf (%s) — owners alerted", state)
        return text

    # dead / frozen / never started / stalled: restart it, at most once per REVIVE_EVERY_MIN
    last = _state["revived_at"]
    if last is not None and (now - last).total_seconds() < REVIVE_EVERY_MIN * 60:
        return None
    first = not _state["alerted"]
    if first:
        _state.update(alerted=True, bot_down_since=ts)
    _state["revived_at"] = now
    _state["revives"] += 1
    done = revive_bot()
    logger.warning("bot down (%s, mark %d min old) — revive #%d: %s", state, age_min, _state["revives"], done)
    if first:
        why = _STATE_TEXT.get(state, "процесс бота не работает" if state != "MISSING" else "бот не запускался после старта сервера")
        text = (f"🔴 Бот не отвечает с {ts:%H:%M} ({int(age_min)} мин): {why}.\n"
                f"Что сделал сервер: {done}.")
        tail = log_tail()
        if tail:
            text += f"\n\nПоследнее из лога бота:\n{tail}"
        alerts.broadcast("system", text, base=owners)
        return text
    if _state["revives"] == 3 and not _state["second_alert"]:
        _state["second_alert"] = True
        text = ("⚠️ Бот не поднялся после трёх перезапусков. Нужен человек: на компьютере откройте окно «Nova Bot» "
                "и пришлите, что в нём написано, или запустите restart_all.bat.")
        tail = log_tail()
        if tail:
            text += f"\n\nПоследнее из лога бота:\n{tail}"
        alerts.broadcast("system", text, base=owners)
        return text
    return None


def keep_awake() -> list[str]:
    """The office laptop IS the server: it must not fall asleep, and closing
    the lid must not suspend it. Applied at every server start (Windows only,
    KEEP_AWAKE=0 in .env turns it off). Returns what was done, for the log."""
    import os
    done: list[str] = []
    if os.name != "nt" or os.environ.get("KEEP_AWAKE", "1").strip() in ("0", "false", "no"):
        return done
    import subprocess
    cmds = [
        ["powercfg", "/change", "standby-timeout-ac", "0"], ["powercfg", "/change", "standby-timeout-dc", "0"],
        ["powercfg", "/change", "hibernate-timeout-ac", "0"], ["powercfg", "/change", "hibernate-timeout-dc", "0"],
        # closing the lid: do nothing (on mains and on battery)
        ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0"],
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0"],
        ["powercfg", "/setactive", "SCHEME_CURRENT"],
    ]
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, timeout=15)
            if r.returncode == 0:
                done.append(" ".join(c[1:]))
            else:
                logger.warning("powercfg %s -> %s", " ".join(c[1:]), r.returncode)
        except Exception as exc:  # noqa: BLE001
            logger.warning("powercfg failed: %s", exc)
    logger.info("keep-awake applied: %d of %d settings", len(done), len(cmds))
    return done


def poke_awake() -> None:
    """Reset Windows' idle-sleep timer (called every minute): while the server
    runs the computer does not doze off on its own, whatever the power plan says."""
    import os
    if os.name != "nt" or os.environ.get("KEEP_AWAKE", "1").strip() in ("0", "false", "no"):
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x00000001)  # ES_SYSTEM_REQUIRED
    except Exception:  # noqa: BLE001
        pass


def tick() -> None:
    """Scheduler entry point: every minute."""
    poke_awake()
    try:
        from . import uptime
        uptime.beat()  # the black box: a gap in these marks = the server or the computer was off
    except Exception:  # noqa: BLE001
        logger.exception("uptime beat failed")
    try:
        check_bot()
    except Exception:  # noqa: BLE001
        logger.exception("bot watch failed")
    try:
        check_update_result()
    except Exception:  # noqa: BLE001
        logger.exception("update result check failed")
    try:
        check_sync()
    except Exception:  # noqa: BLE001
        logger.exception("sync watch failed")
    if config.HEARTBEAT_URL_SERVER:
        ping(config.HEARTBEAT_URL_SERVER)


_sync_state = {"alerted": False}
SYNC_ALERT_AFTER_MIN = 60


def check_sync(now: datetime | None = None) -> str | None:
    """RealtyCalendar sync failing for an hour → tell the owners once (with
    the reason), and again when it recovers. Without this the bot keeps
    posting yesterday's calendar as if nothing happened."""
    from . import services
    s = services.sync_staleness()
    if s["minutes"] is None:
        return None  # never synced yet (fresh install) — nothing to compare
    owners = config.OWNER_TELEGRAM_IDS or None
    if s["minutes"] >= SYNC_ALERT_AFTER_MIN and s["fails"] > 0:
        if _sync_state["alerted"]:
            return None
        _sync_state["alerted"] = True
        when = services.fmt_local(s["last_ok"])
        text = (f"🔴 Синхронизация с RealtyCalendar не работает с {when}: {s['fails']} попыток подряд "
                f"без успеха.\nОшибка: {s['error'] or '—'}\n\n"
                f"Бот и дашборд показывают брони на момент {when}; новые заезды и выезды не видны.\n"
                f"Если в ошибке 401/403 или «не JSON» — истекли RC_TOKEN или cookies в .env: "
                f"войдите в RealtyCalendar в браузере, скопируйте новые значения, затем restart_all.bat.")
        notify.send(text, targets=owners)
        logger.warning("RC sync stale for %s min — owners alerted", s["minutes"])
        return text
    if s["minutes"] < SYNC_ALERT_AFTER_MIN and _sync_state["alerted"]:
        _sync_state["alerted"] = False
        text = "🟢 Синхронизация с RealtyCalendar восстановлена, брони снова актуальные."
        notify.send(text, targets=owners)
        return text
    return None
