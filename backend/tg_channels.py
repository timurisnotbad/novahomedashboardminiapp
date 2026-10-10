"""Telegram as a guest channel for the inbox — two ways:

* «tg» — the company's own Telegram account (what Wazzup does): log in once
  from CRM → Каналы with the phone number + code, then every private chat of
  that account appears in «Чаты» and the team answers from there. Uses
  Telethon (MTProto); needs TG_API_ID / TG_API_HASH from my.telegram.org.
  The client lives in the server's asyncio loop; sync callers hop into it.

* «tgbot» — a separate bot guests write to (TG_GUEST_BOT_TOKEN). Plain Bot
  API long-polling in a thread; no limits, no login, but the guest must start
  the conversation.
"""
import asyncio
import logging
import mimetypes
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

from . import config, database, inbox

logger = logging.getLogger("nova.tg")

MAX_MEDIA = 30 * 1024 * 1024
SESSION = config.DATA_DIR / "tg_user"


# ---------------------------------------------------------------------------
# Own account (Telethon)
# ---------------------------------------------------------------------------
_client = None
_loop: asyncio.AbstractEventLoop | None = None
_login: dict = {}  # phone, phone_code_hash while a login is in progress
_me: dict | None = None
_error: str | None = None


def user_configured() -> bool:
    return bool(config.TG_API_ID and config.TG_API_HASH)


def _run(coro, timeout: float = 60):
    """Run a coroutine on the server loop from a worker thread."""
    if _loop is None:
        raise inbox.BridgeError("Telegram-клиент ещё не запущен")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


async def start() -> None:
    """Called from the FastAPI lifespan. Never blocks startup: the connection
    runs in the background and retries every minute while Telegram is
    unreachable (no internet, blocked network)."""
    global _client, _loop, _error
    if not user_configured():
        return
    try:
        from telethon import TelegramClient
    except ImportError:
        _error = "Библиотека telethon не установлена — запустите install.bat"
        logger.warning(_error)
        return
    _loop = asyncio.get_event_loop()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _client = TelegramClient(str(SESSION), config.TG_API_ID, config.TG_API_HASH, connection_retries=2, retry_delay=2,
                             device_model="Nova Home CRM", system_version="1.0", app_version=config.APP_VERSION)
    _loop.create_task(_connect_loop())


async def _connect_loop() -> None:
    global _error
    while True:
        try:
            if not _client.is_connected():
                await asyncio.wait_for(_client.connect(), 25)
            if not _me and await _client.is_user_authorized():
                await _after_login()
            _error = None
            return
        except asyncio.TimeoutError:
            _error = "Telegram не отвечает (нет доступа к серверам Telegram?) — пробую снова"
        except Exception as exc:  # noqa: BLE001
            _error = f"Telegram: {exc}"
            logger.warning("telegram connect failed: %s", exc)
        await asyncio.sleep(60)


async def _after_login() -> None:
    global _me, _error
    from telethon import events
    me = await _client.get_me()
    _me = {"id": me.id, "name": " ".join(x for x in (me.first_name, me.last_name) if x), "phone": me.phone,
           "username": me.username}
    _error = None
    _client.remove_event_handler(_on_message)
    _client.add_event_handler(_on_message, events.NewMessage())
    _client.remove_event_handler(_on_raw)
    _client.add_event_handler(_on_raw, events.Raw())
    logger.info("telegram account connected: %s", _me)


async def _on_raw(update) -> None:
    """Read receipts: Telegram tells us the peer read our messages up to max_id
    (UpdateReadHistoryOutbox) — the same «two ticks» as WhatsApp."""
    try:
        from telethon.tl import types as t
        if isinstance(update, t.UpdateReadHistoryOutbox) and isinstance(update.peer, t.PeerUser):
            await asyncio.get_event_loop().run_in_executor(None, _mark_read, update.peer.user_id, update.max_id)
        elif isinstance(update, t.UpdateReadHistoryInbox) and isinstance(update.peer, t.PeerUser):
            # this account read the guest's messages somewhere else (phone, Telegram
            # Desktop): the guest already sees two ticks — record who did it
            await asyncio.get_event_loop().run_in_executor(None, inbox.receipt_external, f"tg:{update.peer.user_id}", "Telegram на телефоне/компьютере")
    except Exception:  # noqa: BLE001
        logger.exception("telegram read receipt failed")


def _mark_read(uid: int, max_id: int) -> None:
    jid = f"tg:{uid}"
    with database.get_conn() as conn:
        rows = conn.execute("SELECT m.wa_id FROM inbox_messages m JOIN inbox_chats c ON c.id = m.chat_id "
                            "WHERE c.jid = ? AND m.direction = 'out' AND m.status != 'read' AND m.wa_id LIKE ?",
                            (jid, f"tg:{uid}:%",)).fetchall()
    for r in rows:
        try:
            if int(r["wa_id"].rsplit(":", 1)[1]) <= max_id:
                inbox.store_ack(r["wa_id"], "read")
        except (ValueError, IndexError):
            continue


async def _on_message(event) -> None:
    try:
        if not event.is_private:
            return
        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False) and not event.out:
            return
        m = await _message_dict(event.message, sender)
        if m:
            await asyncio.get_event_loop().run_in_executor(None, inbox.store_message, m, True)
    except Exception:  # noqa: BLE001
        logger.exception("telegram message failed")


async def _message_dict(msg, peer) -> dict | None:
    from telethon.tl import types as t
    out = bool(msg.out)
    other_id = (await _client.get_peer_id(msg.peer_id)) if out else (peer.id if peer else None)
    if not other_id:
        return None
    other = peer if (peer and peer.id == other_id) else await _client.get_entity(other_id)
    name = " ".join(x for x in (getattr(other, "first_name", None), getattr(other, "last_name", None)) if x) \
        or (("@" + other.username) if getattr(other, "username", None) else None)
    phone = re.sub(r"\D", "", getattr(other, "phone", "") or "")
    m = {"channel": "tg", "id": f"tg:{other_id}:{msg.id}", "jid": f"tg:{other_id}", "phone": phone, "from_me": out,
         "push_name": None if out else name, "at": (msg.date or datetime.now(timezone.utc)).isoformat(),
         "kind": "text", "text": msg.message or ""}
    if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
        m["quoted_id"] = f"tg:{other_id}:{msg.reply_to.reply_to_msg_id}"
    media = msg.media
    if media:
        kind, mime, fname, voice = "document", None, None, False
        if isinstance(media, t.MessageMediaPhoto):
            kind, mime = "image", "image/jpeg"
        elif isinstance(media, t.MessageMediaDocument) and media.document:
            doc = media.document
            mime = doc.mime_type
            for a in doc.attributes:
                if isinstance(a, t.DocumentAttributeFilename):
                    fname = a.file_name
                if isinstance(a, t.DocumentAttributeAudio):
                    kind, voice = "audio", bool(a.voice)
                if isinstance(a, t.DocumentAttributeVideo):
                    kind = "video"
                if isinstance(a, t.DocumentAttributeSticker):
                    kind = "sticker"
            if kind == "document" and (mime or "").startswith("image/"):
                kind = "image"
            if doc.size and doc.size > MAX_MEDIA:
                m.update(kind=kind, text=(msg.message or "") + " (файл больше 30 МБ)")
                return m
        elif isinstance(media, t.MessageMediaGeo) and media.geo:
            m.update(kind="location", lat=media.geo.lat, lng=media.geo.long)
            return m
        elif isinstance(media, t.MessageMediaContact):
            m.update(kind="contact", text=" ".join(x for x in (media.first_name, media.last_name) if x) + f" {media.phone_number}")
            return m
        else:
            m["kind"] = "other"
            return m
        m.update(kind=kind, mime=mime, file_name=fname, voice=voice)
        try:
            data = await _client.download_media(msg, file=bytes)
            if data:
                ext = (fname.rsplit(".", 1)[-1] if fname and "." in fname else (mimetypes.guess_extension(mime or "") or ".bin").lstrip("."))
                ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8] or "bin"
                name_ = f"t{uuid.uuid4().hex}.{ext}"
                config.INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
                (config.INBOX_MEDIA_DIR / name_).write_bytes(data)
                m["media"] = name_
        except Exception:  # noqa: BLE001
            logger.exception("telegram media download failed")
    return m


def user_status() -> dict:
    if not user_configured():
        return {"configured": False}
    return {"configured": True, "authorized": bool(_me), "me": _me, "pending_phone": _login.get("phone"),
            "password_needed": _login.get("password_needed", False), "error": _error}


def _err(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "Telegram не отвечает — проверьте интернет (серверы Telegram должны быть доступны с этого компьютера)"
    return f"Telegram: {exc}" if str(exc) else f"Telegram: {type(exc).__name__}"


def user_send_code(phone: str) -> dict:
    phone = "+" + re.sub(r"\D", "", phone)
    if len(phone) < 8:
        raise inbox.BridgeError("Введите номер с кодом страны")
    if _client is None:
        raise inbox.BridgeError(_error or "Telegram-клиент не запущен (TG_API_ID / TG_API_HASH)")

    async def go():
        if not _client.is_connected():
            await asyncio.wait_for(_client.connect(), 25)
        res = await _client.send_code_request(phone)
        _login.clear()
        _login.update(phone=phone, phone_code_hash=res.phone_code_hash)
        return {"ok": True, "phone": phone}
    try:
        return _run(go(), 40)
    except inbox.BridgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise inbox.BridgeError(_err(exc)) from exc


def user_sign_in(code: str = "", password: str = "") -> dict:
    if _client is None or not _login.get("phone"):
        raise inbox.BridgeError("Сначала запросите код")

    async def go():
        from telethon.errors import SessionPasswordNeededError
        try:
            if password:
                await _client.sign_in(password=password)
            else:
                await _client.sign_in(_login["phone"], re.sub(r"\D", "", code), phone_code_hash=_login["phone_code_hash"])
        except SessionPasswordNeededError:
            _login["password_needed"] = True
            return {"password_needed": True}
        _login.clear()
        await _after_login()
        return {"ok": True, "me": _me}
    try:
        return _run(go(), 40)
    except inbox.BridgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise inbox.BridgeError(_err(exc)) from exc


def user_logout() -> None:
    global _me
    if _client is None:
        return

    async def go():
        try:
            await _client.log_out()
        except Exception:  # noqa: BLE001
            pass
    try:
        _run(go(), 20)
    finally:
        _me = None
        _login.clear()
        for p in (SESSION.with_suffix(".session"), SESSION.with_suffix(".session-journal")):
            try:
                p.unlink()
            except OSError:
                pass


def user_send(payload: dict) -> dict:
    if not _me:
        raise inbox.BridgeError("Telegram-аккаунт не подключён — войдите в CRM → Каналы")
    uid = int(payload["jid"].split(":")[1])
    reply_to = None
    q = payload.get("quoted_id") or ""
    if q.startswith("tg:") and q.count(":") == 2:
        reply_to = int(q.rsplit(":", 1)[1])

    async def go():
        if payload.get("media"):
            path = config.INBOX_MEDIA_DIR / payload["media"]
            mime = payload.get("mime") or ""
            attrs = {}
            if payload.get("file_name"):
                from telethon.tl.types import DocumentAttributeFilename
                attrs["attributes"] = [DocumentAttributeFilename(payload["file_name"])]
            msg = await _client.send_file(uid, str(path), caption=payload.get("text") or None, reply_to=reply_to,
                                          voice_note=mime in ("audio/ogg", "audio/opus"),
                                          force_document=not (mime.startswith("image/") or mime.startswith("video/")), **attrs)
        else:
            msg = await _client.send_message(uid, payload.get("text") or "", reply_to=reply_to)
        return {"id": f"tg:{uid}:{msg.id}"}
    try:
        return _run(go(), 90)
    except inbox.BridgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise inbox.BridgeError(_err(exc)) from exc


def user_lookup(phone: str) -> dict:
    """Is this number on Telegram? Imports the contact for a moment (the only
    way Telegram offers), returns {exists, id, name, phone}."""
    if not _me:
        raise inbox.BridgeError("Telegram-аккаунт не подключён — войдите в CRM → Каналы")
    d = re.sub(r"\D", "", phone or "")

    async def go():
        from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
        from telethon.tl.types import InputPhoneContact
        res = await _client(ImportContactsRequest([InputPhoneContact(client_id=0, phone="+" + d, first_name=d, last_name="")]))
        users = list(res.users or [])
        if not users:
            return {"exists": False}
        u = users[0]
        try:
            await _client(DeleteContactsRequest(id=[u]))  # we only wanted to know; don't keep the contact
        except Exception:  # noqa: BLE001
            pass
        name = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x and x != d) \
            or (("@" + u.username) if getattr(u, "username", None) else "")
        return {"exists": True, "id": u.id, "name": name, "phone": re.sub(r"\D", "", getattr(u, "phone", "") or "") or d}
    try:
        return _run(go(), 40)
    except inbox.BridgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise inbox.BridgeError(_err(exc)) from exc


def user_read(jid: str) -> None:
    if not _me:
        return
    uid = int(jid.split(":")[1])

    async def go():
        await _client.send_read_acknowledge(uid)
    _run(go(), 20)


# ---------------------------------------------------------------------------
# Guest bot (Bot API, long polling in a thread)
# ---------------------------------------------------------------------------
import os as _os
_BOT_API = _os.environ.get("TG_BOT_API_URL", "https://api.telegram.org").rstrip("/")  # overridable for tests
_bot_thread: threading.Thread | None = None
_bot_me: dict | None = None
_bot_error: str | None = None


def bot_configured() -> bool:
    return bool(config.TG_GUEST_BOT_TOKEN)


def _bot_api(method: str, timeout: int = 35, **kw) -> dict:
    try:
        r = requests.post(f"{_BOT_API}/bot{config.TG_GUEST_BOT_TOKEN}/{method}", timeout=timeout, **kw)
        d = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise inbox.BridgeError(f"Telegram Bot API: {exc}") from exc
    if not d.get("ok"):
        raise inbox.BridgeError(d.get("description") or "Telegram Bot API error")
    return d.get("result")


def bot_start() -> None:
    global _bot_thread, _bot_error
    if not bot_configured() or _bot_thread:
        return
    if config.TG_GUEST_BOT_TOKEN == config.BOT_TOKEN:
        _bot_error = "TG_GUEST_BOT_TOKEN совпадает с BOT_TOKEN — для гостей нужен отдельный бот"
        logger.error(_bot_error)
        return
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True, name="tg-guest-bot")
    _bot_thread.start()


def _bot_loop() -> None:
    global _bot_me, _bot_error
    offset = None
    while True:
        try:
            if _bot_me is None:
                me = _bot_api("getMe", timeout=15) or {}
                _bot_me = {"username": me.get("username"), "name": me.get("first_name")}
                _bot_error = None
            updates = _bot_api("getUpdates", json={"timeout": 30, "offset": offset, "allowed_updates": ["message"]})
            for u in updates or []:
                offset = u["update_id"] + 1
                msg = u.get("message")
                if msg and (msg.get("chat") or {}).get("type") == "private":
                    try:
                        m = _bot_message(msg)
                        if m:
                            inbox.store_message(m, notify=True)
                    except Exception:  # noqa: BLE001
                        logger.exception("guest bot message failed")
        except inbox.BridgeError as exc:
            _bot_error = str(exc)
            logger.warning("guest bot: %s", exc)
            time.sleep(10)
        except Exception:  # noqa: BLE001
            logger.exception("guest bot loop")
            time.sleep(10)


def _bot_download(file_id: str, size: int | None) -> tuple[bytes, str] | None:
    if size and size > MAX_MEDIA:
        return None
    f = _bot_api("getFile", json={"file_id": file_id}, timeout=15)
    path = f.get("file_path")
    if not path:
        return None
    r = requests.get(f"{_BOT_API}/file/bot{config.TG_GUEST_BOT_TOKEN}/{path}", timeout=60)
    return (r.content, path.rsplit(".", 1)[-1] if "." in path else "bin") if r.status_code == 200 else None


def _bot_message(msg: dict) -> dict | None:
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    cid = chat.get("id")
    if not cid:
        return None
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or (("@" + frm["username"]) if frm.get("username") else None)
    m = {"channel": "tgbot", "id": f"tgbot:{cid}:{msg.get('message_id')}", "jid": f"tgbot:{cid}", "phone": "",
         "from_me": False, "push_name": name, "at": datetime.fromtimestamp(msg.get("date") or time.time(), tz=timezone.utc).isoformat(),
         "kind": "text", "text": msg.get("text") or msg.get("caption") or ""}
    if (msg.get("reply_to_message") or {}).get("message_id"):
        m["quoted_id"] = f"tgbot:{cid}:{msg['reply_to_message']['message_id']}"
    if msg.get("text") == "/start":
        m["text"] = "Гость открыл бота"
    media = None
    if msg.get("photo"):
        p = msg["photo"][-1]
        media, kind, mime = (p["file_id"], p.get("file_size")), "image", "image/jpeg"
    elif msg.get("voice"):
        media, kind, mime = (msg["voice"]["file_id"], msg["voice"].get("file_size")), "audio", msg["voice"].get("mime_type") or "audio/ogg"
        m["voice"] = True
    elif msg.get("audio"):
        media, kind, mime = (msg["audio"]["file_id"], msg["audio"].get("file_size")), "audio", msg["audio"].get("mime_type")
    elif msg.get("video") or msg.get("video_note"):
        v = msg.get("video") or msg.get("video_note")
        media, kind, mime = (v["file_id"], v.get("file_size")), "video", v.get("mime_type") or "video/mp4"
    elif msg.get("document"):
        d = msg["document"]
        media, kind, mime = (d["file_id"], d.get("file_size")), "document", d.get("mime_type")
        m["file_name"] = d.get("file_name")
    elif msg.get("sticker"):
        s = msg["sticker"]
        media, kind, mime = (s["file_id"], s.get("file_size")), "sticker", "image/webp"
    elif msg.get("location"):
        m.update(kind="location", lat=msg["location"]["latitude"], lng=msg["location"]["longitude"])
    elif msg.get("contact"):
        c = msg["contact"]
        m.update(kind="contact", text=f"{c.get('first_name', '')} {c.get('phone_number', '')}".strip())
    if media:
        m.update(kind=kind, mime=mime)
        try:
            got = _bot_download(*media)
            if got:
                ext = re.sub(r"[^A-Za-z0-9]", "", got[1])[:8] or "bin"
                name_ = f"b{uuid.uuid4().hex}.{ext}"
                config.INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
                (config.INBOX_MEDIA_DIR / name_).write_bytes(got[0])
                m["media"] = name_
        except Exception:  # noqa: BLE001
            logger.exception("guest bot media failed")
    return m


def bot_send(payload: dict) -> dict:
    if not bot_configured():
        raise inbox.BridgeError("Telegram-бот для гостей не настроен (TG_GUEST_BOT_TOKEN)")
    cid = int(payload["jid"].split(":")[1])
    reply = None
    q = payload.get("quoted_id") or ""
    if q.startswith("tgbot:") and q.count(":") == 2:
        reply = int(q.rsplit(":", 1)[1])
    if payload.get("media"):
        path = config.INBOX_MEDIA_DIR / payload["media"]
        mime = payload.get("mime") or ""
        method, field = ("sendPhoto", "photo") if mime.startswith("image/") and mime != "image/gif" else \
            ("sendVideo", "video") if mime.startswith("video/") else \
            ("sendVoice", "voice") if mime in ("audio/ogg", "audio/opus") else \
            ("sendAudio", "audio") if mime.startswith("audio/") else ("sendDocument", "document")
        data = {"chat_id": cid}
        if payload.get("text"):
            data["caption"] = payload["text"]
        if reply:
            data["reply_to_message_id"] = reply
        with path.open("rb") as fh:
            res = _bot_api(method, data=data, files={field: (payload.get("file_name") or path.name, fh, mime or "application/octet-stream")}, timeout=90)
    else:
        body = {"chat_id": cid, "text": payload.get("text") or ""}
        if reply:
            body["reply_to_message_id"] = reply
        res = _bot_api("sendMessage", json=body, timeout=20)
    return {"id": f"tgbot:{cid}:{res.get('message_id')}"}


def bot_status() -> dict:
    if not bot_configured():
        return {"configured": False}
    return {"configured": True, "ok": _bot_me is not None and not _bot_error, "me": _bot_me, "error": _bot_error}
