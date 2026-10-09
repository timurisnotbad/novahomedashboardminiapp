"""Official Meta channels for the inbox: WhatsApp Cloud API and Instagram Direct.

Both deliver events to ONE webhook — {WEBAPP_URL}/api/inbox/meta/webhook —
verified with META_VERIFY_TOKEN and (when META_APP_SECRET is set) checked
against the X-Hub-Signature-256 header. Replies go through the Graph API.

WhatsApp Cloud API needs WA_CLOUD_TOKEN + WA_CLOUD_PHONE_ID; Instagram needs
IG_PAGE_TOKEN (a Page access token of the Facebook Page linked to the
Instagram professional account). See README, «Каналы».
"""
import hashlib
import hmac
import logging
import mimetypes
import re
import threading
import uuid
from datetime import datetime, timezone

import requests

from . import config, inbox

logger = logging.getLogger("nova.meta")

MAX_MEDIA = 30 * 1024 * 1024


def wa_configured() -> bool:
    return bool(config.WA_CLOUD_TOKEN and config.WA_CLOUD_PHONE_ID)


def ig_configured() -> bool:
    return bool(config.IG_PAGE_TOKEN)


def webhook_url() -> str:
    base = (config.WEBAPP_URL or "").split("?")[0].rstrip("/")
    return f"{base}{config.API_PREFIX}/inbox/meta/webhook" if base else ""


def signature_ok(body: bytes, header: str) -> bool:
    if not config.META_APP_SECRET:
        return True  # not configured: accept (the verify token still guards the subscription)
    if not header.startswith("sha256="):
        return False
    calc = hmac.new(config.META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, header[7:])


def _graph(method: str, path: str, token: str, **kw) -> dict:
    kw.setdefault("timeout", 30)
    headers = kw.pop("headers", {})
    headers["Authorization"] = "Bearer " + token
    try:
        resp = requests.request(method, f"{config.META_GRAPH}/{path}", headers=headers, **kw)
    except requests.RequestException as exc:
        raise inbox.BridgeError(f"Meta API недоступен: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or "error" in data:
        err = (data.get("error") or {})
        msg = err.get("message") or f"HTTP {resp.status_code}"
        code = err.get("code")
        if code == 131047 or "24 hours" in msg or "re-engagement" in msg.lower():
            msg = "Прошло больше 24 часов с последнего сообщения гостя — WhatsApp разрешает только шаблонное сообщение"
        raise inbox.BridgeError(msg)
    return data


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(tz=timezone.utc).isoformat()


def _save_media(content: bytes, mime: str, hint: str = "") -> str:
    ext = (mimetypes.guess_extension(mime or "") or ("." + hint.rsplit(".", 1)[-1] if "." in hint else ".bin")).lstrip(".")
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8] or "bin"
    name = f"m{uuid.uuid4().hex}.{ext}"
    config.INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (config.INBOX_MEDIA_DIR / name).write_bytes(content)
    return name


def _media_public_url(name: str) -> str:
    base = (config.WEBAPP_URL or "").split("?")[0].rstrip("/")
    return f"{base}{config.API_PREFIX}/inbox/media/{name}?s={inbox.media_sig(name)}"


# ---------------------------------------------------------------------------
# WhatsApp Cloud API
# ---------------------------------------------------------------------------
def _wa_download(media_id: str) -> tuple[bytes, str] | None:
    meta = _graph("GET", media_id, config.WA_CLOUD_TOKEN)
    url, mime = meta.get("url"), meta.get("mime_type") or ""
    if not url or int(meta.get("file_size") or 0) > MAX_MEDIA:
        return None
    r = requests.get(url, headers={"Authorization": "Bearer " + config.WA_CLOUD_TOKEN}, timeout=60)
    if r.status_code != 200:
        return None
    return r.content, mime


def _wa_message(value: dict, msg: dict) -> dict | None:
    sender = msg.get("from") or ""
    if not sender:
        return None
    names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
    t = msg.get("type")
    out = {"channel": "wac", "id": "wac:" + msg.get("id", uuid.uuid4().hex), "jid": f"{sender}@s.whatsapp.net",
           "phone": sender, "from_me": False, "push_name": names.get(sender), "at": _iso(msg.get("timestamp")),
           "kind": "text", "text": ""}
    ctx = msg.get("context") or {}
    if ctx.get("id"):
        out["quoted_id"] = "wac:" + ctx["id"]
    if t == "text":
        out["text"] = (msg.get("text") or {}).get("body", "")
    elif t in ("image", "video", "audio", "document", "sticker"):
        body = msg.get(t) or {}
        out.update(kind=t, text=body.get("caption", ""), mime=body.get("mime_type"), file_name=body.get("filename"),
                   voice=bool(body.get("voice")))
        try:
            got = _wa_download(body.get("id", ""))
            if got:
                out["media"] = _save_media(got[0], got[1], body.get("filename") or "")
        except Exception:  # noqa: BLE001
            logger.exception("wa cloud media download failed")
    elif t == "location":
        loc = msg.get("location") or {}
        out.update(kind="location", lat=loc.get("latitude"), lng=loc.get("longitude"), text=loc.get("name") or loc.get("address") or "")
    elif t == "contacts":
        out.update(kind="contact", text=", ".join((c.get("name") or {}).get("formatted_name", "") for c in msg.get("contacts") or []))
    elif t == "reaction":
        r = msg.get("reaction") or {}
        out.update(kind="reaction", text=r.get("emoji") or "", reaction_to="wac:" + (r.get("message_id") or ""))
    elif t in ("button", "interactive"):
        b = msg.get("button") or (msg.get("interactive") or {}).get("button_reply") or (msg.get("interactive") or {}).get("list_reply") or {}
        out["text"] = b.get("text") or b.get("title") or ""
    else:
        out["kind"] = "other"
    return out


def _handle_wa_change(value: dict) -> None:
    for msg in value.get("messages") or []:
        try:
            m = _wa_message(value, msg)
            if m:
                inbox.store_message(m, notify=True)
        except Exception:  # noqa: BLE001
            logger.exception("wa cloud message failed")
    for st in value.get("statuses") or []:
        s = st.get("status")
        if s in ("sent", "delivered", "read", "failed"):
            inbox.store_ack("wac:" + (st.get("id") or ""), s)


def wa_send(payload: dict) -> dict:
    if not wa_configured():
        raise inbox.BridgeError("WhatsApp Cloud API не настроен (WA_CLOUD_TOKEN, WA_CLOUD_PHONE_ID)")
    to = inbox.phone_of_jid(payload["jid"]) or re.sub(r"\D", "", payload["jid"])
    body: dict = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
    if payload.get("media"):
        mime = payload.get("mime") or "application/octet-stream"
        kind = ("image" if mime.startswith("image/") and mime != "image/gif" else "video" if mime.startswith("video/")
                else "audio" if mime.startswith("audio/") else "document")
        link = _media_public_url(payload["media"])
        if not link.startswith("https://"):
            raise inbox.BridgeError("Для отправки файлов через Cloud API нужен публичный https-адрес (WEBAPP_URL)")
        obj = {"link": link}
        if kind in ("image", "video", "document") and payload.get("text"):
            obj["caption"] = payload["text"]
        if kind == "document" and payload.get("file_name"):
            obj["filename"] = payload["file_name"]
        body.update(type=kind, **{kind: obj})
    else:
        body.update(type="text", text={"body": payload.get("text") or "", "preview_url": True})
    if payload.get("quoted_id", "").startswith("wac:"):
        body["context"] = {"message_id": payload["quoted_id"][4:]}
    data = _graph("POST", f"{config.WA_CLOUD_PHONE_ID}/messages", config.WA_CLOUD_TOKEN, json=body)
    mid = ((data.get("messages") or [{}])[0]).get("id") or uuid.uuid4().hex
    return {"id": "wac:" + mid}


def wa_status() -> dict:
    if not wa_configured():
        return {"configured": False}
    try:
        d = _graph("GET", config.WA_CLOUD_PHONE_ID, config.WA_CLOUD_TOKEN,
                   params={"fields": "display_phone_number,verified_name,quality_rating"}, timeout=10)
        return {"configured": True, "ok": True, "phone": d.get("display_phone_number"), "name": d.get("verified_name"),
                "quality": d.get("quality_rating")}
    except inbox.BridgeError as exc:
        return {"configured": True, "ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Instagram Direct (Messenger Platform, through the linked Facebook Page)
# ---------------------------------------------------------------------------
_ig_names: dict[str, str] = {}


def _ig_name(igsid: str) -> str | None:
    if igsid in _ig_names:
        return _ig_names[igsid]
    try:
        d = _graph("GET", igsid, config.IG_PAGE_TOKEN, params={"fields": "name,username"}, timeout=10)
        nm = d.get("name") or (("@" + d["username"]) if d.get("username") else None)
    except Exception:  # noqa: BLE001
        nm = None
    _ig_names[igsid] = nm
    return nm


def _ig_event(ev: dict) -> dict | None:
    msg = ev.get("message")
    if not msg:
        return None
    echo = bool(msg.get("is_echo"))
    other = (ev.get("recipient") if echo else ev.get("sender")) or {}
    igsid = other.get("id")
    if not igsid:
        return None
    out = {"channel": "ig", "id": "ig:" + (msg.get("mid") or uuid.uuid4().hex), "jid": f"ig:{igsid}", "phone": "",
           "from_me": echo, "push_name": None if echo else _ig_name(igsid),
           "at": _iso(int(ev.get("timestamp") or 0) / 1000), "kind": "text", "text": msg.get("text") or ""}
    if (msg.get("reply_to") or {}).get("mid"):
        out["quoted_id"] = "ig:" + msg["reply_to"]["mid"]
    if msg.get("reaction"):
        out.update(kind="reaction", text=msg["reaction"].get("emoji") or "", reaction_to="ig:" + (msg["reaction"].get("mid") or ""))
        return out
    for att in msg.get("attachments") or []:
        t = att.get("type")
        url = (att.get("payload") or {}).get("url")
        kind = {"image": "image", "video": "video", "audio": "audio", "file": "document", "share": "other", "story_mention": "other"}.get(t, "other")
        if t in ("share", "story_mention"):
            out["text"] = (out["text"] + " " + (url or "")).strip() or ("Поделился(ась) " + ("историей" if t == "story_mention" else "публикацией"))
            continue
        out["kind"] = kind
        if url:
            try:
                r = requests.get(url, timeout=60, stream=True)
                content = r.raw.read(MAX_MEDIA + 1)
                if r.status_code == 200 and len(content) <= MAX_MEDIA:
                    mime = (r.headers.get("Content-Type") or "").split(";")[0]
                    out["media"] = _save_media(content, mime, url.split("?")[0])
                    out["mime"] = mime
            except Exception:  # noqa: BLE001
                logger.exception("ig media download failed")
        break
    return out


def _handle_ig_entry(entry: dict) -> None:
    for ev in entry.get("messaging") or entry.get("standby") or []:
        try:
            m = _ig_event(ev)
            if m:
                inbox.store_message(m, notify=True)
            elif ev.get("read"):
                pass  # read receipts carry a watermark, not ids — skipped
        except Exception:  # noqa: BLE001
            logger.exception("instagram event failed")


def ig_send(payload: dict) -> dict:
    if not ig_configured():
        raise inbox.BridgeError("Instagram не настроен (IG_PAGE_TOKEN)")
    igsid = payload["jid"].split(":", 1)[1]
    body: dict = {"recipient": {"id": igsid}, "messaging_type": "RESPONSE"}
    if payload.get("media"):
        mime = payload.get("mime") or ""
        t = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else "file"
        link = _media_public_url(payload["media"])
        if not link.startswith("https://"):
            raise inbox.BridgeError("Для отправки файлов в Instagram нужен публичный https-адрес (WEBAPP_URL)")
        body["message"] = {"attachment": {"type": t, "payload": {"url": link}}}
        if payload.get("text"):
            _graph("POST", "me/messages", config.IG_PAGE_TOKEN, json={"recipient": {"id": igsid}, "messaging_type": "RESPONSE",
                                                                       "message": {"text": payload["text"]}})
    else:
        body["message"] = {"text": payload.get("text") or ""}
    data = _graph("POST", "me/messages", config.IG_PAGE_TOKEN, json=body)
    return {"id": "ig:" + (data.get("message_id") or uuid.uuid4().hex)}


def ig_status() -> dict:
    if not ig_configured():
        return {"configured": False}
    try:
        d = _graph("GET", "me", config.IG_PAGE_TOKEN, params={"fields": "name,instagram_business_account{username}"}, timeout=10)
        ig = d.get("instagram_business_account") or {}
        return {"configured": True, "ok": True, "page": d.get("name"), "username": ig.get("username")}
    except inbox.BridgeError as exc:
        return {"configured": True, "ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Webhook entry point
# ---------------------------------------------------------------------------
def handle_webhook(data: dict) -> None:
    """Runs in a thread: Meta expects a fast 200, media downloads can be slow."""
    threading.Thread(target=_handle, args=(data,), daemon=True).start()


def _handle(data: dict) -> None:
    obj = data.get("object")
    for entry in data.get("entry") or []:
        if obj == "whatsapp_business_account":
            for ch in entry.get("changes") or []:
                if ch.get("field") == "messages":
                    _handle_wa_change(ch.get("value") or {})
        elif obj in ("instagram", "page"):
            _handle_ig_entry(entry)
        else:
            logger.info("meta webhook: unknown object %s", obj)
