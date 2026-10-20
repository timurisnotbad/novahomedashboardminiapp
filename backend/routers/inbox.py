"""«Чаты» — shared WhatsApp inbox API (see backend/inbox.py)."""
import hmac
import mimetypes
import re
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config, database, inbox, meta_api, wazzup

MAX_UPLOAD = 30 * 1024 * 1024


def inbox_user(  # sync on purpose: it reads the DB, so it runs in the threadpool, not in the event loop
    request: Request,
    x_telegram_init_data: str = Header(default=""),  # noqa: B008
    x_owner_key: str = Header(default=""),  # noqa: B008
    x_inbox_key: str = Header(default=""),  # noqa: B008
) -> dict:
    user = inbox.resolve_user(x_telegram_init_data, x_owner_key, x_inbox_key, request.cookies.get("nh_crm", ""))
    if not user:
        raise HTTPException(status_code=403, detail="Нет доступа к чатам — откройте их командой /chats в боте")
    return user


def owner_user(user: dict = Depends(inbox_user)) -> dict:  # noqa: B008
    if not user["owner"]:
        raise HTTPException(status_code=403, detail="Только для владельца")
    return user


router = APIRouter(prefix="/inbox", tags=["inbox"])
# bridge -> server and signed media links: no user headers
public = APIRouter(prefix="/inbox", tags=["inbox"])


def _fail(exc: Exception):
    raise HTTPException(status_code=424, detail=config.redact(str(exc))[:300]) from exc


class SendIn(BaseModel):
    text: str = ""
    quoted_id: str | None = None


class ChatPatch(BaseModel):
    assignee: str | None = None
    name: str | None = None
    unread: int | None = None


class NewChatIn(BaseModel):
    phone: str
    name: str = ""
    text: str = ""
    channel: str = "wa"


class TemplateIn(BaseModel):
    title: str = ""
    text: str
    command: str = ""


@router.get("/me")
def me(user: dict = Depends(inbox_user)):  # noqa: B008
    st = inbox.bridge_status()
    from .. import ai
    return {"user": user, "agents": inbox.agents(), "rev": inbox.current_rev(),
            "wa": {"status": st.get("status"), "me": st.get("me"), "error": st.get("error"), "wazzup": wazzup.configured()},
            "ai": {"enabled": ai.enabled(), "mode": ai.setting("ai_mode"), "author": ai.AUTHOR,
                   "transcribe": bool(ai.openai_key()) and ai.setting("ai_transcribe") == "1"}}


@router.get("/status")
def status(user: dict = Depends(inbox_user)):  # noqa: B008
    st = inbox.bridge_status(fresh=True)  # the QR modal polls this
    if not user["owner"]:
        st.pop("qr", None)  # only owners link the phone
    st["wazzup"] = wazzup.configured()  # WhatsApp works through Wazzup: the QR bridge is only a backup
    return st


@router.post("/logout")
def logout(user: dict = Depends(owner_user)):  # noqa: B008
    try:
        inbox.bridge_logout()
    except inbox.BridgeError as exc:
        _fail(exc)
    return {"ok": True}


@router.get("/chats")
def chats(q: str = "", only: str = "all", before: str = "", limit: int = 300, user: dict = Depends(inbox_user)):  # noqa: B008
    """`before` = last_at of the last chat shown → the next (older) page."""
    limit = max(20, min(limit, 500))
    rows = inbox.chat_list(q, only, user["name"], limit, before[:19])
    return {"rev": inbox.current_rev(), "chats": rows, "more": len(rows) >= limit and not q}


@router.get("/poll")
async def poll(since: int = 0, chat: int | None = None, wait: float = 0, user: dict = Depends(inbox_user)):  # noqa: B008
    """`wait` > 0: hold the request until something changes (long-poll)."""
    from starlette.concurrency import run_in_threadpool
    if wait > 0:
        await inbox.wait_for_change(since, wait)
    return await run_in_threadpool(inbox.poll, since, chat)


@router.get("/chats/{chat_id}")
def chat(chat_id: int, before: int | None = None, user: dict = Depends(inbox_user)):  # noqa: B008
    c = inbox.get_chat(chat_id)
    if not c:
        raise HTTPException(status_code=404, detail="Чат не найден")
    return {"chat": c, "messages": inbox.messages(chat_id, before), "rev": inbox.current_rev()}


@router.post("/chats/{chat_id}/read")
def read(chat_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    inbox.mark_read(chat_id)
    return {"ok": True}


@router.post("/chats/{chat_id}/eta")
def chat_eta(chat_id: int, payload: dict, user: dict = Depends(inbox_user)):  # noqa: B008
    """«Приедет в …» for the chat's booking — the housekeepers' morning list."""
    from .. import crm
    c = inbox.get_chat(chat_id)
    if not c or not c.get("booking"):
        raise HTTPException(status_code=400, detail="У чата нет брони — сначала привяжите бронь")
    try:
        crm.set_eta(c["booking"]["id"], str(payload.get("eta") or ""), str(payload.get("note") or ""), user["name"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with database.get_conn() as conn:
        conn.execute("UPDATE inbox_chats SET rev = ? WHERE id = ?", (inbox._bump(conn), chat_id))  # noqa: SLF001
    return {"ok": True, "chat": inbox.get_chat(chat_id)}


@router.post("/chats/{chat_id}/receipt")
def receipt(chat_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    """«Прочитано» for the guest: read receipt to WhatsApp / Telegram."""
    sent = inbox.send_receipt(chat_id, "button")
    c = inbox.get_chat(chat_id)
    return {"ok": True, "sent": sent, "chat": c, "note": None if sent else (c or {}).get("receipt")}


@router.patch("/chats/{chat_id}")
def patch_chat(chat_id: int, payload: ChatPatch, user: dict = Depends(inbox_user)):  # noqa: B008
    fields = payload.model_dump(exclude_unset=True)
    if "assignee" in fields:
        fields["assignee"] = (fields["assignee"] or "").strip() or None
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()[:80] or None
    c = inbox.update_chat(chat_id, **fields)
    if not c:
        raise HTTPException(status_code=404, detail="Чат не найден")
    return c


class BookingLinkIn(BaseModel):
    booking_id: int


class ClientPatch(BaseModel):
    status: str | None = None
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    phone2: str | None = None
    instagram: str | None = None
    telegram: str | None = None
    notes: str | None = None
    city: str | None = None
    lang: str | None = None


@router.get("/chats/{chat_id}/client")
def get_chat_client(chat_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    from .. import crm, crm_amo
    chat = inbox.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    cid = chat.get("client_id") or crm_amo._client_for_chat(chat)  # noqa: SLF001
    c = crm.get_client(cid) or {}
    return {k: c.get(k) or "" for k in ("id", "name", "phone", "email", "phone2", "instagram", "telegram", "notes", "city", "lang", "status")} | {"id": cid}


@router.get("/chats/{chat_id}/bookings")
def chat_bookings(chat_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    from .. import crm_ext
    try:
        return crm_ext.chat_bookings(chat_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/chats/{chat_id}/bookings")
def chat_link_booking(chat_id: int, payload: BookingLinkIn, user: dict = Depends(inbox_user)):  # noqa: B008
    from .. import crm_ext
    try:
        crm_ext.link_chat(payload.booking_id, chat_id, user["name"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return inbox.get_chat(chat_id)


@router.delete("/chats/{chat_id}/bookings/{booking_id}")
def chat_unlink_booking(chat_id: int, booking_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    from .. import crm_ext
    crm_ext.unlink_chat(booking_id, chat_id)
    return inbox.get_chat(chat_id)


@router.get("/bookings/search")
def bookings_search(q: str = "", user: dict = Depends(inbox_user)):  # noqa: B008
    from .. import crm_ext
    return crm_ext.bookings_search(q)


@router.get("/client-statuses")
def client_statuses(user: dict = Depends(inbox_user)):  # noqa: B008
    from .. import crm
    return crm.client_statuses()


@router.patch("/chats/{chat_id}/client")
def patch_client(chat_id: int, payload: ClientPatch, user: dict = Depends(inbox_user)):  # noqa: B008
    """Set the guest's status straight from the chat (creates the CRM card if needed)."""
    from .. import crm, crm_amo
    chat = inbox.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    cid = chat.get("client_id") or crm_amo._client_for_chat(chat)  # noqa: SLF001
    if payload.status is not None:
        crm.set_client_status(cid, payload.status)
    fields = {k: v for k, v in payload.model_dump().items() if k != "status" and v is not None}
    if fields:
        cur = crm.get_client(cid) or {}
        body = {k: cur.get(k) for k in ("name", "phone", "email", "source", "notes", "fields", "status", "phone2", "instagram", "telegram", "city", "lang", "birthday", "passport")}
        body.update(fields)
        try:
            crm.save_client(cid, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if payload.name is not None:
            inbox.update_chat(chat_id, name=payload.name)
        try:
            from .crm import _push_client_bg
            _push_client_bg(cid, user["name"])
        except Exception:  # noqa: BLE001
            pass
    return inbox.get_chat(chat_id)


@router.post("/chats/{chat_id}/send")
def send(chat_id: int, payload: SendIn, user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        r = inbox.send(chat_id, user["name"], payload.text[:4000], quoted_id=payload.quoted_id)
    except inbox.BridgeError as exc:
        _fail(exc)
    try:
        from .. import ai
        ai.pause_for_human(chat_id, payload.text)  # a person took over: the assistant steps back for a while
    except Exception:  # noqa: BLE001
        pass
    return r


# ---- AI assistant per chat ------------------------------------------------------
@router.post("/chats/{chat_id}/ai")
def chat_ai_mode(chat_id: int, payload: dict, user: dict = Depends(inbox_user)):  # noqa: B008
    """mode: off | draft | auto | null (= as in settings)."""
    from .. import ai
    ai.set_chat_mode(chat_id, payload.get("mode"))
    c = inbox.get_chat(chat_id)
    eff = c and ai.chat_mode(c)
    reason = ""
    if c and eff == "off" and (c.get("ai_mode") or ai.setting("ai_mode")) != "off":
        reason = "ключ API не задан" if not ai.enabled() else f"канал «{c.get('channel_name')}» не включён для ИИ (Интеграции → ИИ-ассистент → Каналы)"
    return {"ok": True, "ai_mode": c and c.get("ai_mode"), "effective": eff, "reason": reason}


@router.post("/chats/{chat_id}/ai/draft")
def chat_ai_draft(chat_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    """«Ответить с ИИ»: a draft right now, whatever the mode."""
    from .. import ai
    if not ai.enabled():
        raise HTTPException(status_code=400, detail="ИИ-ассистент не настроен: Интеграции → ИИ-ассистент → ключ API")
    # The model with tools and photos can take 20–40 s; a request that long dies in the
    # browser timeout or the tunnel («Нет связи с сервером»). Queue it and answer at once:
    # the draft (or the reason there is none) reaches the chat through the usual poll.
    from .. import bg
    ai.mark_thinking(chat_id)
    bg.run(ai.handle, chat_id, force=True, key=f"ai-draft-{chat_id}")
    return {"status": "queued"}


@router.delete("/chats/{chat_id}/ai/draft")
def chat_ai_draft_clear(chat_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    from .. import ai
    ai.clear_draft(chat_id)
    return {"ok": True}


@router.post("/chats/{chat_id}/ai/draft/send")
def chat_ai_draft_send(chat_id: int, payload: dict, user: dict = Depends(inbox_user)):  # noqa: B008
    """Send the assistant's draft (edited text allowed) plus the photos it queued."""
    from .. import ai
    try:
        return ai.send_draft(chat_id, user["name"], payload.get("text"))
    except inbox.BridgeError as exc:
        _fail(exc)


@router.post("/messages/{message_id}/transcribe")
def message_transcribe(message_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    """🎙 on a voice note: speech → text once (OpenAI key in Интеграции → ИИ-ассистент)."""
    from .. import ai
    if not ai.openai_key():
        raise HTTPException(status_code=400, detail="Расшифровка голосовых не настроена: нужен ключ OpenAI (Интеграции → ИИ-ассистент)")
    text = ai.transcribe_message(message_id)
    if not text:
        raise HTTPException(status_code=424, detail="Не удалось расшифровать: см. журнал ИИ")
    return {"text": text}


@router.post("/chats/{chat_id}/ai/rate")
def chat_ai_rate(chat_id: int, payload: dict, user: dict = Depends(inbox_user)):  # noqa: B008
    from .. import ai
    ai.rate(chat_id, bool(payload.get("good")))
    return {"ok": True}


@router.post("/chats/{chat_id}/file")
async def send_file(chat_id: int, request: Request, user: dict = Depends(inbox_user)):  # noqa: B008
    """Raw file body (no multipart dependency): name in X-File-Name (URL-encoded),
    type in Content-Type, optional caption in X-Caption (URL-encoded)."""
    from urllib.parse import unquote
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Файл больше 30 МБ")
    body = await _read_capped(request, MAX_UPLOAD)
    if not body:
        raise HTTPException(status_code=400, detail="Пустой файл")
    name = unquote(request.headers.get("x-file-name", "") or "file")[:120]
    mime = (request.headers.get("content-type") or "").split(";")[0].strip() \
        or mimetypes.guess_type(name)[0] or "application/octet-stream"
    caption = unquote(request.headers.get("x-caption", "") or "")[:1000]
    ext = re.sub(r"[^A-Za-z0-9]", "", name.rsplit(".", 1)[-1])[:8] if "." in name else "bin"
    stored = f"up{uuid.uuid4().hex}.{ext or 'bin'}"
    config.INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (config.INBOX_MEDIA_DIR / stored).write_bytes(body)
    from starlette.concurrency import run_in_threadpool
    try:
        return await run_in_threadpool(inbox.send, chat_id, user["name"], caption, stored, mime, name)
    except inbox.BridgeError as exc:
        _fail(exc)


@router.post("/messages/{message_id}/retry")
def retry(message_id: int, user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        return inbox.retry(message_id, user["name"])
    except inbox.BridgeError as exc:
        _fail(exc)


@router.get("/new/channels")
def new_chat_channels(user: dict = Depends(inbox_user)):  # noqa: B008
    return inbox.new_chat_channels()


@router.get("/new/check")
def new_chat_check(phone: str, channel: str = "wa", user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        return inbox.check_contact(phone, channel)
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/new")
def new_chat(payload: NewChatIn, user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        c = inbox.start_chat(payload.phone, payload.name, payload.channel)
        if payload.text.strip():
            inbox.send(c["id"], user["name"], payload.text[:4000])
        return inbox.get_chat(c["id"])
    except inbox.BridgeError as exc:
        _fail(exc)


@router.get("/templates")
def get_templates(user: dict = Depends(inbox_user)):  # noqa: B008
    return inbox.templates()


@router.post("/templates")
def add_template(payload: TemplateIn, user: dict = Depends(inbox_user)):  # noqa: B008
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Пустой шаблон")
    from .. import crm
    try:
        return {"id": crm.save_command(None, payload.command, payload.title, payload.text)["id"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/templates/{tid}")
def del_template(tid: int, user: dict = Depends(inbox_user)):  # noqa: B008
    inbox.delete_template(tid)
    return {"ok": True}


# ---- Booking.com Chrome extension ------------------------------------------------
def _ext_auth(x_ext_token: str = Header(default="")):  # noqa: B008
    from .. import booking_ext
    if not hmac.compare_digest(x_ext_token or "", booking_ext.token()):
        raise HTTPException(status_code=403, detail="bad extension token")
    return True


@public.get("/ext/config")
def ext_config(_=Depends(_ext_auth)):  # noqa: B008
    from .. import booking_ext, config as cfg
    return {"selectors": booking_ext.selectors(), "version": cfg.APP_VERSION, "poll": 15, "import_history": booking_ext.import_on(),
            "catchup": booking_ext.catchup_active()}


@public.post("/ext/ping")
def ext_ping(payload: dict, _=Depends(_ext_auth)):  # noqa: B008
    from .. import booking_ext
    booking_ext.ping(payload or {})
    return {"ok": True}


@public.post("/ext/messages")
def ext_messages(payload: dict, _=Depends(_ext_auth)):  # noqa: B008
    from .. import booking_ext
    convs = payload.get("conversations") if isinstance(payload.get("conversations"), list) else [payload]
    out, errors = [], []
    opened = payload.get("opened") if isinstance(payload.get("opened"), dict) else None
    for c in convs:
        if not isinstance(c, dict):
            continue
        try:
            out.append(booking_ext.ingest(c, imported=bool(payload.get("imported")), unread_item=bool(opened and opened.get("unread"))))
        except ValueError as exc:
            errors.append(str(exc))
    if opened:
        booking_ext.mark_seen(opened)
    booking_ext.set_debug(payload.get("debug") or "")
    just_read = {str(c.get("guest") or "").strip().lower() for c in convs if isinstance(c, dict)}
    want = booking_ext.wanted(payload.get("list") or [], skip=just_read) if not payload.get("busy") and isinstance(payload.get("list"), list) else []
    if payload.get("deep") and booking_ext.import_on():
        booking_ext.import_progress(bool(want))
    if isinstance(payload.get("list"), list) and not payload.get("busy"):
        booking_ext.catchup_progress(bool(want), bool(payload.get("deep")))
    if errors and not out and not want:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {"ok": True, "results": out, "open": want, "errors": errors, "catchup": booking_ext.catchup_active()}


@public.get("/ext/outbox")
def ext_outbox(_=Depends(_ext_auth)):  # noqa: B008
    from .. import booking_ext
    return booking_ext.outbox()


@public.post("/ext/outbox/{oid}")
def ext_outbox_done(oid: int, payload: dict, _=Depends(_ext_auth)):  # noqa: B008
    from .. import booking_ext
    booking_ext.outbox_done(oid, bool(payload.get("ok")), payload.get("error") or "")
    return {"ok": True}


# ---- bridge & media ---------------------------------------------------------
async def _read_capped(request: Request, limit: int) -> bytes:
    """Read a body without trusting Content-Length: stop at `limit` bytes."""
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(status_code=413, detail="Слишком большой запрос")
    return bytes(buf)


async def _json_capped(request: Request, limit: int = 2_000_000):
    import json
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(status_code=413, detail="Слишком большой запрос")
    raw = await _read_capped(request, limit)
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="bad json") from exc


@public.post("/hook")
async def hook(request: Request, x_inbox_secret: str = Header(default="")):  # noqa: B008
    if not hmac.compare_digest(x_inbox_secret, config.INBOX_SECRET):
        raise HTTPException(status_code=403, detail="bad secret")
    data = await _json_capped(request, 8_000_000)  # history batches from the bridge can be big
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="bad json")
    from starlette.concurrency import run_in_threadpool
    ev = data.get("event")
    if ev == "message":
        # "append" messages are the phone's older history: no unread, no alert
        notify = bool(data.get("notify"))
        await run_in_threadpool(inbox.store_message, data.get("message") or {}, notify, not notify)
    elif ev == "history":
        await run_in_threadpool(inbox.store_history, data.get("messages") or [])
    elif ev == "contacts":
        await run_in_threadpool(inbox.store_contacts, data.get("contacts") or [])
    elif ev == "edit":
        await run_in_threadpool(inbox.edit_message, str(data.get("id") or ""), data.get("text") or "")
    elif ev == "ack":
        await run_in_threadpool(inbox.store_ack, data.get("id") or "", data.get("status") or "")
    elif ev == "status":
        await run_in_threadpool(inbox.on_bridge_status, data.get("status") or "")
    return {"ok": True}


@public.get("/meta/webhook")
def meta_verify(request: Request):
    """Meta's one-time subscription check (hub.challenge)."""
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and config.META_VERIFY_TOKEN and hmac.compare_digest(q.get("hub.verify_token") or "", config.META_VERIFY_TOKEN):
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(q.get("hub.challenge") or "")
    raise HTTPException(status_code=403, detail="bad verify token")


@public.post("/meta/webhook")
async def meta_webhook(request: Request):
    # Nothing to receive until Meta is configured WITH its app secret: an open
    # webhook would let anyone inject «guest» messages and make the server fetch URLs
    if not config.META_APP_SECRET or not (meta_api.wa_configured() or meta_api.ig_configured()):
        raise HTTPException(status_code=404)
    if int(request.headers.get("content-length") or 0) > 1_000_000:
        raise HTTPException(status_code=413)
    body = await _read_capped(request, 1_000_000)
    if not meta_api.signature_ok(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=403, detail="bad signature")
    import json
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {"ok": False}
    if isinstance(data, dict):
        meta_api.handle_webhook(data)  # answer fast, process in a thread
    return {"ok": True}


@public.post("/wazzup/webhook/{token}")
async def wazzup_webhook(token: str, request: Request):
    if not hmac.compare_digest(token, wazzup.webhook_token()):
        raise HTTPException(status_code=403, detail="bad token")
    try:
        data = await _json_capped(request, 2_000_000)
    except HTTPException:
        data = {}
    if isinstance(data, dict) and not data.get("test"):
        wazzup.handle_webhook(data)
    return {"ok": True}  # Wazzup's connection test and every delivery expect a plain 200


@public.get("/media/{name}")
def media(name: str, s: str = ""):
    # links are signed by the server (img/audio tags can't send auth headers)
    if not re.fullmatch(r"[A-Za-z0-9]+\.[A-Za-z0-9]{1,8}", name) or not hmac.compare_digest(s, inbox.media_sig(name)):
        raise HTTPException(status_code=404)
    path = config.INBOX_MEDIA_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404)
    # Guests pick the file names and types. Only media the chat UI renders inline
    # is served as such; everything else downloads as an opaque attachment —
    # an uploaded .html/.svg must never run on the CRM origin.
    ext = name.rsplit(".", 1)[-1].lower()
    inline = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
              "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime", "m4a": "audio/mp4", "mp3": "audio/mpeg",
              "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac", "wav": "audio/wav", "pdf": "application/pdf"}
    headers = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "sandbox; default-src 'none'"}
    if ext in inline and _magic_ok(path, ext):
        return FileResponse(path, media_type=inline[ext], headers=headers)
    headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(path, media_type="application/octet-stream", headers=headers)


def _magic_ok(path, ext: str) -> bool:
    """The first bytes must match the extension for inline types (a .png that is
    really HTML is downloaded, not rendered)."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    sig = {"jpg": (b"\xff\xd8",), "jpeg": (b"\xff\xd8",), "png": (b"\x89PNG",), "gif": (b"GIF8",), "webp": (b"RIFF",),
           "pdf": (b"%PDF",), "mp4": (b"ftyp",), "mov": (b"ftyp",), "m4a": (b"ftyp",), "webm": (b"\x1aE\xdf\xa3",),
           "mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"), "ogg": (b"OggS",), "oga": (b"OggS",), "opus": (b"OggS",),
           "wav": (b"RIFF",), "aac": (b"\xff\xf1", b"\xff\xf9", b"ID3")}.get(ext)
    if not sig:
        return False
    return any(head.startswith(m) or (m == b"ftyp" and head[4:8] == b"ftyp") for m in sig)
