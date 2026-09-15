"""Telegram bot for Nova Home Dashboard.

Provides a button that opens the Mini App, plus text fallbacks (/today, /sync)
and a daily 08:00 summary. Run with: ``python bot.py``.

Requires BOT_TOKEN and WEBAPP_URL in the environment (see .env.example).
"""
import asyncio
import datetime
import logging
import re
import sys
from pathlib import Path

# Run from any working directory (double-click, shortcut, scheduled task):
# without this, "from backend import …" fails when cwd is not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from telegram import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        ReplyKeyboardMarkup,
        Update,
        WebAppInfo,
    )
    from telegram.constants import ParseMode
    from telegram.ext import (Application, ApplicationHandlerStop, CommandHandler, ContextTypes,
                              MessageHandler, filters)

    from backend import (attendance, booking_prices, config, database, issues, logsetup, notify,
                         pay_parse, rc_sync, services, supplies)
except BaseException as _import_exc:  # noqa: BLE001 — a missing library must not close the window
    import traceback

    try:  # write the reason down before anything else: the window may be closed fast
        _dir = Path(__file__).resolve().parent / "logs"
        _dir.mkdir(exist_ok=True)
        with (_dir / "bot-error.log").open("a", encoding="utf-8") as _fh:
            import datetime as _dt
            _fh.write(f"\n===== {_dt.datetime.now().isoformat(timespec='seconds')} "
                      f"ошибка при загрузке библиотек =====\n")
            traceback.print_exception(type(_import_exc), _import_exc, _import_exc.__traceback__, file=_fh)
    except BaseException:  # noqa: BLE001
        pass
    print("\n❌ Бот не смог запуститься: не хватает библиотек или они сломаны.\n")
    print(f"   {type(_import_exc).__name__}: {_import_exc}\n")
    print("   Исправить: запустите install.bat в папке проекта")
    print("   (он же: python -m pip install -r backend\\requirements.txt)\n")
    traceback.print_exc()
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nНажмите Enter, чтобы закрыть окно… ")
    except BaseException:  # noqa: BLE001
        pass
    raise SystemExit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
LOG_PATH = logsetup.setup("bot")  # everything also goes to logs/bot.log
logger = logging.getLogger("nova.bot")

# PTB's JobQueue treats naive times as UTC — schedule in the server's local
# timezone (Tashkent) so 10:00 means 10:00 local, not 15:00.
LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo


def _webapp_url(uid=None) -> str:
    """Owners get the dashboard URL with their access key appended: some
    Telegram clients (notably the native macOS app) pass no initData to Mini
    Apps, so the signature check alone can't unlock owner mode there. The bot
    knows who the owners are, hands the key only to them, and the frontend
    stores it locally."""
    url = config.WEBAPP_URL
    url += ("&" if "?" in url else "?") + "v=" + config.APP_VERSION  # cache-buster per release
    if config.OWNER_KEY and uid and uid in config.OWNER_TELEGRAM_IDS:
        return url + "&okey=" + config.OWNER_KEY
    if config.PAY_KEY and uid:
        # PAY_VIEWERS get their own key so the Оплаты block works on clients
        # that pass no initData (macOS Telegram)
        try:
            from backend import auth as _auth
            if uid in _auth.pay_viewer_ids():
                return url + "&okey=" + config.PAY_KEY
        except Exception:  # noqa: BLE001
            logger.exception("pay viewer url check failed")
    if config.STAFF_KEY and uid:
        # everyone else: the staff key proves the app was opened from the bot
        # (the API rejects bare visits to the domain)
        url += "&okey=" + config.STAFF_KEY
    return url


def _webapp_markup(uid=None) -> InlineKeyboardMarkup | None:
    if not config.WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 Открыть дашборд", web_app=WebAppInfo(url=_webapp_url(uid)))]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg:
        return
    _register(update.effective_user)
    uid = update.effective_user.id if update.effective_user else None
    if chat and chat.type != "private":
        # Telegram allows Web App buttons in private chats only — sending them
        # here fails with Bad Request and the person gets nothing
        me = context.bot.username or "novahomedashboardbot"
        await msg.reply_text(
            f"Дашборд открывается из личного чата: напишите /start боту @{me}."
        )
        return
    markup = _webapp_markup(uid)
    apt_count = len(_apartment_names()) or config.TOTAL_APARTMENTS
    text = f"🏠 *Nova Home Dashboard*\n\nОперационная сводка по {apt_count} апартаментам."
    if uid and uid in config.OWNER_TELEGRAM_IDS and not config.OWNER_KEY:
        text += ("\n\n⚠️ OWNER\\_KEY не задан в .env. Добавьте строку "
                 "`OWNER\\_KEY=любой-длинный-секрет` и перезапустите — иначе на Telegram "
                 "для Mac без подписи откроется режим сотрудника.")
    if markup:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        # Also expose a persistent keyboard button (opens WebApp from chat).
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📊 Дашборд", web_app=WebAppInfo(url=_webapp_url(uid)))]],
            resize_keyboard=True,
        )
        await update.effective_message.reply_text("Меню:", reply_markup=kb)
    else:
        await update.effective_message.reply_text(
            text + "\n\n⚠️ WEBAPP_URL не задан — доступна только текстовая сводка (/today).",
            parse_mode=ParseMode.MARKDOWN,
        )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    summary = await asyncio.to_thread(services.build_text_summary, datetime.date.today())
    await update.effective_message.reply_text(summary)


async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.effective_message.reply_text("🔄 Синхронизация…")
    try:
        count = await asyncio.to_thread(rc_sync.sync_to_db)
        await msg.edit_text(f"✅ Синхронизировано: {count} броней")
    except Exception as exc:  # noqa: BLE001
        await msg.edit_text(f"❌ Ошибка синхронизации: {exc}")


async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report this chat's id — add the bot to your team group and run /chatid to
    get the id for NOTIFY_CHAT_IDS."""
    chat = update.effective_chat
    thread = _thread_of(update.effective_message) if update.effective_message else None
    tname = _topic_name_of(update.effective_message) if thread else ""
    extra = (f"\nID темы: {thread}" + (f" («{tname}»)" if tname else "")) if thread else ""
    await update.effective_message.reply_text(
        f"ID этого чата: {chat.id}{extra}\n"
        f"Впишите его в NOTIFY_CHAT_IDS в .env, чтобы сюда приходили уведомления."
    )


_TOPIC_ROLES = {
    "уборки": "cleaning", "уборка": "cleaning", "cleaning": "cleaning",
    "явка": "attendance", "приход": "attendance", "attendance": "attendance",
    "поломки": "issues", "ремонт": "issues", "issues": "issues",
    "общий": "general", "общее": "general", "general": "general",
}


async def topic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner, inside a forum topic: /topic уборки|явка|общий — bind this topic
    to that kind of bot message."""
    user = update.effective_user
    msg = update.effective_message
    if config.OWNER_TELEGRAM_IDS and (not user or user.id not in config.OWNER_TELEGRAM_IDS):
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await msg.reply_text("Эту команду нужно отправить внутри темы рабочей группы.")
        return
    parts = (msg.text or "").split()
    role = _TOPIC_ROLES.get(parts[1].lower()) if len(parts) > 1 else None
    if not role:
        labels = {"cleaning": "уборки", "attendance": "явка", "issues": "поломки", "general": "общий"}
        lines = []
        try:
            for r in await asyncio.to_thread(database.chat_topics, chat.id):
                where = (f"«{r['name']}»" if r.get("name")
                         else ("General" if not r.get("thread_id") else f"тема #{r['thread_id']}"))
                lines.append(f"  • {labels.get(r['role'], r['role'])} → {where}")
        except Exception:  # noqa: BLE001
            logger.exception("chat_topics failed")
        current = ("Сейчас привязано:\n" + "\n".join(lines)) if lines else (
            "Сейчас ничего не привязано — отчёты в темах бот пропускает, "
            "всё остальное уходит в General.")
        await msg.reply_text(
            current + "\n\n"
            "Использование — внутри нужной темы:\n"
            "/topic уборки — отчёты горничных, контроль 18:00, вечерний план\n"
            "/topic явка — приходы и перекличка\n"
            "/topic поломки — бот читает эту тему и заносит закупки и задачи в «Контроль»\n"
            "/topic общий — всё остальное\n\n"
            "Тема General привязывается так же (отправьте команду в General)."
        )
        return
    thread = _thread_of(msg)  # None in General (also when sent as a reply there)
    name = _topic_name_of(msg)
    await asyncio.to_thread(database.set_topic, chat.id, role, thread, name)
    _forget_topics(chat.id)
    where = f"«{name}»" if name else ("General" if not thread else f"тема #{thread}")
    await msg.reply_text(f"✅ Привязано: {parts[1].lower()} → {where}")


_loc_status: dict[int, str] = {}  # uid -> last attendance status (to report transitions once)


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark staff arrival from a live location inside the work zone."""
    msg = update.effective_message
    if not msg or not msg.location:
        return
    loc = msg.location
    user = update.effective_user
    _register(user)
    is_live = bool(loc.live_period) or update.edited_message is not None

    if not is_live:
        # a plain (static) location: help the owner grab coordinates; nudge staff
        if user and user.id in config.OWNER_TELEGRAM_IDS:
            await msg.reply_text(
                f"📍 Координаты: {loc.latitude:.6f}, {loc.longitude:.6f}\n"
                f"Впишите их в WORK_LAT / WORK_LNG в .env, чтобы задать рабочую зону."
            )
        else:
            await msg.reply_text(
                "Нужна *live*-геолокация: Прикрепить → Геопозиция → «Транслировать». "
                "Обычная не подходит.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    uid = user.id if user else 0
    # Prefer @username for the group mention; fall back to the profile name,
    # then to the name from STAFF_IDS.
    name = None
    if user and user.username:
        name = f"@{user.username}"
    elif user:
        name = user.full_name or user.first_name
    name = name or config.STAFF.get(uid) or (str(uid) if uid else "Сотрудник")
    is_edit = update.edited_message is not None
    logger.info(
        "location: uid=%s edit=%s live_period=%s coords=%.6f,%.6f",
        uid, is_edit, loc.live_period, loc.latitude, loc.longitude,
    )
    try:
        res = await asyncio.to_thread(
            attendance.check_arrival, uid, name, loc.latitude, loc.longitude
        )
    except Exception:  # noqa: BLE001
        logger.exception("check_arrival failed")
        return
    status = res.get("status")
    logger.info(
        "attendance: status=%s distance=%.0fm radius=%sm",
        status, res.get("distance", -1), config.WORK_RADIUS_M,
    )
    prev = _loc_status.get(uid)
    _loc_status[uid] = status
    if status == "recorded":
        if res.get("notify"):
            notify.send(res["notify"], topic="attendance")
        try:
            await msg.reply_text(res["reply"])
        except Exception:  # noqa: BLE001
            pass
    elif not is_edit or (status == "too_late" and prev not in (None, "too_late")):
        # "outside" / "already": answer once (on the initial share) so the person
        # gets feedback, but don't repeat on every live-location tick. The one
        # transition worth announcing: still travelling when the window closed.
        try:
            await msg.reply_text(res["reply"])
        except Exception:  # noqa: BLE001
            pass


_RU_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def _parse_price_dates(text: str):
    """Parse the args of /цены. Supports: (none)→завтра+1 ночь; two ISO dates;
    'D-D'→текущий месяц; 'D-D мес'→указанный месяц."""
    today = datetime.date.today()
    parts = (text or "").split()[1:]  # drop the command itself
    isos = [p for p in parts if len(p) == 10 and p[4] == "-" and p[7] == "-"]
    if len(isos) >= 2:
        try:
            ci = datetime.date.fromisoformat(isos[0])
            co = datetime.date.fromisoformat(isos[1])
            if co > ci:
                return ci, co
        except ValueError:
            pass
    rng = next((p for p in parts if "-" in p and p[0].isdigit()), None)
    if rng:
        try:
            d1, d2 = (int(x) for x in rng.split("-")[:2])
            month, year = today.month, today.year
            mon_tok = next((p[:3].lower() for p in parts if p[:3].lower() in _RU_MONTHS), None)
            if mon_tok:
                month = _RU_MONTHS[mon_tok]
                if month < today.month:
                    year += 1
            ci = datetime.date(year, month, d1)
            co = datetime.date(year, month, d2)
            if co > ci:
                return ci, co
        except (ValueError, TypeError):
            pass
    ci = today + datetime.timedelta(days=1)
    return ci, ci + datetime.timedelta(days=1)


async def _send_html(bot, chat_id, text: str) -> None:
    """Send an HTML message, splitting if it exceeds Telegram's 4096 limit."""
    for chunk in notify.split_text(text):
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML,
                               disable_notification=config.quiet_now())


async def _reply_long(msg, text: str) -> None:
    """reply_text that never hits 'message is too long'."""
    for chunk in notify.split_text(text):
        await msg.reply_text(chunk)


async def prices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Booking.com price report (owner only). /цены [даты]"""
    user = update.effective_user
    if config.OWNER_TELEGRAM_IDS and (not user or user.id not in config.OWNER_TELEGRAM_IDS):
        await update.effective_message.reply_text("Команда доступна только владельцу.")
        return
    ci, co = _parse_price_dates(update.effective_message.text or "")
    note = await update.effective_message.reply_text("⏳ Собираю цены с Booking.com… (30–90 сек)")
    try:
        data = await asyncio.to_thread(booking_prices.scrape_prices, ci, co, False)
        text = booking_prices.format_report_html(data)
    except Exception as exc:  # noqa: BLE001
        text = f"❌ Ошибка при сборе цен: {exc}"
    try:
        await note.delete()
    except Exception:  # noqa: BLE001
        pass
    await _send_html(context.bot, update.effective_chat.id, text)


async def daily_prices(context: ContextTypes.DEFAULT_TYPE) -> None:
    """09:00 Tashkent — price report for tomorrow (1 night) to owners."""
    if not config.OWNER_TELEGRAM_IDS:
        return
    ci = datetime.date.today() + datetime.timedelta(days=1)
    co = ci + datetime.timedelta(days=1)
    try:
        data = await asyncio.to_thread(booking_prices.scrape_prices, ci, co, False)
        text = booking_prices.format_report_html(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Daily prices failed: %s", exc)
        return
    for uid in config.OWNER_TELEGRAM_IDS:
        try:
            await _send_html(context.bot, uid, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send daily prices to %s: %s", uid, exc)


def _register(user) -> None:
    """Auto-register everyone who talks to the bot (owners excluded) — the
    roster drives reminders and the roll call, no .env editing needed."""
    if not user or user.id in config.OWNER_TELEGRAM_IDS:
        return
    try:
        database.upsert_staff(user.id, user.full_name or user.first_name or "", user.username or "")
    except Exception:  # noqa: BLE001
        logger.exception("staff auto-register failed")


def _staff_roster() -> dict[int, str]:
    """uid -> display name (@username preferred). DB registry + STAFF_IDS from
    .env merged; owners and PAY_VIEWERS (accountants — they talk to the bot
    but don't clean) excluded, so they are not "absent" at the roll call."""
    skip = set(config.OWNER_TELEGRAM_IDS)
    try:
        from backend import auth as _auth
        skip |= _auth.pay_viewer_ids()
    except Exception:  # noqa: BLE001
        logger.exception("pay viewer ids failed")
    roster: dict[int, str] = {}
    for uid, nm in config.STAFF.items():
        if uid not in skip:
            roster[uid] = nm
    try:
        for r in database.all_staff():
            uid = r["staff_id"]
            if uid in skip:
                continue
            roster[uid] = f"@{r['username']}" if r.get("username") else (r.get("name") or str(uid))
    except Exception:  # noqa: BLE001
        logger.exception("staff roster read failed")
    return roster


async def staff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: show the auto-registered staff roster with removal hints."""
    user = update.effective_user
    if config.OWNER_TELEGRAM_IDS and (not user or user.id not in config.OWNER_TELEGRAM_IDS):
        return
    roster = _staff_roster()
    if not roster:
        await update.effective_message.reply_text(
            "Список пуст. Сотрудник попадает в него автоматически, как только "
            "напишет боту /start или пришлёт локацию."
        )
        return
    lines = ["👥 Сотрудники (авто-список):"]
    for uid, nm in sorted(roster.items(), key=lambda x: x[1].lower()):
        lines.append(f"  • {nm} — {uid}")
    lines.append("")
    lines.append("Убрать уволенного: /staff_del <id>")
    await _reply_long(update.message, "\n".join(lines))


async def staff_del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: /staff_del <id> — stop tracking a person (fired staff)."""
    user = update.effective_user
    if config.OWNER_TELEGRAM_IDS and (not user or user.id not in config.OWNER_TELEGRAM_IDS):
        return
    parts = (update.effective_message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.effective_message.reply_text("Использование: /staff_del <id> (id смотри в /staff)")
        return
    database.set_staff_active(int(parts[1]), False)
    await update.effective_message.reply_text(
        "✅ Убран из списка. Если человек снова напишет боту — вернётся автоматически."
    )



# ---------------------------------------------------------------------------
# Cleaning reports: photo / video / кружок (video note) — ДО и ПОСЛЕ уборки
# ---------------------------------------------------------------------------
# A report is media + an apartment number. The number can be in the caption
# (photos/videos) or arrive as a separate text message before/after the media —
# required for кружки, which Telegram does not allow captions on. Cyrillic
# letters in numbers are understood: б-051 == B-051.
#
# "до 103" (or "oldin 103") opens a cleaning session for the apartment, locked
# to that cleaner; a later "103" from the same person closes it and the
# duration is recorded. Someone else's "103" can't close it — no mix-ups.
_REPORT_TTL = 15 * 60  # seconds to pair media with a number
# uid -> [(kind, file_id, ts), ...] — several кружки may precede one number
_pending_media: dict[int, list[tuple[str, str, float]]] = {}
_pending_apt: dict[int, tuple[str, str, float]] = {}     # uid -> (apartment, phase, ts)
_last_media: dict[int, tuple[str, str, float]] = {}      # uid -> last media used in a report
_album_apt: dict[str, str] = {}                          # media_group_id -> apartment
_album_prompted: set[str] = set()                        # albums already asked for a number
_LAST_MEDIA_TTL = 5 * 60  # a «103» right after a кружок closes the session with that кружок

_CYR2LAT = str.maketrans({"А": "A", "В": "B", "Б": "B", "С": "C", "Е": "E"})

# Words that mark a report as "before cleaning" — in Russian, Uzbek (latin)
# and English, since not every cleaner has a Cyrillic keyboard: "до 103",
# "do 103", "do-135", "do135", "oldin 103", "start 103". "до 15:00" (a time)
# is not one of them. Longer words come first so "done" is not read as "do".
_START_WORDS = (
    r"boshladim|boshlayapman|начинаю|начало|начала|начал|before|oldin|avval|kirdim|start|старт|до|do"
)
# "after cleaning" words — the default anyway, but they may be glued to the
# number ("posle135", "keyin-103") and must be split off before matching it
_FINISH_WORDS = (
    r"закончила|закончил|tugatdim|tugadim|tugadi|tayyor|tamom|finish|готово|после|posle|keyin"
    r"|убрала|убрал|after|конец|done|end"
)
_START_RE = re.compile(
    rf"(?iu)(?<![a-zа-яё])(?:{_START_WORDS})(?![a-zа-яё])(?!\s*\d{{1,2}}[:.]\d{{2}})"
)
# "do135" / "posle-135" / "до_б051" → "do 135" / "posle 135" / "до б051"
_GLUED_PHASE_RE = re.compile(
    rf"(?iu)(?<![a-zа-яё])({_START_WORDS}|{_FINISH_WORDS})[\s\-–—_:.]*"
    rf"(?=\d|[a-zа-яё]\s*-?\s*\d)"
)


def _split_phase_words(text: str) -> str:
    """Put a space between a phase word and the number glued to it."""
    return _GLUED_PHASE_RE.sub(lambda m: m.group(1) + " ", text or "")

_apt_cache: list = [0.0, []]  # [expires_ts, names]


def _apartment_names() -> list[str]:
    """Live apartment list: static map + names seen in synced bookings (the
    backend auto-discovers new RC units into the DB; the bot reads them from
    there since the processes are separate). Cached for 5 minutes."""
    import time as _time
    if _time.time() < _apt_cache[0] and _apt_cache[1]:
        return _apt_cache[1]
    names = set(config.APARTMENTS.values())
    try:
        names.update(database.distinct_apartments())
    except Exception:  # noqa: BLE001
        logger.exception("distinct_apartments failed")
    _apt_cache[0] = _time.time() + 300
    _apt_cache[1] = sorted(names)
    return _apt_cache[1]


def _match_apartment(text: str, allow_bare: bool = False):
    """Find an apartment code in free text: 'убрала б-051' -> 'B-051'.

    Matches whole tokens only ('полотенца 2 для 103' must not become A-210).
    With allow_bare a standalone 3-digit number resolves too: '103' -> 'B-103'."""
    if not text:
        return None
    text = _split_phase_words(text)
    names = _apartment_names()
    up = text.upper()
    # 1) the full name as a whole token sequence: "BLV 2A-156", "B-103", "б 103".
    #    Cyrillic→Latin only for code-like tokens (single letters, letter+digits);
    #    ordinary words («убрала») must never turn into apartment letters.
    toks = []
    for tok in re.findall(r"[A-ZА-ЯЁ0-9]+", up):
        if len(tok) == 1 or any(ch.isdigit() for ch in tok):
            tok = tok.translate(_CYR2LAT)
        toks.append(tok)
    t = " " + " ".join(toks) + " "
    for name in names:
        n = " ".join(re.findall(r"[A-ZА-ЯЁ0-9]+", str(name).upper()))
        if n and f" {n} " in t:
            return name
    # 2) letter+digits glued or split by a dash: "B103", "b-103", "б103".
    #    The letter must be a token of its own (the final «а» of «убрала 103»
    #    is not an apartment letter), so look at the untranslated text.
    for letter, num in re.findall(r"(?<![A-ZА-ЯЁ0-9])([A-ZА-Я])\s*-?\s*(\d{3})(?![0-9])", text.upper()):
        cand = f"{letter.translate(_CYR2LAT)}-{num}"
        if cand in names:
            return cand
    if allow_bare:
        for num in re.findall(r"(?<![A-ZА-ЯЁ0-9])(\d{3})(?![0-9])", up):
            hits = [a for a in names if str(a).endswith(num)]
            if len(hits) == 1:
                return hits[0]
    return None


def _thread_of(msg):
    """Forum topic id of a message. Telegram also fills message_thread_id for
    plain replies in General / non-forum groups (the reply chain root), so only
    real topic messages count."""
    try:
        return msg.message_thread_id if msg.is_topic_message else None
    except Exception:  # noqa: BLE001
        return None


def _report_phase(text: str) -> str:
    """'start' for a ДО report ("до 103"), otherwise 'finish'."""
    return "start" if text and _START_RE.search(text) else "finish"


def _display_name(user) -> str:
    if user and user.username:
        return f"@{user.username}"
    return (user.full_name if user else "") or "Сотрудник"


def _media_of(msg):
    if msg.video_note:
        return "video_note", msg.video_note.file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    doc = msg.document
    if doc and (doc.mime_type or "").split("/")[0] in ("image", "video"):
        return "document", doc.file_id  # photo/video sent "as a file" (no compression)
    return None, None


def _fmt_dur(mins) -> str:
    mins = int(mins or 0)
    h, m = divmod(mins, 60)
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def _hm(ts: str | None) -> str:
    return (ts or "")[11:16] or "—"


async def _forward_media(context, chat_id, kind, file_id, caption=None, thread=None) -> None:
    q = config.quiet_now()
    kw = {"chat_id": chat_id, "disable_notification": q}
    if thread:
        kw["message_thread_id"] = thread
    if kind == "video_note":
        await context.bot.send_video_note(video_note=file_id, **kw)
        if caption:
            await context.bot.send_message(text=caption, **kw)
    elif kind == "video":
        await context.bot.send_video(video=file_id, caption=caption, **kw)
    elif kind == "document":
        await context.bot.send_document(document=file_id, caption=caption, **kw)
    else:
        await context.bot.send_photo(photo=file_id, caption=caption, **kw)


async def _forward_report(context, kind, file_id, caption, src_chat=None, src_thread=None) -> None:
    """Forward the media into the cleaning topic of every team chat (not back
    into the chat/thread it came from)."""
    for chat_id in config.NOTIFY_CHAT_IDS:
        thread = None
        try:
            thread = await asyncio.to_thread(database.get_topic, chat_id, "cleaning")
        except Exception:  # noqa: BLE001
            pass
        if src_chat is not None and chat_id == src_chat and (thread or None) == (src_thread or None):
            continue  # already visible right here — don't echo it back
        try:
            await _forward_media(context, chat_id, kind, file_id, caption, thread)
        except Exception as exc:  # noqa: BLE001
            logger.warning("report forward to %s failed: %s", chat_id, exc)


def _minutes_between(earlier_iso: str | None, now: datetime.datetime):
    if not earlier_iso:
        return None
    try:
        earlier = datetime.datetime.fromisoformat(earlier_iso)
    except ValueError:
        return None
    mins = int((now - earlier).total_seconds() // 60)
    return mins if mins >= 0 else None


def _force_close(session: dict, now: datetime.datetime) -> None:
    """Close a session nobody finished (owner's /сброс, or too old). The
    apartment is NOT marked cleaned — nobody confirmed it — so the 18:00
    control and the dashboard keep asking for the report; the elapsed time is
    not stored as a duration (it would be meaningless)."""
    database.finish_session(session["id"], now.isoformat(timespec="seconds"), None, True)
    try:
        day = session.get("work_date") or now.date().isoformat()
        if database.get_cleaning_status(session["apartment"], day) == "in_progress":
            database.set_cleaning_status(session["apartment"], day, "pending")
    except Exception:  # noqa: BLE001
        logger.exception("set_cleaning_status on force close failed")


def _session_step(apt: str, phase: str, uid: int, who: str, now: datetime.datetime) -> dict:
    """Apply a ДО/ПОСЛЕ report to the cleaning sessions (runs in a thread).
    Returns the caption for the forwarded media and the reply to the sender."""
    today = now.date().isoformat()
    ts = now.isoformat(timespec="seconds")
    hm = now.strftime("%H:%M")
    max_min = int(config.SESSION_MAX_HOURS * 60)
    cur = database.open_session(apt)
    if cur and (_minutes_between(cur.get("started_at"), now) or 0) > max_min:
        _force_close(cur, now)  # a stale leftover must not block anyone
        cur = None

    if phase == "start":
        if cur:
            if cur.get("staff_id") == uid:
                return {
                    "caption": f"📎 {apt} · доп. видео ДО · {who} · {hm}",
                    "reply": f"Уборка {apt} уже начата в {_hm(cur.get('started_at'))} — "
                             f"жду отчёт «после»: кружок + «{apt}».",
                }
            return {
                "caption": f"⚠️ {apt} · видео от {who} · квартиру убирает "
                           f"{cur.get('staff_name')} с {_hm(cur.get('started_at'))}",
                "reply": f"⛔ {apt} уже убирает {cur.get('staff_name')} с "
                         f"{_hm(cur.get('started_at'))}. Пока уборка не закрыта, "
                         f"начать её заново нельзя.",
            }
        mine = database.open_session_for_staff(uid)
        if mine and (_minutes_between(mine.get("started_at"), now) or 0) > max_min:
            _force_close(mine, now)  # yesterday's forgotten «после» — don't block today
            mine = None
        if mine:
            return {
                "caption": None,
                "reply": f"⚠️ У вас уже открыта уборка {mine.get('apartment')} с "
                         f"{_hm(mine.get('started_at'))}. Сначала закройте её: "
                         f"кружок + «{mine.get('apartment')}», потом начинайте {apt}.",
            }
        # travel time: from the previous finished apartment, else from the
        # morning arrival (live location)
        prev = database.last_finished_session(uid, today)
        ref = prev.get("finished_at") if prev else None
        if not ref:
            arr = database.arrival_for(uid, today)
            ref = arr.get("arrived_at") if arr else None
        travel = _minutes_between(ref, now)
        if travel is not None and travel > 6 * 60:
            travel = None  # a whole-day gap is not a "transfer"
        database.start_session(apt, uid, who, today, ts, travel)
        database.set_cleaning_status(apt, today, "in_progress")
        trav = f" · 🚶 переход {_fmt_dur(travel)}" if travel is not None else ""
        return {
            "caption": f"▶️ ДО · {apt} · {who} · {hm}{trav}",
            "reply": f"▶️ {apt} — уборка начата в {hm}. Когда закончите — "
                     f"кружок + «{apt}».",
            "status": "in_progress",
        }

    # ---- finish ----
    if cur:
        if cur.get("staff_id") != uid:
            return {
                "caption": f"⚠️ {apt} · видео от {who} · квартиру убирает "
                           f"{cur.get('staff_name')} с {_hm(cur.get('started_at'))}",
                "reply": f"⛔ {apt} убирает {cur.get('staff_name')} с "
                         f"{_hm(cur.get('started_at'))}. Закрыть уборку может только он(а).",
            }
        dur = _minutes_between(cur.get("started_at"), now) or 0
        database.finish_session(cur["id"], ts, dur)
        # a cleaning that crossed midnight belongs to the day it started
        database.set_cleaning_status(apt, cur.get("work_date") or today, "done")
        return {
            "caption": f"✅ ПОСЛЕ · {apt} · {who} · {_hm(cur.get('started_at'))}–{hm} · {_fmt_dur(dur)}",
            "reply": f"✅ {apt} — уборка завершена, {_fmt_dur(dur)}. Спасибо!",
            "status": "done",
        }
    last = database.session_for(apt, today)
    if last and last.get("finished_at"):
        if last.get("forced") and last.get("staff_id") == uid:
            # the owner's /сброс or the auto-close hit while the cleaner was still
            # working — this «после» is the real report: close that same session
            dur = _minutes_between(last.get("started_at"), now)
            if dur is not None and dur > max_min:
                dur = None  # too long to be a meaningful duration
            database.finish_session(last["id"], ts, dur, forced=False)
            database.set_cleaning_status(apt, last.get("work_date") or today, "done")
            took = f" · {_fmt_dur(dur)}" if dur is not None else ""
            return {
                "caption": f"✅ ПОСЛЕ · {apt} · {who} · {_hm(last.get('started_at'))}–{hm}{took}",
                "reply": f"✅ {apt} — уборка завершена{took}. Спасибо!",
                "status": "done",
            }
        return {
            "caption": f"📎 {apt} · доп. видео · {who} · {hm}",
            "reply": f"Принял доп. видео к уборке {apt} (закрыта в {_hm(last.get('finished_at'))}). "
                     f"Если это новая уборка — начните с «до {apt}».",
        }
    database.add_closed_session(apt, uid, who, today, ts)
    database.set_cleaning_status(apt, today, "done")
    return {
        "caption": f"✅ ПОСЛЕ · {apt} · {who} · {hm} · без отчёта «до»",
        "reply": f"✅ {apt} — отмечена как убранная. В следующий раз пришлите кружок "
                 f"ДО уборки с текстом «до {apt}» — так посчитается время уборки.",
        "status": "done",
    }


async def _process_report(context, msg, user, apt: str, phase: str, media: list) -> None:
    """`media` — one or more (kind, file_id) the person sent for this report."""
    now = datetime.datetime.now()
    if media:
        if len(_last_media) > 200:
            _last_media.clear()
        _last_media[user.id] = (media[-1][0], media[-1][1], now.timestamp())
    try:
        res = await asyncio.to_thread(_session_step, apt, phase, user.id, _display_name(user), now)
    except Exception:  # noqa: BLE001
        logger.exception("session step failed")
        try:  # never say "принято" when nothing was recorded
            await msg.reply_text(f"⚠️ Не удалось записать отчёт по {apt}. Отправьте ещё раз через минуту.")
        except Exception:  # noqa: BLE001
            pass
        return
    if res.get("caption"):
        for i, (kind, file_id) in enumerate(media):
            cap = res["caption"] if i == 0 else None
            await _forward_report(context, kind, file_id, cap, msg.chat_id, _thread_of(msg))
    try:
        await msg.reply_text(res["reply"])
    except Exception:  # noqa: BLE001
        pass


# ---- forum topics -----------------------------------------------------------
# In a group with topics the bot reads cleaning reports ONLY in the topic bound
# as «Уборки» — a кружок in «Общение» is just a кружок. The binding is made
# with /topic уборки, or learned automatically from the topic's name the first
# time someone writes there (Telegram attaches the topic's creation message,
# and with it the name, to every message in the topic).
_topic_cache: dict[int, tuple[float, dict]] = {}   # chat_id -> (expires, {role: thread_id})
_TOPIC_CACHE_TTL = 60.0
_topic_seen: set[tuple[int, int]] = set()          # (chat_id, thread_id) already inspected
_unbound_warned: dict[int, str] = {}               # chat_id -> day the owner was told

_TOPIC_NAME_ROLES = (
    ("cleaning", ("уборк", "убор", "clean", "tozal", "отчёт", "отчет", "hisobot")),
    ("attendance", ("явк", "приход", "attend", "davomat", "локац", "location", "kelish")),
    ("issues", ("полом", "ремонт", "неисправ", "issue", "broken", "repair", "buzil", "muammo", "ta'mir", "tamir")),
)


def _role_for_topic_name(name: str) -> str | None:
    low = (name or "").lower()
    for role, needles in _TOPIC_NAME_ROLES:
        if any(n in low for n in needles):
            return role
    return None


def _forget_topics(chat_id: int) -> None:
    _topic_cache.pop(chat_id, None)


async def _bindings(chat_id: int) -> dict:
    """{role: thread_id} for the chat, cached for a minute (every group
    message goes through this — the DB must not be hit each time)."""
    import time as _time
    hit = _topic_cache.get(chat_id)
    if hit and hit[0] > _time.time():
        return hit[1]
    try:
        rows = await asyncio.to_thread(database.chat_topics, chat_id)
    except Exception:  # noqa: BLE001
        logger.exception("chat_topics failed")
        return hit[1] if hit else {}
    data = {r["role"]: r.get("thread_id") for r in rows}
    if len(_topic_cache) > 100:
        _topic_cache.clear()
    _topic_cache[chat_id] = (_time.time() + _TOPIC_CACHE_TTL, data)
    return data


def _topic_name_of(msg) -> str:
    """Name of the forum topic a message was posted in ('' when unknown)."""
    try:
        rt = msg.reply_to_message
        if rt is not None and rt.forum_topic_created is not None:
            return rt.forum_topic_created.name or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _observe_topic(msg, context) -> None:
    """Auto-bind a topic by its name when the chat has no binding for that
    role yet — so «Уборки» works without the owner typing /topic."""
    thread = _thread_of(msg)
    if not thread or msg.chat.type == "private":
        return
    key = (msg.chat_id, thread)
    if key in _topic_seen:
        return
    if len(_topic_seen) > 500:
        _topic_seen.clear()
    _topic_seen.add(key)
    name = _topic_name_of(msg)
    role = _role_for_topic_name(name)
    if not role:
        return
    bound = await _bindings(msg.chat_id)
    if role in bound:
        return  # the owner's /topic wins
    try:
        await asyncio.to_thread(database.set_topic, msg.chat_id, role, thread, name)
    except Exception:  # noqa: BLE001
        logger.exception("auto set_topic failed")
        return
    _forget_topics(msg.chat_id)
    logger.info("topic auto-bound: chat=%s role=%s thread=%s name=%r", msg.chat_id, role, thread, name)
    what = {"cleaning": "отчёты горничных, контроль 18:00 и вечерний план",
            "attendance": "приходы и перекличка",
            "issues": "всё, что там пишут, бот будет заносить в Контроль → Закупки и Задачи",
            }.get(role, role)
    text = (f"🔗 В группе «{msg.chat.title or msg.chat_id}» тема «{name}» привязана автоматически: "
            f"туда пойдут {what}.\nИзменить: отправьте /topic внутри нужной темы.")
    for uid in config.OWNER_TELEGRAM_IDS:
        try:
            await context.bot.send_message(chat_id=uid, text=text, disable_notification=True)
        except Exception:  # noqa: BLE001
            pass


def _is_forum(msg) -> bool:
    """A group with topics enabled (Telegram marks the chat; a message that
    sits inside a topic is proof as well)."""
    try:
        return bool(getattr(msg.chat, "is_forum", False) or msg.is_topic_message)
    except Exception:  # noqa: BLE001
        return False


async def _in_cleaning_topic(msg, context=None) -> bool:
    """Where a cleaning report counts:
    * private chat — always;
    * group without topics — anywhere;
    * group with topics — only in the topic bound as «Уборки». With no binding
      yet, nothing is accepted (the owner is told once a day how to bind)."""
    if msg.chat.type == "private":
        return True
    bound = await _bindings(msg.chat_id)
    if "cleaning" in bound:
        return (bound["cleaning"] or None) == _thread_of(msg)
    if not _is_forum(msg):
        return True  # an ordinary group: no topics to choose from
    if context is not None:
        await _warn_unbound(msg, context)
    return False


async def _warn_unbound(msg, context) -> None:
    today = datetime.date.today().isoformat()
    if _unbound_warned.get(msg.chat_id) == today:
        return
    _unbound_warned[msg.chat_id] = today
    text = (f"⚠️ В группе «{msg.chat.title or msg.chat_id}» пришёл отчёт (кружок/фото), но тема "
            f"«Уборки» не привязана — такие сообщения бот пропускает.\n\n"
            f"Как привязать: зайдите в тему «Уборки» и отправьте там /topic уборки. "
            f"Или просто попросите горничную прислать отчёт в теме с названием «Уборки» — "
            f"бот привяжет её сам.")
    for uid in config.OWNER_TELEGRAM_IDS:
        try:
            await context.bot.send_message(chat_id=uid, text=text, disable_notification=True)
        except Exception:  # noqa: BLE001
            pass


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo/video/кружок from staff → cleaning report (paired with a number)."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    _register(user)
    kind, file_id = _media_of(msg)
    if not kind:
        return
    uid = user.id
    now_ts = datetime.datetime.now().timestamp()
    mgid = str(msg.media_group_id) if msg.media_group_id else None

    # album mate of an already-announced report: forward silently
    if mgid and mgid in _album_apt and not msg.caption:
        await _forward_report(context, kind, file_id, None, msg.chat_id, _thread_of(msg))
        return
    await _observe_topic(msg, context)
    if not await _in_cleaning_topic(msg, context):
        return  # a photo somewhere else in the group — not a report, whatever the caption says

    caption = msg.caption or ""
    apt = _match_apartment(caption, allow_bare=True)
    phase = _report_phase(caption)
    if not apt:
        pend = _pending_apt.get(uid)
        if pend and now_ts - pend[2] < _REPORT_TTL:
            apt, phase = pend[0], pend[1]
            # «до 103» stays valid for further кружки (hall, kitchen…) until the
            # TTL; a «после» number is consumed by its first media
            if phase != "start":
                _pending_apt.pop(uid, None)
        elif pend:
            _pending_apt.pop(uid, None)
    if apt:
        if mgid:
            if len(_album_apt) > 200:
                _album_apt.clear()
            _album_apt[mgid] = apt
        await _process_report(context, msg, user, apt, phase, [(kind, file_id)])
        return

    # number not known yet: hold the media and ask for it (once per album)
    if len(_pending_media) > 200:
        _pending_media.clear()
    lst = [m for m in _pending_media.get(uid, []) if now_ts - m[2] < _REPORT_TTL]
    lst.append((kind, file_id, now_ts))
    _pending_media[uid] = lst[-10:]
    if mgid:
        if mgid in _album_prompted:
            return
        if len(_album_prompted) > 500:
            _album_prompted.clear()
        _album_prompted.add(mgid)
    try:
        await msg.reply_text(
            "Принял! Теперь напишите номер квартиры:\n"
            "• «до Б-051» / «do 051» / «oldin 051» — если это видео ДО уборки\n"
            "• «Б-051» / «051» — если уборка закончена"
        )
    except Exception:  # noqa: BLE001
        pass


async def on_text_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Second half of a cleaning report: the apartment number.

    Reacts ONLY when this person has media waiting for a number (we asked for
    it), so the bot stays silent in the work group's normal conversation. In a
    private chat the number may also come first, media after."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return
    uid = user.id
    now_ts = datetime.datetime.now().timestamp()
    private = msg.chat.type == "private"
    if not private:
        await _observe_topic(msg, context)
        if not await _in_cleaning_topic(msg):
            return  # numbers count only in the «Уборки» topic, like the media
    pend_all = _pending_media.get(uid) or []
    pend = [m for m in pend_all if now_ts - m[2] < _REPORT_TTL]
    waiting = bool(pend)
    expired = bool(pend_all) and not waiting  # asked for a number, answer came too late

    if not waiting and not private:
        if expired and _match_apartment(msg.text, allow_bare=True):
            _pending_media.pop(uid, None)
            try:
                await msg.reply_text(
                    "⌛ Прошло больше 15 минут — кружок уже не привязать. "
                    "Пришлите его ещё раз вместе с номером."
                )
            except Exception:  # noqa: BLE001
                pass
        return  # ordinary group chatter — not our business

    apt = _match_apartment(msg.text, allow_bare=waiting or private)
    if not apt:
        if waiting and private:
            try:
                await msg.reply_text(
                    "Не понял номер квартиры. Напишите, например: Б-051 или «до Б-051» "
                    "(латиницей: b-051, do 051, oldin 051)"
                )
            except Exception:  # noqa: BLE001
                pass
        return
    _register(user)
    phase = _report_phase(msg.text)
    if waiting:
        _pending_media.pop(uid, None)
        await _process_report(context, msg, user, apt, phase, [(k, f) for k, f, _t in pend])
    elif private:
        # «до 103» → кружок → (short cleaning) → кружок → «103»: the second
        # кружок was attached to the open session as "доп. видео ДО", so the
        # «103» that follows it IS the finish — close with that кружок instead
        # of asking for yet another one.
        lm = _last_media.get(uid)
        if phase == "finish" and lm and now_ts - lm[2] < _LAST_MEDIA_TTL:
            try:
                cur = await asyncio.to_thread(database.open_session, apt)
            except Exception:  # noqa: BLE001
                cur = None
            if cur and cur.get("staff_id") == uid:
                _pending_apt.pop(uid, None)
                _last_media.pop(uid, None)
                await _process_report(context, msg, user, apt, "finish", [(lm[0], lm[1])])
                return
        # number first, media to follow (private chat only)
        if len(_pending_apt) > 200:
            _pending_apt.clear()
        _pending_apt[uid] = (apt, phase, now_ts)
        what = "ДО уборки" if phase == "start" else "после уборки"
        try:
            await msg.reply_text(f"Записал: {apt} ({what}). Теперь отправьте кружок, видео или фото 📸")
        except Exception:  # noqa: BLE001
            pass


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: /сброс 103 — force-close an open cleaning session (the cleaner
    left without the «после» report). Without an argument: list open ones."""
    user = update.effective_user
    msg = update.effective_message
    if config.OWNER_TELEGRAM_IDS and (not user or user.id not in config.OWNER_TELEGRAM_IDS):
        return
    now = datetime.datetime.now()
    parts = (msg.text or "").split(maxsplit=1)
    apt = _match_apartment(parts[1], allow_bare=True) if len(parts) > 1 else None
    if not apt:
        open_ = await asyncio.to_thread(database.open_sessions)
        if not open_:
            await msg.reply_text("Открытых уборок сейчас нет.")
            return
        lines = ["⏳ Открытые уборки:"]
        for s in open_:
            lines.append(f"  • {s['apartment']} — {s.get('staff_name')} с {_hm(s.get('started_at'))}")
        lines.append("")
        lines.append("Закрыть принудительно: /сброс <номер>")
        await _reply_long(msg, "\n".join(lines))
        return
    cur = await asyncio.to_thread(database.open_session, apt)
    if not cur:
        await msg.reply_text(f"Открытой уборки {apt} нет.")
        return
    await asyncio.to_thread(_force_close, cur, now)
    await msg.reply_text(
        f"✅ Уборка {apt} ({cur.get('staff_name')}, с {_hm(cur.get('started_at'))}) закрыта "
        f"принудительно — в статистике помечена как незавершённая, квартира остаётся "
        f"«без отчёта» до кружка «после»."
    )


async def sessions_autoclose(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 30 min: close sessions older than SESSION_MAX_HOURS with no
    «после» report (the cleaner forgot), and tell the owner. Age-based rather
    than a fixed hour, so a late 22:40–23:20 cleaning is left alone."""
    now = datetime.datetime.now()
    cutoff = (now - datetime.timedelta(hours=config.SESSION_MAX_HOURS)).isoformat(timespec="seconds")
    try:
        stale = await asyncio.to_thread(database.stale_open_sessions, cutoff)
    except Exception:  # noqa: BLE001
        logger.exception("autoclose read failed")
        return
    if not stale:
        return
    lines = [f"🌙 Автозакрытие уборок без отчёта «после» (прошло больше {config.SESSION_MAX_HOURS:g} ч):"]
    for s in stale:
        try:
            await asyncio.to_thread(_force_close, s, now)
        except Exception:  # noqa: BLE001
            logger.exception("autoclose failed for %s", s.get("apartment"))
        lines.append(f"  • {s['apartment']} — {s.get('staff_name')}, начата "
                     f"{(s.get('started_at') or '')[5:10]} {_hm(s.get('started_at'))}")
    for uid in config.OWNER_TELEGRAM_IDS:
        try:
            await context.bot.send_message(chat_id=uid, text="\n".join(lines),
                                           disable_notification=config.quiet_now())
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Supplies: "нужно 103 полотенца 2, шампунь"
# ---------------------------------------------------------------------------
async def on_supplies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text or not supplies.is_request(msg.text):
        return
    private = msg.chat.type == "private"
    apt, items = supplies.parse_items(msg.text, lambda t: _match_apartment(t, allow_bare=True))
    if not items:
        if private:
            try:
                await msg.reply_text(
                    "Что купить? Напишите, например:\n«нужно 103 полотенца 2, шампунь, туалетная бумага»"
                )
            except Exception:  # noqa: BLE001
                pass
        return
    if not private and not supplies.looks_like_list(apt, items):
        return  # "нужно перезвонить гостю" in the group is not a shopping list
    _register(user)
    who = _display_name(user)
    now = datetime.datetime.now().isoformat(timespec="minutes")
    for item, qty in items:
        try:
            await asyncio.to_thread(database.add_supply, apt, item, qty, who, now)
        except Exception:  # noqa: BLE001
            logger.exception("add_supply failed")
    shown = ", ".join(f"{it} ×{q}" if q > 1 else it for it, q in items)
    where = f" ({apt})" if apt else ""
    try:
        await msg.reply_text(f"📝 Записал в закупки: {shown}{where}")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# «Поломки» topic: every line becomes a purchase or a repair task
# ---------------------------------------------------------------------------
async def on_issue_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs in its own handler group before everything else. Inside the topic
    bound as «Поломки» it records the lines and stops the other handlers (so
    «нужно …» there is not written twice); elsewhere it does nothing."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text or msg.chat.type == "private":
        return
    await _observe_topic(msg, context)
    bound = await _bindings(msg.chat_id)
    if "issues" not in bound or (bound["issues"] or None) != _thread_of(msg):
        return
    items = issues.parse_lines(msg.text, lambda t: _match_apartment(t, allow_bare=True))
    if not items:
        raise ApplicationHandlerStop  # chatter in «Поломки» is nobody else's business either
    _register(user)
    who = _display_name(user)
    now = datetime.datetime.now().isoformat(timespec="minutes")
    added_buy: list[str] = []
    added_fix: list[str] = []
    for it in items:
        key = issues.src_key(msg.chat_id, msg.message_id, it["kind"], it["apartment"], it["text"])
        try:
            if it["kind"] == "buy":
                if await asyncio.to_thread(database.src_recorded, "supplies", key):
                    continue  # an edited message: this line was recorded already
                await asyncio.to_thread(database.add_supply, it["apartment"], it["text"], it["qty"],
                                        who, now, key)
                added_buy.append(f"{it['text']}{' ×' + str(it['qty']) if it['qty'] > 1 else ''}"
                                 + (f" ({it['apartment']})" if it["apartment"] else ""))
            else:
                if await asyncio.to_thread(database.src_recorded, "tasks", key):
                    continue
                await asyncio.to_thread(database.add_task, it["apartment"], it["text"], None, None,
                                        f"{who} · чат «Поломки»", key)
                added_fix.append(it["text"] + (f" ({it['apartment']})" if it["apartment"] else ""))
        except Exception:  # noqa: BLE001
            logger.exception("issue line failed: %r", it)
    if added_buy or added_fix:
        logger.info("issues: %d purchases, %d tasks from %s", len(added_buy), len(added_fix), who)
        # a quiet acknowledgement: a reaction, not another message in the chat
        try:
            await context.bot.set_message_reaction(chat_id=msg.chat_id, message_id=msg.message_id,
                                                   reaction="✍")
        except Exception as exc:  # noqa: BLE001
            logger.warning("reaction failed (%s) — replying instead", exc)
            parts = []
            if added_buy:
                parts.append("в закупки: " + ", ".join(added_buy))
            if added_fix:
                parts.append("в задачи: " + ", ".join(added_fix))
            try:
                await msg.reply_text("📝 Записал " + "; ".join(parts), disable_notification=True)
            except Exception:  # noqa: BLE001
                pass
    raise ApplicationHandlerStop


async def supplies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/нужно — show the open shopping list."""
    text = await asyncio.to_thread(supplies.format_list)
    await _reply_long(update.effective_message, text)


def _fmt_sum(x: float) -> str:
    return f"{int(x):,}".replace(",", " ") + " сум"


def _auto_fine_noshow(staff_name: str, day: datetime.date) -> bool:
    """Create the no-show fine once per staff per day. Returns True if added."""
    marker = f"Автоштраф: неявка {day.strftime('%d.%m.%Y')}"
    if database.has_penalty_marker(staff_name, marker):
        return False
    dh, dm = config.ATTEND_DEADLINE_T
    database.add_penalty(
        "fine", staff_name, config.AUTO_FINE_NOSHOW,
        f"{marker} (нет live-локации до {dh:02d}:{dm:02d})",
        datetime.datetime.now().isoformat(timespec="minutes"),
    )
    return True


async def on_channel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read the payments channel (bot must be an admin there): parse each post,
    try to match it to a PMS booking, and store the result for reconciliation.
    Edits re-parse and update the stored row."""
    msg = update.channel_post or update.edited_channel_post
    if not msg:
        return
    if config.PAY_CHANNEL_IDS and msg.chat_id not in config.PAY_CHANNEL_IDS:
        return
    text = (msg.caption or msg.text or "").strip()
    if not text:
        return
    # Telegram gives UTC-aware timestamps; everything else in the DB is local
    # (Tashkent) time, and the month filter relies on it.
    posted = msg.date.astimezone().replace(tzinfo=None) if msg.date else datetime.datetime.now()
    post_date = posted.date()
    try:
        parsed = pay_parse.parse_payment(text, post_date, _apartment_names())
        parsed["_raw"] = text
        bookings = await asyncio.to_thread(database.all_active_bookings)
        bid, score = pay_parse.match_booking(parsed, bookings)
        await asyncio.to_thread(
            database.upsert_channel_payment,
            msg.chat_id, msg.message_id,
            posted.isoformat(timespec="minutes"),
            text[:1000], parsed, bid, score,
        )
        logger.info(
            "payment post chat=%s msg=%s: apt=%s amount=%s %s matched=%s score=%s",
            msg.chat_id, msg.message_id, parsed.get("apartment"),
            parsed.get("amount"), parsed.get("currency") or "", bid, score,
        )
    except Exception:  # noqa: BLE001
        logger.exception("payment post parse failed")


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everyone can ask their Telegram id — needed once to fill STAFF_IDS."""
    user = update.effective_user
    if not user:
        return
    uname = f" (@{user.username})" if user.username else ""
    await update.effective_message.reply_text(
        f"🆔 Ваш Telegram ID: {user.id}{uname}\n"
        f"Отправьте его руководителю для добавления в список сотрудников."
    )


async def attendance_remind(context: ContextTypes.DEFAULT_TYPE) -> None:
    """ATTEND_REMIND (10:00): DM every staff member who hasn't checked in yet."""
    staff = _staff_roster()
    if not staff:
        return
    today = datetime.date.today().isoformat()
    done = {r["staff_id"] for r in database.attendance_for(today)}
    dh, dm = config.ATTEND_DEADLINE_T
    sh, sm = config.ATTEND_REMIND_T  # noqa: F841 — reminder fires at this time
    text = (
        "📍 Доброе утро! Не забудьте отметить приход: находясь на объекте, "
        "отправьте боту live-геолокацию (Прикрепить → Геопозиция → «Транслировать») "
        f"до {dh:02d}:{dm:02d}.\n"
        "Без отметки день будет засчитан как неявка."
    )
    for uid, nm in staff.items():
        if uid in done:
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=text,
                                           disable_notification=config.quiet_now())
        except Exception as exc:  # noqa: BLE001
            logger.warning("attendance remind to %s (%s) failed: %s", uid, nm, exc)


async def attendance_deadline(context: ContextTypes.DEFAULT_TYPE) -> None:
    """ATTEND_DEADLINE (14:00): post the roll call to the team group."""
    staff = _staff_roster()
    if not staff:
        return
    today = datetime.date.today().isoformat()
    try:
        if await asyncio.to_thread(database.job_done, "attendance_deadline", today):
            return  # already ran today (startup catch-up + scheduled run)
    except Exception:  # noqa: BLE001
        pass
    rows = {r["staff_id"]: r for r in database.attendance_for(today)}
    dh, dm = config.ATTEND_DEADLINE_T
    lines = [f"📋 Явка на {datetime.date.today().strftime('%d.%m')}:"]
    absent = 0
    for uid, nm in staff.items():
        r = rows.get(uid)
        if r:
            t = (r.get("arrived_at") or "")[11:16]
            status = "вовремя ✅" if r.get("on_time") else f"опоздание {r.get('late_minutes', 0)} мин ⚠️"
            shown = r.get("staff_name") or nm
            lines.append(f"✅ {shown} — {t} ({status})")
        else:
            absent += 1
            line = f"❌ {nm} — не отправил(а) live-локацию до {dh:02d}:{dm:02d} → неявка"
            try:  # keep the no-show in the attendance log for the statistics
                await asyncio.to_thread(database.record_absent, uid, nm, today)
            except Exception:  # noqa: BLE001
                logger.exception("record_absent failed for %s", nm)
            if config.AUTO_FINE_NOSHOW > 0:
                try:
                    if await asyncio.to_thread(_auto_fine_noshow, nm, datetime.date.today()):
                        line += f" · штраф {_fmt_sum(config.AUTO_FINE_NOSHOW)} 📒"
                except Exception:  # noqa: BLE001
                    logger.exception("auto-fine failed for %s", nm)
            lines.append(line)
    if not absent:
        lines.append("Все на месте 💪")
    notify.send("\n".join(lines), topic="attendance")
    _loc_status.clear()  # tomorrow starts fresh
    try:
        await asyncio.to_thread(database.mark_job, "attendance_deadline", today)
    except Exception:  # noqa: BLE001
        logger.exception("mark_job failed")


async def cleaning_watch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """CLEANING_CHECK (18:00): which of today's checkouts have no cleaning
    report yet — so nothing is forgotten by the evening."""
    today = datetime.date.today()
    today_s = today.isoformat()
    try:
        if await asyncio.to_thread(database.job_done, "cleaning_watch", today_s):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        cleanings = (await asyncio.to_thread(services.build_cleaning, today, 1))["cleanings"]
    except Exception:  # noqa: BLE001
        logger.exception("cleaning_watch failed")
        return
    todays = [c for c in cleanings if c.get("cleaning_date") == today_s]
    try:
        open_ = await asyncio.to_thread(database.open_sessions)
        sessions = await asyncio.to_thread(database.sessions_for_date, today_s)
    except Exception:  # noqa: BLE001
        open_, sessions = [], []
    if not todays and not open_ and not sessions:
        return  # no checkouts today — nothing to control
    started = {s["apartment"]: s for s in open_}
    not_done = [c["apartment"] for c in todays
                if c.get("status") != "done" and c["apartment"] not in started]
    done = [c["apartment"] for c in todays if c.get("status") == "done"]
    ch, cm = config.CLEANING_CHECK_T
    lines = [f"🧹 Контроль уборок на {ch:02d}:{cm:02d}:"]
    if not_done:
        lines.append("❌ Нет отчёта об уборке: " + ", ".join(sorted(not_done)))
        lines.append("Горничные, пришлите фото/кружок с номером квартиры!")
    if started:
        lines.append("⏳ Начата, но нет отчёта «после»: " + ", ".join(
            f"{a} ({s.get('staff_name')} с {_hm(s.get('started_at'))})"
            for a, s in sorted(started.items())))
    if done:
        lines.append("✅ Убрано: " + ", ".join(sorted(done)))
    if not not_done and not started:
        lines.append("Все уборки закрыты, молодцы 💪")
    timed = [s for s in sessions if s.get("duration_min") is not None and not s.get("forced")]
    if timed:
        avg = sum(int(s["duration_min"]) for s in timed) / len(timed)
        longest = max(timed, key=lambda s: int(s["duration_min"]))
        lines.append("")
        lines.append(
            f"⏱ Сегодня уборок с таймингом: {len(timed)}, среднее {_fmt_dur(avg)}, "
            f"самая долгая — {longest['apartment']} {_fmt_dur(longest['duration_min'])} "
            f"({longest.get('staff_name')})"
        )
    notify.send("\n".join(lines), topic="cleaning")
    try:
        await asyncio.to_thread(database.mark_job, "cleaning_watch", today_s)
    except Exception:  # noqa: BLE001
        logger.exception("mark_job failed")
    # shopping list → owner only
    try:
        open_items = await asyncio.to_thread(database.open_supplies)
    except Exception:  # noqa: BLE001
        open_items = []
    if open_items:
        text = supplies.format_list(open_items)
        for uid in config.OWNER_TELEGRAM_IDS:
            try:
                await context.bot.send_message(chat_id=uid, text=text,
                                               disable_notification=config.quiet_now())
            except Exception:  # noqa: BLE001
                pass


async def attendance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await asyncio.to_thread(database.attendance_for, datetime.date.today().isoformat())
    if not rows:
        await update.effective_message.reply_text("Сегодня ещё никто не отметился на работе.")
        return
    lines = ["📋 Приходы сегодня:"]
    for r in rows:
        t = (r.get("arrived_at") or "")[11:16]
        st = "вовремя ✅" if r.get("on_time") else f"опоздание {r.get('late_minutes')} мин ⚠️"
        lines.append(f"• {r.get('staff_name')} — {t} ({st})")
    await _reply_long(update.message, "\n".join(lines))


async def daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.OWNER_TELEGRAM_IDS:
        return
    summary = await asyncio.to_thread(services.build_text_summary, datetime.date.today())
    for uid in config.OWNER_TELEGRAM_IDS:
        try:
            await context.bot.send_message(chat_id=uid, text=summary,
                                           disable_notification=config.quiet_now())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send daily summary to %s: %s", uid, exc)


async def _set_commands(app) -> None:
    """Publish the command menu: a short list for everyone, the full one in
    the owners' private chats. Runs once at startup; failures are harmless."""
    from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
    common = [
        BotCommand("start", "Открыть дашборд"),
        BotCommand("today", "Сводка на сегодня"),
        BotCommand("supplies", "Список закупок (/нужно)"),
        BotCommand("attendance", "Кто отметился сегодня"),
        BotCommand("myid", "Мой Telegram ID"),
    ]
    owner = common + [
        BotCommand("staff", "Список сотрудников"),
        BotCommand("reset", "Закрыть зависшую уборку (/сброс)"),
        BotCommand("prices", "Цены Booking.com (/цены)"),
        BotCommand("topic", "Привязать тему группы"),
        BotCommand("chatid", "ID чата / темы"),
        BotCommand("sync", "Синхронизация с RealtyCalendar"),
    ]
    try:
        await app.bot.set_my_commands(common, scope=BotCommandScopeDefault())
        for uid in config.OWNER_TELEGRAM_IDS:
            try:
                await app.bot.set_my_commands(owner, scope=BotCommandScopeChat(chat_id=uid))
            except Exception as exc:  # noqa: BLE001 — owner never opened the bot yet
                logger.info("owner command menu for %s skipped: %s", uid, exc)
    except Exception:  # noqa: BLE001
        logger.exception("set_my_commands failed")
    await _catch_up_jobs(app)


_CATCH_UP_WINDOW_H = 3  # run a missed daily job only within this many hours of its time


async def _catch_up_jobs(app) -> None:
    """The bot was down when a daily job was due (restart, crash): run it now if
    the moment is still relevant, otherwise tell the owner it was skipped.
    PTB's JobQueue never runs missed occurrences on its own."""
    now = datetime.datetime.now()
    today = now.date().isoformat()
    ctx = type("Ctx", (), {"bot": app.bot})()  # the jobs only use context.bot
    for name, (h, m), job in (
        ("attendance_deadline", config.ATTEND_DEADLINE_T, attendance_deadline),
        ("cleaning_watch", config.CLEANING_CHECK_T, cleaning_watch),
    ):
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < due:
            continue  # not yet today — the scheduler will run it
        try:
            if await asyncio.to_thread(database.job_done, name, today):
                continue
        except Exception:  # noqa: BLE001
            continue
        late_h = (now - due).total_seconds() / 3600
        if late_h <= _CATCH_UP_WINDOW_H:
            logger.info("catch-up: running missed job %s (%.1f h late)", name, late_h)
            try:
                await job(ctx)
            except Exception:  # noqa: BLE001
                logger.exception("catch-up %s failed", name)
        else:
            text = (f"⚠️ Бот был выключен в {h:02d}:{m:02d} — задача «{name}» за сегодня "
                    f"не выполнялась (прошло {late_h:.0f} ч, запускать поздно).")
            for uid in config.OWNER_TELEGRAM_IDS:
                try:
                    await app.bot.send_message(chat_id=uid, text=text, disable_notification=True)
                except Exception:  # noqa: BLE001
                    pass
            try:
                await asyncio.to_thread(database.mark_job, name, today)  # don't nag on every restart
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задан. Откройте файл .env в папке проекта и впишите токен "
            "от @BotFather: BOT_TOKEN=123456:AA...  (образец — .env.example)"
        )

    database.init_db()
    app = Application.builder().token(config.BOT_TOKEN).post_init(_set_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("sync", sync_cmd))
    app.add_handler(CommandHandler("chatid", chatid_cmd))
    app.add_handler(CommandHandler("topic", topic_cmd))
    app.add_handler(CommandHandler("attendance", attendance_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("staff", staff_cmd))
    app.add_handler(CommandHandler("staff_del", staff_del_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("supplies", supplies_cmd))
    # Telegram only detects latin /commands, so accept typed Cyrillic ones too
    # (slash required — a plain «цены» in the group is just conversation).
    _msg = filters.UpdateType.MESSAGE
    # «Поломки» topic reader — its own group, so it sees every group text
    # (new and edited) before the group-0 handlers and can stop them
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE), on_issue_text), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"(?iu)^/цены\b") & _msg, prices_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"(?iu)^/сброс\b") & _msg, reset_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"(?iu)^/(нужно|закупки|список)\b") & _msg, supplies_cmd))
    # shopping list requests: "нужно 103 полотенца 2, шампунь" (any chat)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE & filters.Regex(supplies.TRIGGER_ANY_RE),
        on_supplies))
    # live location: filters.LOCATION already matches both the initial share and
    # the live-location edits, so a single handler covers all points.
    # payments channel reader — must be registered before the media handlers
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, on_channel_payment))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    # cleaning reports: media with caption, or кружок/фото + номер отдельным сообщением.
    # UpdateType.MESSAGE: an edited caption/text must not create a second report
    # (live locations arrive as edits and keep their own handler above).
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE
         | filters.Document.IMAGE | filters.Document.VIDEO) & filters.UpdateType.MESSAGE, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE,
                                   on_text_report))

    # Daily summary at 08:00 + Booking.com price report at 09:00 (server local time).
    if not app.job_queue:
        # PTB builds a JobQueue only when APScheduler AND pytz are importable.
        # Without it every scheduled job is silently skipped — the roll call,
        # the 18:00 cleaning control, the auto-close. Make that impossible to miss.
        msg = ("РАСПИСАНИЕ ВЫКЛЮЧЕНО: не установлен модуль планировщика, поэтому "
               "перекличка 14:00, контроль уборок 18:00 и автозакрытие работать НЕ БУДУТ. "
               "Выполните в папке проекта: pip install \"python-telegram-bot[job-queue]\" pytz "
               "и перезапустите restart_all.bat")
        logger.error("%s", msg)
        print("\n" + "!" * 70 + f"\n⚠️  {msg}\n" + "!" * 70 + "\n", flush=True)
    if app.job_queue:
        app.job_queue.run_daily(daily_summary, time=datetime.time(hour=8, minute=0, tzinfo=LOCAL_TZ))
        app.job_queue.run_daily(daily_prices, time=datetime.time(hour=9, minute=0, tzinfo=LOCAL_TZ))
        # attendance workflow: morning reminder + roll call (server local time)
        rh, rm = config.ATTEND_REMIND_T
        dh, dm = config.ATTEND_DEADLINE_T
        app.job_queue.run_daily(attendance_remind, time=datetime.time(hour=rh, minute=rm, tzinfo=LOCAL_TZ))
        app.job_queue.run_daily(attendance_deadline, time=datetime.time(hour=dh, minute=dm, tzinfo=LOCAL_TZ))
        ch, cm = config.CLEANING_CHECK_T
        app.job_queue.run_daily(cleaning_watch, time=datetime.time(hour=ch, minute=cm, tzinfo=LOCAL_TZ))
        # forgotten «после» reports: close sessions older than SESSION_MAX_HOURS
        app.job_queue.run_repeating(sessions_autoclose, interval=30 * 60, first=120)

    logger.info("Bot started (demo_mode=%s)", config.DEMO_MODE)
    print("\n" + "=" * 62)
    print(f"  ✅ БОТ ЗАПУЩЕН (версия {config.APP_VERSION}). Это окно должно оставаться открытым.")
    print(f"  Лог: {LOG_PATH}")
    print("=" * 62 + "\n", flush=True)
    # ALL_TYPES guards against a token whose allowed_updates was ever narrowed by
    # a previous webhook, which would silently drop edited_message (live) updates.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def _pause(reason: str = "") -> None:
    """A console window must never disappear with the error inside it."""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input(f"\n{reason}Нажмите Enter, чтобы закрыть окно… ")
    except Exception:  # noqa: BLE001
        pass


def _friendly(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc)
    if name == "Conflict":
        return ("Бот уже запущен в другом окне (Telegram разрешает только одно подключение). "
                "Закройте лишнее окно «Nova Bot» или запустите restart_all.bat.")
    if name == "InvalidToken":
        return "Неверный BOT_TOKEN в .env — скопируйте токен из @BotFather заново."
    if name in ("NetworkError", "TimedOut"):
        return "Нет связи с Telegram. Проверьте интернет и запустите ещё раз."
    if name == "ModuleNotFoundError":
        return (f"Не установлен модуль: {text}. Выполните в папке проекта:\n"
                f"    pip install -r backend\\requirements.txt")
    return f"{name}: {text}"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:  # noqa: PERF203 — configuration problem, show it and wait
        if exc.code not in (0, None):
            print(f"\n❌ {exc.code}\n", flush=True)
            _pause()
    except BaseException as exc:  # noqa: BLE001 — never let the window close silently
        path = logsetup.log_crash("bot", exc)
        logger.exception("bot crashed")
        print(f"\n❌ Бот остановился с ошибкой.\n\n{_friendly(exc)}\n\n"
              f"Подробности сохранены в файл:\n    {path}\n"
              f"Пришлите этот файл — по нему видно причину.\n", flush=True)
        _pause()
