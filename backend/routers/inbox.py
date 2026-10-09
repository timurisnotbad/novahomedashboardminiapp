"""«Чаты» — shared WhatsApp inbox API (see backend/inbox.py)."""
import hmac
import mimetypes
import re
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config, inbox

MAX_UPLOAD = 30 * 1024 * 1024


async def inbox_user(
    x_telegram_init_data: str = Header(default=""),  # noqa: B008
    x_owner_key: str = Header(default=""),  # noqa: B008
    x_inbox_key: str = Header(default=""),  # noqa: B008
) -> dict:
    user = inbox.resolve_user(x_telegram_init_data, x_owner_key, x_inbox_key)
    if not user:
        raise HTTPException(status_code=403, detail="Нет доступа к чатам — откройте их командой /chats в боте")
    return user


async def owner_user(user: dict = Depends(inbox_user)) -> dict:  # noqa: B008
    if not user["owner"]:
        raise HTTPException(status_code=403, detail="Только для владельца")
    return user


router = APIRouter(prefix="/inbox", tags=["inbox"])
# bridge -> server and signed media links: no user headers
public = APIRouter(prefix="/inbox", tags=["inbox"])


def _fail(exc: Exception):
    raise HTTPException(status_code=502, detail=str(exc)) from exc


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


class TemplateIn(BaseModel):
    title: str = ""
    text: str


@router.get("/me")
def me(user: dict = Depends(inbox_user)):  # noqa: B008
    st = inbox.bridge_status()
    return {"user": user, "agents": inbox.agents(), "rev": inbox.current_rev(),
            "wa": {"status": st.get("status"), "me": st.get("me"), "error": st.get("error")}}


@router.get("/status")
def status(user: dict = Depends(inbox_user)):  # noqa: B008
    st = inbox.bridge_status()
    if not user["owner"]:
        st.pop("qr", None)  # only owners link the phone
    return st


@router.post("/logout")
def logout(user: dict = Depends(owner_user)):  # noqa: B008
    try:
        inbox.bridge_logout()
    except inbox.BridgeError as exc:
        _fail(exc)
    return {"ok": True}


@router.get("/chats")
def chats(q: str = "", only: str = "all", user: dict = Depends(inbox_user)):  # noqa: B008
    return {"rev": inbox.current_rev(), "chats": inbox.chat_list(q, only, user["name"])}


@router.get("/poll")
def poll(since: int = 0, chat: int | None = None, user: dict = Depends(inbox_user)):  # noqa: B008
    return inbox.poll(since, chat)


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


@router.post("/chats/{chat_id}/send")
def send(chat_id: int, payload: SendIn, user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        return inbox.send(chat_id, user["name"], payload.text[:4000], quoted_id=payload.quoted_id)
    except inbox.BridgeError as exc:
        _fail(exc)


@router.post("/chats/{chat_id}/file")
async def send_file(chat_id: int, request: Request, user: dict = Depends(inbox_user)):  # noqa: B008
    """Raw file body (no multipart dependency): name in X-File-Name (URL-encoded),
    type in Content-Type, optional caption in X-Caption (URL-encoded)."""
    from urllib.parse import unquote
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Пустой файл")
    if len(body) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Файл больше 30 МБ")
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


@router.post("/new")
def new_chat(payload: NewChatIn, user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        c = inbox.start_chat(payload.phone, payload.name)
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
    return {"id": inbox.add_template(payload.title or payload.text[:30], payload.text)}


@router.delete("/templates/{tid}")
def del_template(tid: int, user: dict = Depends(inbox_user)):  # noqa: B008
    inbox.delete_template(tid)
    return {"ok": True}


# ---- bridge & media ---------------------------------------------------------
@public.post("/hook")
async def hook(request: Request, x_inbox_secret: str = Header(default="")):  # noqa: B008
    if not hmac.compare_digest(x_inbox_secret, config.INBOX_SECRET):
        raise HTTPException(status_code=403, detail="bad secret")
    data = await request.json()
    from starlette.concurrency import run_in_threadpool
    ev = data.get("event")
    if ev == "message":
        await run_in_threadpool(inbox.store_message, data.get("message") or {}, bool(data.get("notify")))
    elif ev == "history":
        await run_in_threadpool(inbox.store_history, data.get("messages") or [])
    elif ev == "contacts":
        await run_in_threadpool(inbox.store_contacts, data.get("contacts") or [])
    elif ev == "ack":
        await run_in_threadpool(inbox.store_ack, data.get("id") or "", data.get("status") or "")
    elif ev == "status":
        await run_in_threadpool(inbox.on_bridge_status, data.get("status") or "")
    return {"ok": True}


@public.get("/media/{name}")
def media(name: str, s: str = ""):
    # links are signed by the server (img/audio tags can't send auth headers)
    if not re.fullmatch(r"[A-Za-z0-9]+\.[A-Za-z0-9]{1,8}", name) or not hmac.compare_digest(s, inbox.media_sig(name)):
        raise HTTPException(status_code=404)
    path = config.INBOX_MEDIA_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
