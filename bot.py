"""Telegram bot for Nova Home Dashboard.

Provides a button that opens the Mini App, plus text fallbacks (/today, /sync)
and a daily 08:00 summary. Run with: ``python bot.py``.

Requires BOT_TOKEN and WEBAPP_URL in the environment (see .env.example).
"""
import asyncio
import datetime
import logging
import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from backend import attendance, booking_prices, config, database, notify, pay_parse, rc_sync, services

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
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
    url += ("&" if "?" in url else "?") + "v=18"  # cache-buster per release
    if config.OWNER_KEY and uid and uid in config.OWNER_TELEGRAM_IDS:
        url += "&okey=" + config.OWNER_KEY
    elif config.PAY_KEY and uid:
        # PAY_VIEWERS get their own key so the Оплаты block works on clients
        # that pass no initData (macOS Telegram)
        try:
            from backend import auth as _auth
            if uid in _auth.pay_viewer_ids():
                url += "&okey=" + config.PAY_KEY
        except Exception:  # noqa: BLE001
            logger.exception("pay viewer url check failed")
    return url


def _webapp_markup(uid=None) -> InlineKeyboardMarkup | None:
    if not config.WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 Открыть дашборд", web_app=WebAppInfo(url=_webapp_url(uid)))]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _register(update.effective_user)
    uid = update.effective_user.id if update.effective_user else None
    markup = _webapp_markup(uid)
    apt_count = len(_apartment_names()) or config.TOTAL_APARTMENTS
    text = f"🏠 *Nova Home Dashboard*\n\nОперационная сводка по {apt_count} апартаментам."
    if uid and uid in config.OWNER_TELEGRAM_IDS and not config.OWNER_KEY:
        text += "\n\n⚠️ OWNER\\_KEY не задан в .env — вкладки владельца не откроются на Mac."
    if markup:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        # Also expose a persistent keyboard button (opens WebApp from chat).
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📊 Дашборд", web_app=WebAppInfo(url=_webapp_url(uid)))]],
            resize_keyboard=True,
        )
        await update.message.reply_text("Меню:", reply_markup=kb)
    else:
        await update.message.reply_text(
            text + "\n\n⚠️ WEBAPP_URL не задан — доступна только текстовая сводка (/today).",
            parse_mode=ParseMode.MARKDOWN,
        )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    summary = await asyncio.to_thread(services.build_text_summary, datetime.date.today())
    await update.message.reply_text(summary)


async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("🔄 Синхронизация…")
    try:
        count = await asyncio.to_thread(rc_sync.sync_to_db)
        await msg.edit_text(f"✅ Синхронизировано: {count} броней")
    except Exception as exc:  # noqa: BLE001
        await msg.edit_text(f"❌ Ошибка синхронизации: {exc}")


async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report this chat's id — add the bot to your team group and run /chatid to
    get the id for NOTIFY_CHAT_IDS."""
    chat = update.effective_chat
    thread = update.effective_message.message_thread_id if update.effective_message else None
    extra = f"\nID темы: {thread}" if thread else ""
    await update.message.reply_text(
        f"ID этого чата: {chat.id}{extra}\n"
        f"Впишите его в NOTIFY_CHAT_IDS в .env, чтобы сюда приходили уведомления."
    )


_TOPIC_ROLES = {
    "уборки": "cleaning", "уборка": "cleaning", "cleaning": "cleaning",
    "явка": "attendance", "приход": "attendance", "attendance": "attendance",
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
        await msg.reply_text(
            "Использование — внутри нужной темы:\n"
            "/topic уборки — отчёты горничных и контроль 18:00\n"
            "/topic явка — приходы и перекличка\n"
            "/topic общий — всё остальное\n\n"
            "Тема General привязывается так же (отправьте команду в General)."
        )
        return
    thread = msg.message_thread_id  # None in General
    name = ""
    try:
        rt = msg.reply_to_message
        if rt and rt.forum_topic_created:
            name = rt.forum_topic_created.name
    except Exception:  # noqa: BLE001
        pass
    database.set_topic(chat.id, role, thread, name)
    where = f"«{name}»" if name else ("General" if not thread else f"тема #{thread}")
    await msg.reply_text(f"✅ Привязано: {parts[1].lower()} → {where}")


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
    logger.info(
        "attendance: status=%s distance=%.0fm radius=%sm",
        res.get("status"), res.get("distance", -1), config.WORK_RADIUS_M,
    )
    if res.get("status") == "recorded":
        if res.get("notify"):
            notify.send(res["notify"], topic="attendance")
        try:
            await msg.reply_text(res["reply"])
        except Exception:  # noqa: BLE001
            pass
    elif not is_edit:
        # "outside" / "already": answer once (on the initial share) so the person
        # gets feedback, but don't repeat on every live-location tick.
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
    while text:
        chunk, text = text[:4000], text[4000:]
        if text:  # break on a newline so tags aren't split mid-line
            cut = chunk.rfind("\n")
            if cut > 0:
                text, chunk = chunk[cut + 1:] + text, chunk[:cut]
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML,
                               disable_notification=config.quiet_now())


async def prices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Booking.com price report (owner only). /цены [даты]"""
    user = update.effective_user
    if config.OWNER_TELEGRAM_IDS and (not user or user.id not in config.OWNER_TELEGRAM_IDS):
        await update.message.reply_text("Команда доступна только владельцу.")
        return
    ci, co = _parse_price_dates(update.message.text or "")
    note = await update.message.reply_text("⏳ Собираю цены с Booking.com… (30–90 сек)")
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
    .env merged; owners excluded."""
    roster: dict[int, str] = {}
    for uid, nm in config.STAFF.items():
        if uid not in config.OWNER_TELEGRAM_IDS:
            roster[uid] = nm
    try:
        for r in database.all_staff():
            uid = r["staff_id"]
            if uid in config.OWNER_TELEGRAM_IDS:
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
        await update.message.reply_text(
            "Список пуст. Сотрудник попадает в него автоматически, как только "
            "напишет боту /start или пришлёт локацию."
        )
        return
    lines = ["👥 Сотрудники (авто-список):"]
    for uid, nm in sorted(roster.items(), key=lambda x: x[1].lower()):
        lines.append(f"  • {nm} — {uid}")
    lines.append("")
    lines.append("Убрать уволенного: /staff_del <id>")
    await update.message.reply_text("\n".join(lines))


async def staff_del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: /staff_del <id> — stop tracking a person (fired staff)."""
    user = update.effective_user
    if config.OWNER_TELEGRAM_IDS and (not user or user.id not in config.OWNER_TELEGRAM_IDS):
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("Использование: /staff_del <id> (id смотри в /staff)")
        return
    database.set_staff_active(int(parts[1]), False)
    await update.message.reply_text(
        "✅ Убран из списка. Если человек снова напишет боту — вернётся автоматически."
    )



# ---------------------------------------------------------------------------
# Cleaning reports: photo / video / кружок (video note)
# ---------------------------------------------------------------------------
# A report is media + an apartment number. The number can be in the caption
# (photos/videos) or arrive as a separate text message before/after the media —
# required for кружки, which Telegram does not allow captions on. Cyrillic
# letters in numbers are understood: б-051 == B-051.
_REPORT_TTL = 15 * 60  # seconds to pair media with a number
_pending_media: dict[int, tuple[str, str, float]] = {}   # uid -> (kind, file_id, ts)
_pending_apt: dict[int, tuple[str, float]] = {}          # uid -> (apartment, ts)
_album_apt: dict[str, str] = {}                          # media_group_id -> apartment

_CYR2LAT = str.maketrans({"А": "A", "В": "B", "Б": "B", "С": "C", "Е": "E"})

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

    With allow_bare (only when we already asked the person for a number),
    a standalone 3-digit number resolves too: '103' -> 'B-103'."""
    if not text:
        return None
    names = _apartment_names()
    t = re.sub(r"[^A-Z0-9]", "", text.upper().translate(_CYR2LAT))
    for name in names:
        n = re.sub(r"[^A-Z0-9]", "", str(name).upper())
        if n and n in t:
            return name
    if allow_bare:
        for num in re.findall(r"\d{3}", text):
            hits = [a for a in names if str(a).endswith(num)]
            if len(hits) == 1:
                return hits[0]
    return None


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
    return None, None


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
    else:
        await context.bot.send_photo(photo=file_id, caption=caption, **kw)


async def _send_report(context, apt: str, kind: str, file_id: str, who: str,
                       src_chat=None, src_thread=None) -> None:
    """Mark the apartment cleaned today and forward the media to the group."""
    now = datetime.datetime.now()
    try:
        await asyncio.to_thread(
            database.set_cleaning_status, apt, now.date().isoformat(), "done"
        )
    except Exception:  # noqa: BLE001
        logger.exception("set_cleaning_status from report failed")
    caption = f"🧹 {apt} — уборка выполнена ✅\n{who} · {now.strftime('%H:%M')}"
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
        for chat_id in config.NOTIFY_CHAT_IDS:
            try:
                thread = await asyncio.to_thread(database.get_topic, chat_id, "cleaning")
                if chat_id == msg.chat_id and (thread or None) == (msg.message_thread_id or None):
                    continue
                await _forward_media(context, chat_id, kind, file_id, None, thread)
            except Exception:  # noqa: BLE001
                pass
        return

    apt = _match_apartment(msg.caption or "")
    if not apt:
        pend = _pending_apt.pop(uid, None)
        if pend and now_ts - pend[1] < _REPORT_TTL:
            apt = pend[0]
    if apt:
        if mgid:
            if len(_album_apt) > 200:
                _album_apt.clear()
            _album_apt[mgid] = apt
        await _send_report(context, apt, kind, file_id, _display_name(user),
                           msg.chat_id, msg.message_thread_id)
        try:
            await msg.reply_text(f"✅ Принято! {apt} отмечена как убранная.")
        except Exception:  # noqa: BLE001
            pass
        return

    # number not known yet: hold the media and ask for it
    if len(_pending_media) > 200:
        _pending_media.clear()
    _pending_media[uid] = (kind, file_id, now_ts)
    try:
        await msg.reply_text(
            "Принял! Теперь напишите номер квартиры, например: Б-051"
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
    pend = _pending_media.get(uid)
    waiting = bool(pend) and now_ts - pend[2] < _REPORT_TTL

    if not waiting and not private:
        return  # ordinary group chatter — not our business

    apt = _match_apartment(msg.text, allow_bare=waiting)
    if not apt:
        if waiting and private:
            try:
                await msg.reply_text("Не понял номер квартиры. Напишите, например: Б-051")
            except Exception:  # noqa: BLE001
                pass
        return
    _register(user)
    if waiting:
        _pending_media.pop(uid, None)
        kind, file_id, _ts = pend
        await _send_report(context, apt, kind, file_id, _display_name(user),
                           msg.chat_id, msg.message_thread_id)
        try:
            await msg.reply_text(f"✅ Принято! {apt} отмечена как убранная.")
        except Exception:  # noqa: BLE001
            pass
    elif private:
        # number first, media to follow (private chat only)
        if len(_pending_apt) > 200:
            _pending_apt.clear()
        _pending_apt[uid] = (apt, now_ts)
        try:
            await msg.reply_text(f"Записал: {apt}. Теперь отправьте кружок, видео или фото уборки 📸")
        except Exception:  # noqa: BLE001
            pass


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
    post_date = (msg.date or datetime.datetime.now()).date()
    try:
        parsed = pay_parse.parse_payment(text, post_date, _apartment_names())
        parsed["_raw"] = text
        bookings = await asyncio.to_thread(database.all_active_bookings)
        bid, score = pay_parse.match_booking(parsed, bookings)
        await asyncio.to_thread(
            database.upsert_channel_payment,
            msg.chat_id, msg.message_id,
            (msg.date or datetime.datetime.now()).isoformat(timespec="minutes"),
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
    await update.message.reply_text(
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
    """ATTEND_DEADLINE (12:30): post the roll call to the team group."""
    staff = _staff_roster()
    if not staff:
        return
    today = datetime.date.today().isoformat()
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


async def cleaning_watch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """CLEANING_CHECK (18:00): which of today's checkouts have no cleaning
    report yet — so nothing is forgotten by the evening."""
    today = datetime.date.today()
    try:
        cleanings = (await asyncio.to_thread(services.build_cleaning, today, 1))["cleanings"]
    except Exception:  # noqa: BLE001
        logger.exception("cleaning_watch failed")
        return
    todays = [c for c in cleanings if c.get("cleaning_date") == today.isoformat()]
    if not todays:
        return  # no checkouts today — nothing to control
    not_done = [c["apartment"] for c in todays if c.get("status") != "done"]
    done = [c["apartment"] for c in todays if c.get("status") == "done"]
    ch, cm = config.CLEANING_CHECK_T
    lines = [f"🧹 Контроль уборок на {ch:02d}:{cm:02d}:"]
    if not_done:
        lines.append("❌ Нет отчёта об уборке: " + ", ".join(sorted(not_done)))
        lines.append("Горничные, пришлите фото/кружок с номером квартиры!")
    if done:
        lines.append("✅ Убрано: " + ", ".join(sorted(done)))
    if not not_done:
        lines.append("Все уборки закрыты, молодцы 💪")
    notify.send("\n".join(lines), topic="cleaning")


async def attendance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await asyncio.to_thread(database.attendance_for, datetime.date.today().isoformat())
    if not rows:
        await update.message.reply_text("Сегодня ещё никто не отметился на работе.")
        return
    lines = ["📋 Приходы сегодня:"]
    for r in rows:
        t = (r.get("arrived_at") or "")[11:16]
        st = "вовремя ✅" if r.get("on_time") else f"опоздание {r.get('late_minutes')} мин ⚠️"
        lines.append(f"• {r.get('staff_name')} — {t} ({st})")
    await update.message.reply_text("\n".join(lines))


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


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Add it to .env (see .env.example).")

    database.init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()
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
    # Telegram only detects latin /commands, so accept a typed "/цены" too.
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^/?цены\b"), prices_cmd))
    # live location: filters.LOCATION already matches both the initial share and
    # the live-location edits, so a single handler covers all points.
    # payments channel reader — must be registered before the media handlers
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, on_channel_payment))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    # cleaning reports: media with caption, or кружок/фото + номер отдельным сообщением
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_report))

    # Daily summary at 08:00 + Booking.com price report at 09:00 (server local time).
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

    logger.info("Bot started (demo_mode=%s)", config.DEMO_MODE)
    # ALL_TYPES guards against a token whose allowed_updates was ever narrowed by
    # a previous webhook, which would silently drop edited_message (live) updates.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
