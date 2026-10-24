"""The CRM notification bot (CRM_BOT_TOKEN): a second Telegram bot so that CRM
notifications do not mix with the dashboard bot. It only needs to do three
things: let a user link it (/link CODE from CRM → «Telegram-уведомления»),
deliver notifications (notify.send with its token), and pass a reply to a
«💬» alert on to the guest. It also hands out the chats / payments pages.
Long-polling thread inside the backend process — no extra window."""
import logging
import threading
import time

import requests

from . import config, database, inbox

logger = logging.getLogger(__name__)
_API = "https://api.telegram.org"
_thread = None
_me: dict | None = None
_error: str | None = None


def configured() -> bool:
    return bool(config.CRM_BOT_TOKEN) and config.CRM_BOT_TOKEN != config.BOT_TOKEN


def username() -> str:
    return (_me or {}).get("username") or ""


def error() -> str | None:
    return _error


def _api(method: str, timeout: int = 35, **kw):
    try:
        r = requests.post(f"{_API}/bot{config.CRM_BOT_TOKEN}/{method}", timeout=timeout, **kw)
        d = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(config.redact(str(exc))[:200]) from exc
    if not d.get("ok"):
        raise RuntimeError(str(d.get("description") or d)[:200])
    return d.get("result")


def _say(chat_id: int, text: str, reply_to: int | None = None, markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if markup:
        payload["reply_markup"] = markup
    try:
        _api("sendMessage", json=payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("crm bot say failed: %s", exc)


def _page_markup(uid: int, page: str, label: str) -> dict:
    base = config.WEBAPP_URL.split("?")[0].rstrip("/")
    url = f"{base}/{page}/?v={config.APP_VERSION}&k={inbox.personal_key(uid)}"
    return {"inline_keyboard": [[{"text": label, "web_app": {"url": url}}], [{"text": "🖥 Открыть в браузере", "url": url}]]}


def handle(msg: dict) -> str | None:
    """One private message; returns what was done (for tests)."""
    chat = msg.get("chat") or {}
    user = msg.get("from") or {}
    if chat.get("type") != "private" or not user.get("id"):
        return None
    uid = int(user["id"])
    cid = int(chat["id"])
    text = (msg.get("text") or "").strip()
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or str(uid)
    from . import crm
    low = text.lower()
    if low.startswith("/start") or low.startswith("/help"):
        linked = any(int(u.get("tg_id") or 0) == uid for u in crm.list_users())
        _say(cid, ("✅ Этот Telegram уже привязан к CRM: уведомления будут приходить сюда.\n" if linked else
                   "Это бот уведомлений Nova Home CRM.\nЧтобы привязать: в CRM откройте «Telegram-уведомления», скопируйте команду вида /link КОД и отправьте её сюда.\n")
              + "Ответ на уведомление о сообщении гостя уходит гостю. Команды: /chats — чаты, /оплата — оплаты.")
        return "start"
    if low.startswith("/link"):
        parts = text.split()
        u = crm.link_telegram(parts[1], uid, name) if len(parts) > 1 else None
        if u:
            _say(cid, f"✅ Готово, {u['name']}: уведомления CRM будут приходить сюда — сообщения гостей без ответа, задачи, брони, ассистент. "
                      "Какие именно — настраивается в CRM → «Telegram-уведомления». Ответьте на уведомление о сообщении — ответ уйдёт гостю.")
            return "linked"
        _say(cid, "Код не подошёл или устарел (действует 30 минут). Получите новый в CRM → «Telegram-уведомления» и отправьте /link КОД ещё раз.")
        return "link-failed"
    if low.startswith(("/chats", "/чаты", "/чат", "/оплат", "/cash", "/касса")):
        if not config.WEBAPP_URL:
            _say(cid, "WEBAPP_URL не задан в .env — страницы недоступны.")
            return "no-url"
        if not inbox.may_use(uid):
            _say(cid, "У вас нет доступа. Попросите владельца добавить вас в INBOX_USERS.")
            return "denied"
        inbox.remember_agent(uid, name, user.get("username") or "")
        pay = low.startswith(("/оплат", "/cash", "/касса"))
        _say(cid, "💵 Оплаты" if pay else "💬 Чаты", markup=_page_markup(uid, "cash" if pay else "inbox", "💵 Записать оплату" if pay else "💬 Открыть чаты"))
        return "page"
    reply = msg.get("reply_to_message") or {}
    if reply.get("message_id") and text:
        chat_id = inbox.chat_for_alert(cid, int(reply["message_id"]))
        if chat_id:
            if not inbox.may_use(uid):
                _say(cid, "У вас нет доступа к чатам.", msg.get("message_id"))
                return "denied"
            inbox.remember_agent(uid, name, user.get("username") or "")
            try:
                inbox.send(chat_id, inbox._agent_name(uid), text)  # noqa: SLF001
                try:
                    _api("setMessageReaction", json={"chat_id": cid, "message_id": msg.get("message_id"), "reaction": [{"type": "emoji", "emoji": "👍"}]})
                except Exception:  # noqa: BLE001
                    _say(cid, "✅ Отправлено гостю", msg.get("message_id"))
                return "replied"
            except Exception as exc:  # noqa: BLE001
                _say(cid, f"❌ Не отправлено: {config.redact(str(exc))[:200]}", msg.get("message_id"))
                return "reply-failed"
    if text and not text.startswith("/"):
        _say(cid, "Я только присылаю уведомления CRM. Чтобы написать гостю, ответьте на его уведомление; чаты целиком — /chats.")
        return "hint"
    return None


def _loop() -> None:
    global _me, _error
    offset = None
    while True:
        try:
            if _me is None:
                me = _api("getMe", timeout=15) or {}
                _me = {"username": me.get("username"), "name": me.get("first_name")}
                _error = None
                logger.info("CRM bot @%s is polling", _me["username"])
                try:
                    _api("setMyCommands", json={"commands": [{"command": "start", "description": "Что умеет бот"}, {"command": "link", "description": "Привязать к CRM: /link КОД"},
                                                             {"command": "chats", "description": "Чаты с гостями"}, {"command": "cash", "description": "Записать оплату"}]})
                except Exception:  # noqa: BLE001
                    pass
            updates = _api("getUpdates", json={"timeout": 30, "offset": offset, "allowed_updates": ["message"]})
            for u in updates or []:
                offset = u["update_id"] + 1
                msg = u.get("message")
                if msg:
                    try:
                        handle(msg)
                    except Exception:  # noqa: BLE001
                        logger.exception("crm bot update failed")
        except Exception as exc:  # noqa: BLE001
            _error = str(exc)[:200]
            logger.warning("CRM bot: %s", _error)
            time.sleep(10)


def start() -> None:
    global _thread, _error
    if _thread:
        return
    if config.CRM_BOT_TOKEN and config.CRM_BOT_TOKEN == config.BOT_TOKEN:
        _error = "CRM_BOT_TOKEN совпадает с BOT_TOKEN — для уведомлений CRM нужен отдельный бот"
        logger.error(_error)
        return
    if not configured():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="crm-bot")
    _thread.start()
