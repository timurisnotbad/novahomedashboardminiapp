"""Wazzup (wazzup24.ru) as an inbox channel — the paid WhatsApp connection the
company already uses. Wazzup keeps the WhatsApp session; we talk to its
API v3: it POSTs incoming messages and delivery statuses to our webhook, we
send replies with POST /v3/message.

Needs WAZZUP_API_KEY in .env (Wazzup → Интеграция с CRM → Ключ API). The
webhook is registered automatically at startup when WEBAPP_URL is https:
    {WEBAPP_URL}/api/inbox/wazzup/webhook/{token}
(the token in the path is derived from INBOX_SECRET — Wazzup does not sign
its webhooks, so the secret path is what keeps strangers out).

Wazzup channels can be WhatsApp, Instagram or Telegram; a WhatsApp chat
is keyed by phone, so it is the SAME chat as the QR bridge / Cloud API one.
"""
import hashlib
import logging
import mimetypes
import re
import threading
import time
import uuid

import requests

from . import config, database, inbox

logger = logging.getLogger("nova.wazzup")

import os as _os
API = _os.environ.get("WAZZUP_API_URL", "https://api.wazzup24.ru/v3").rstrip("/")  # overridable for tests
MAX_MEDIA = 30 * 1024 * 1024
# chatType in Wazzup → our channel code
CHANNEL_BY_TYPE = {"whatsapp": "wz", "whatsgroup": "wz", "instagram": "wzig", "telegram": "wztg", "tgapi": "wztg", "viber": "wz"}
TYPE_BY_CHANNEL = {"wz": "whatsapp", "wzig": "instagram", "wztg": "telegram"}

SCHEMA = """
-- which Wazzup channel/chat id a chat belongs to (needed to send)
CREATE TABLE IF NOT EXISTS wazzup_chats (
    chat_id INTEGER PRIMARY KEY,
    channel_id TEXT,
    chat_type TEXT,
    ext_chat_id TEXT,
    updated_at TEXT
);
"""

_state: dict = {"webhook_ok": None, "webhook_error": None, "channels": [], "checked_at": 0.0}


def configured() -> bool:
    return bool(config.WAZZUP_API_KEY)


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)


def webhook_token() -> str:
    return hashlib.sha256(("wazzup:" + config.INBOX_SECRET).encode()).hexdigest()[:24]


def webhook_url() -> str:
    base = (config.WEBAPP_URL or "").split("?")[0].rstrip("/")
    return f"{base}{config.API_PREFIX}/inbox/wazzup/webhook/{webhook_token()}" if base else ""


def _api(method: str, path: str, timeout: int = 20, **kw):
    try:
        resp = requests.request(method, API + path, timeout=timeout,
                                headers={"Authorization": "Bearer " + config.WAZZUP_API_KEY, "Content-Type": "application/json"}, **kw)
    except requests.RequestException as exc:
        raise inbox.BridgeError(f"Wazzup недоступен: {exc}") from exc
    try:
        data = resp.json() if resp.text else {}
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        msg = (err or {}).get("description") if isinstance(err, dict) else (err or data.get("message") if isinstance(data, dict) else None)
        if resp.status_code == 401:
            msg = "неверный ключ API (WAZZUP_API_KEY)"
        raise inbox.BridgeError(f"Wazzup: {msg or 'HTTP ' + str(resp.status_code)}")
    return data


# ---------------------------------------------------------------------------
# Setup & status
# ---------------------------------------------------------------------------
def channels(force: bool = False) -> list[dict]:
    if not configured():
        return []
    if not force and time.time() - _state["checked_at"] < 60:
        return _state["channels"]
    data = _api("GET", "/channels")
    chans = data if isinstance(data, list) else data.get("channels") or data.get("data") or []
    _state["channels"] = [{"channelId": c.get("channelId"), "transport": c.get("transport"), "plainId": c.get("plainId"),
                           "state": c.get("state"), "name": c.get("name")} for c in chans]
    _state["checked_at"] = time.time()
    return _state["channels"]


def default_channel(chat_type: str = "whatsapp") -> str | None:
    wanted = {"whatsapp": ("whatsapp", "wapi"), "instagram": ("instagram",), "telegram": ("telegram", "tgapi")}.get(chat_type, (chat_type,))
    if chat_type == "whatsapp" and config.WAZZUP_CHANNEL_ID:
        return config.WAZZUP_CHANNEL_ID
    for c in channels():
        if c.get("transport") in wanted and (c.get("state") in (None, "active")):
            return c["channelId"]
    for c in channels():
        if c.get("transport") in wanted:
            return c["channelId"]
    return None


def register_webhook() -> dict:
    """PATCH /v3/webhooks: Wazzup tests the URL with a POST and expects 200."""
    url = webhook_url()
    if not url.startswith("https://"):
        raise inbox.BridgeError("Для вебхука Wazzup нужен публичный https-адрес (WEBAPP_URL)")
    _api("PATCH", "/webhooks", json={"webhooksUri": url,
                                      "subscriptions": {"messagesAndStatuses": True, "contactsAndDealsCreation": False, "channelsUpdates": True}})
    _state["webhook_ok"] = True
    _state["webhook_error"] = None
    return {"ok": True, "url": url}


def start() -> None:
    """At server startup: check the key, register the webhook — in a thread,
    never blocking startup."""
    if not configured():
        return
    init_db()

    def run():
        try:
            channels(force=True)
            register_webhook()
            logger.info("wazzup: webhook registered, channels: %s", [(c["transport"], c["state"]) for c in _state["channels"]])
        except inbox.BridgeError as exc:
            _state["webhook_ok"] = False
            _state["webhook_error"] = str(exc)
            logger.warning("wazzup setup: %s", exc)
    threading.Thread(target=run, daemon=True).start()


def status() -> dict:
    if not configured():
        return {"configured": False}
    out = {"configured": True, "webhook_url": webhook_url(), "webhook_ok": _state["webhook_ok"], "webhook_error": _state["webhook_error"]}
    try:
        out["channels"] = channels()
        out["ok"] = True
    except inbox.BridgeError as exc:
        out["ok"] = False
        out["error"] = str(exc)
        out["channels"] = []
    with database.get_conn() as conn:
        out["chats"] = conn.execute("SELECT COUNT(*) FROM inbox_chats WHERE channel IN ('wz', 'wzig', 'wztg')").fetchone()[0]
    return out


# ---------------------------------------------------------------------------
# Incoming webhook
# ---------------------------------------------------------------------------
def _jid(chat_type: str, chat_id: str) -> tuple[str, str]:
    """(jid, phone) for a Wazzup chat."""
    if chat_type in ("whatsapp", "viber"):
        digits = re.sub(r"\D", "", chat_id or "")
        return f"{digits}@s.whatsapp.net", digits
    if chat_type == "whatsgroup":
        return f"wzgroup:{chat_id}", ""
    if chat_type == "instagram":
        return f"wzig:{chat_id}", ""
    return f"wztg:{chat_id}", ""


def _save_media(url: str) -> tuple[str | None, str | None]:
    try:
        r = requests.get(url, timeout=60, stream=True)
        content = r.raw.read(MAX_MEDIA + 1)
        if r.status_code != 200 or len(content) > MAX_MEDIA:
            return None, None
        mime = (r.headers.get("Content-Type") or "").split(";")[0] or mimetypes.guess_type(url.split("?")[0])[0] or "application/octet-stream"
        ext = (mimetypes.guess_extension(mime) or "." + (url.split("?")[0].rsplit(".", 1)[-1] if "." in url.split("?")[0].rsplit("/", 1)[-1] else "bin")).lstrip(".")
        ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8] or "bin"
        name = f"w{uuid.uuid4().hex}.{ext}"
        config.INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        (config.INBOX_MEDIA_DIR / name).write_bytes(content)
        return name, mime
    except Exception:  # noqa: BLE001
        logger.exception("wazzup media download failed")
        return None, None


def _message(m: dict) -> dict | None:
    chat_type = m.get("chatType") or "whatsapp"
    ext_chat = str(m.get("chatId") or "")
    if not ext_chat or not m.get("messageId"):
        return None
    jid, phone = _jid(chat_type, ext_chat)
    contact = m.get("contact") or {}
    kind_map = {"text": "text", "image": "image", "video": "video", "audio": "audio", "document": "document", "vcard": "contact",
                "geo": "location", "sticker": "sticker", "wapi_template": "text", "unsupported": "other"}
    out = {"channel": CHANNEL_BY_TYPE.get(chat_type, "wz"), "id": "wz:" + m["messageId"], "jid": jid, "phone": phone,
           "from_me": bool(m.get("isEcho")), "push_name": None if m.get("isEcho") else (contact.get("name") or contact.get("username")),
           "at": m.get("dateTime") or "", "kind": kind_map.get(m.get("type"), "other"), "text": m.get("text") or "",
           "wz": {"channel_id": m.get("channelId"), "chat_type": chat_type, "ext_chat_id": ext_chat}}
    q = m.get("quotedMessage") or {}
    if q.get("messageId"):
        out["quoted_id"] = "wz:" + q["messageId"]
    if m.get("type") == "geo" and m.get("geo"):
        out.update(lat=(m["geo"] or {}).get("latitude"), lng=(m["geo"] or {}).get("longitude"))
    if m.get("contentUri") and out["kind"] in ("image", "video", "audio", "document", "sticker"):
        name, mime = _save_media(m["contentUri"])
        if name:
            out["media"], out["mime"] = name, mime
        out["file_name"] = (m.get("contentUri") or "").split("?")[0].rsplit("/", 1)[-1] if out["kind"] == "document" else None
    if m.get("status") == "error" and m.get("isEcho"):
        out["error"] = (m.get("error") or {}).get("description")
    return out


def _remember(chat_id: int, wz: dict) -> None:
    with database.get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO wazzup_chats (chat_id, channel_id, chat_type, ext_chat_id, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (chat_id, wz.get("channel_id"), wz.get("chat_type"), wz.get("ext_chat_id"), inbox._now()))  # noqa: SLF001


STATUS = {"sent": "sent", "delivered": "delivered", "read": "read", "error": "failed", "inbound": None}


def handle_webhook(data: dict) -> None:
    threading.Thread(target=_handle, args=(data,), daemon=True).start()


def _handle(data: dict) -> None:
    for m in data.get("messages") or []:
        try:
            msg = _message(m)
            if not msg:
                continue
            wz = msg.pop("wz")
            res = inbox.store_message(msg, notify=True)
            chat_id = (res or {}).get("chat_id")
            if chat_id is None:
                with database.get_conn() as conn:
                    r = conn.execute("SELECT id FROM inbox_chats WHERE jid = ?", (msg["jid"],)).fetchone()
                    chat_id = r["id"] if r else None
            if chat_id:
                _remember(chat_id, wz)
            st = STATUS.get(m.get("status"))
            if st and m.get("isEcho"):
                inbox.store_ack(msg["id"], st)
        except Exception:  # noqa: BLE001
            logger.exception("wazzup message failed")
    for s in data.get("statuses") or []:
        st = STATUS.get(s.get("status"))
        if st and s.get("messageId"):
            inbox.store_ack("wz:" + s["messageId"], st)
    if data.get("channelsUpdates"):
        _state["checked_at"] = 0.0


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send(payload: dict) -> dict:
    if not configured():
        raise inbox.BridgeError("Wazzup не настроен (WAZZUP_API_KEY)")
    jid = payload["jid"]
    with database.get_conn() as conn:
        chat = conn.execute("SELECT id FROM inbox_chats WHERE jid = ?", (jid,)).fetchone()
        wz = conn.execute("SELECT * FROM wazzup_chats WHERE chat_id = ?", (chat["id"],)).fetchone() if chat else None
    if wz:
        channel_id, chat_type, ext = wz["channel_id"], wz["chat_type"], wz["ext_chat_id"]
    else:
        # a WhatsApp chat that came through another transport (QR bridge / Cloud): by phone
        phone = inbox.phone_of_jid(jid)
        if not phone:
            raise inbox.BridgeError("Для этого чата нет Wazzup-канала")
        chat_type, ext = "whatsapp", phone
        channel_id = default_channel("whatsapp")
    if not channel_id:
        raise inbox.BridgeError("В Wazzup нет активного канала для отправки")
    body: dict = {"channelId": channel_id, "chatType": chat_type, "chatId": ext}
    if payload.get("media"):
        base = (config.WEBAPP_URL or "").split("?")[0].rstrip("/")
        if not base.startswith("https://"):
            raise inbox.BridgeError("Для отправки файлов через Wazzup нужен публичный https-адрес (WEBAPP_URL)")
        body["contentUri"] = f"{base}{config.API_PREFIX}/inbox/media/{payload['media']}?s={inbox.media_sig(payload['media'])}"
        if payload.get("text"):
            body["text"] = payload["text"]  # caption, where the transport supports it
    else:
        body["text"] = payload.get("text") or ""
    q = payload.get("quoted_id") or ""
    if q.startswith("wz:"):
        body["refMessageId"] = q[3:]
    data = _api("POST", "/message", timeout=30, json=body)
    mid = (data or {}).get("messageId") or uuid.uuid4().hex
    if chat and not wz:
        _remember(chat["id"], {"channel_id": channel_id, "chat_type": chat_type, "ext_chat_id": ext})
    return {"id": "wz:" + mid}
