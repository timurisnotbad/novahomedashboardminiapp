"""Nova Home CRM API (see backend/crm.py). Cookie session; admin-only routes
for users, pipelines, fields and integrations."""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import booking_ext, config, crm, crm_amo, crm_ext, database, docs, inbox, meta_api, rc_push, rc_sync, tg_channels, wazzup

logger = logging.getLogger("nova.crm.api")
router = APIRouter(prefix="/crm", tags=["crm"])


def current_user(request: Request) -> dict:
    u = crm.user_from_token(request.cookies.get(crm.COOKIE, ""))
    if not u:
        raise HTTPException(status_code=401, detail="Войдите в систему")
    return u


def admin_user(user: dict = Depends(current_user)) -> dict:  # noqa: B008
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user


def _bad(exc: Exception):
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _set_cookie(resp: Response, user: dict) -> None:
    resp.set_cookie(crm.COOKIE, crm.make_token(user), max_age=crm.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=config.WEBAPP_URL.startswith("https"), path="/")


# ---- auth ---------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str
    password: str


class SetupIn(BaseModel):
    name: str = "Администратор"
    email: str
    password: str


@router.get("/session")
def session(request: Request):
    u = crm.user_from_token(request.cookies.get(crm.COOKIE, ""))
    return {"user": u, "setup_needed": not crm.has_users(), "version": config.APP_VERSION, "times": crm.default_times()}


@router.post("/setup")
def setup(payload: SetupIn, response: Response):
    if crm.has_users():
        raise HTTPException(status_code=403, detail="Администратор уже создан")
    try:
        u = crm.create_user(payload.name, payload.email, payload.password, "admin")
    except ValueError as exc:
        _bad(exc)
    _set_cookie(response, u)
    return {"user": u}


_fails: dict[str, list[float]] = {}  # ip -> timestamps of failed logins


@router.post("/login")
def login(payload: LoginIn, request: Request, response: Response):
    import time
    ip = request.client.host if request.client else "?"
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="Слишком много попыток — подождите 10 минут")
    u = crm.login(payload.email, payload.password)
    if not u:
        _fails[ip] = recent + [time.time()]
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    _fails.pop(ip, None)
    _set_cookie(response, u)
    return {"user": u}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(crm.COOKIE, path="/")
    return {"ok": True}


# ---- users --------------------------------------------------------------------
class UserIn(BaseModel):
    name: str | None = None
    email: str | None = None
    password: str | None = None
    role: str | None = None
    active: bool | None = None


@router.get("/users")
def users(user: dict = Depends(current_user)):  # noqa: B008
    return crm.list_users()


@router.post("/users")
def add_user(payload: UserIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm.create_user(payload.name or "", payload.email or "", payload.password or "", payload.role or "manager")
    except ValueError as exc:
        _bad(exc)


@router.patch("/users/{uid}")
def edit_user(uid: int, payload: UserIn, user: dict = Depends(admin_user)):  # noqa: B008
    if uid == user["id"] and (payload.active is False or (payload.role and payload.role != "admin")):
        raise HTTPException(status_code=400, detail="Нельзя заблокировать или понизить себя")
    try:
        return crm.update_user(uid, payload.name, payload.email, payload.password, payload.role, payload.active)
    except ValueError as exc:
        _bad(exc)


class PasswordIn(BaseModel):
    password: str


@router.post("/me/password")
def my_password(payload: PasswordIn, response: Response, user: dict = Depends(current_user)):  # noqa: B008
    try:
        u = crm.update_user(user["id"], password=payload.password)
    except ValueError as exc:
        _bad(exc)
    _set_cookie(response, u)  # the token version changed
    return {"ok": True}


# ---- home ---------------------------------------------------------------------
@router.get("/home")
def home(user: dict = Depends(current_user)):  # noqa: B008
    d = crm_amo.home(user["id"])
    d["wa"] = inbox.bridge_status().get("status")
    return d


@router.get("/badges")
async def badges(rev: int = 0, wait: float = 0, user: dict = Depends(current_user)):  # noqa: B008
    """Sidebar counters + the latest incoming message (sound / popup in the shell).
    With rev+wait the request is held until the inbox changes (long-poll)."""
    from starlette.concurrency import run_in_threadpool
    if wait > 0 and rev > 0:
        await inbox.wait_for_change(rev, wait)
    return await run_in_threadpool(_badges, user)


def _badges(user: dict) -> dict:
    with database.get_conn() as conn:
        unread = conn.execute("SELECT COALESCE(SUM(unread), 0) FROM inbox_chats").fetchone()[0]
        mine = conn.execute("SELECT COUNT(*) FROM crm_tasks WHERE status = 'open' AND assignee_uid = ?", (user["id"],)).fetchone()[0]
        last = conn.execute(
            "SELECT m.id, m.chat_id, m.kind, m.text, m.at, c.name, c.push_name, c.phone, c.channel FROM inbox_messages m "
            "JOIN inbox_chats c ON c.id = m.chat_id WHERE m.direction = 'in' ORDER BY m.id DESC LIMIT 1").fetchone()
    latest = None
    if last:
        latest = {"id": last["id"], "chat_id": last["chat_id"], "at": last["at"], "channel": inbox.CHANNELS.get(last["channel"] or "wa", "WhatsApp"),
                  "title": last["name"] or last["push_name"] or (("+" + last["phone"]) if last["phone"] else "Гость"),
                  "text": inbox._preview(last["kind"], last["text"])}  # noqa: SLF001
    return {"unread": unread, "my_open_tasks": mine, "latest": latest, "rev": inbox.rev_hint()}


# ---- pipelines & fields -------------------------------------------------------
class PipelineIn(BaseModel):
    name: str
    stages: list[dict]


@router.get("/pipelines")
def get_pipelines(user: dict = Depends(current_user)):  # noqa: B008
    return crm.pipelines()


@router.post("/deals/backfill")
async def deals_backfill(user: dict = Depends(current_user)):  # noqa: B008
    """«Создать сделки из чатов» + move booking deals along the stay."""
    loop = asyncio.get_event_loop()
    n = await loop.run_in_executor(None, crm_amo.backfill_deals)
    await loop.run_in_executor(None, crm_ext.ensure_flow_stages)
    m = await loop.run_in_executor(None, crm_ext.booking_flow)
    return {"created": n, "moved": m}


@router.post("/pipelines")
def add_pipeline(payload: PipelineIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm.save_pipeline(None, payload.name, payload.stages)
    except ValueError as exc:
        _bad(exc)


@router.put("/pipelines/{pid}")
def put_pipeline(pid: int, payload: PipelineIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm.save_pipeline(pid, payload.name, payload.stages)
    except ValueError as exc:
        _bad(exc)


@router.delete("/pipelines/{pid}")
def del_pipeline(pid: int, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        crm.delete_pipeline(pid)
    except ValueError as exc:
        _bad(exc)
    return {"ok": True}


class FieldIn(BaseModel):
    name: str
    type: str = "text"
    options: list[str] = []
    entity: str = "client"


@router.get("/fields")
def get_fields(entity: str = "", user: dict = Depends(current_user)):  # noqa: B008
    return crm.fields(entity or None)


@router.post("/fields")
def add_field(payload: FieldIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm.save_field(None, payload.name, payload.type, payload.options, payload.entity)
    except ValueError as exc:
        _bad(exc)


@router.put("/fields/{fid}")
def put_field(fid: int, payload: FieldIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm.save_field(fid, payload.name, payload.type, payload.options, payload.entity)
    except ValueError as exc:
        _bad(exc)


@router.delete("/fields/{fid}")
def del_field(fid: int, user: dict = Depends(admin_user)):  # noqa: B008
    crm.delete_field(fid)
    return {"ok": True}


class OrderIn(BaseModel):
    ids: list[int]


@router.post("/fields/order")
def order_fields(payload: OrderIn, user: dict = Depends(admin_user)):  # noqa: B008
    crm.reorder("crm_fields", payload.ids)
    return {"ok": True}


# ---- clients ------------------------------------------------------------------
@router.get("/clients")
def get_clients(q: str = "", history: int = 0, status: str | None = None, user: dict = Depends(current_user)):  # noqa: B008
    rows = crm_ext.clients_with_history(q) if history else crm.clients(q)
    if status is not None:
        rows = [c for c in rows if (c.get("status") or "") == status]
    return rows


@router.get("/clients/statuses")
def get_client_statuses(user: dict = Depends(current_user)):  # noqa: B008
    with database.get_conn() as conn:
        counts = {r[0] or "": r[1] for r in conn.execute("SELECT status, COUNT(*) FROM crm_clients GROUP BY status").fetchall()}
    items = crm.client_statuses()
    for it in items:
        it["n"] = counts.get(it["name"], 0)
    return {"items": items, "text": crm.get_setting("client_statuses", crm.DEFAULT_CLIENT_STATUSES),
            "default": crm.DEFAULT_CLIENT_STATUSES, "none": counts.get("", 0)}


class StatusesIn(BaseModel):
    text: str


@router.post("/clients/statuses")
def set_client_statuses(payload: StatusesIn, user: dict = Depends(admin_user)):  # noqa: B008
    crm.set_setting("client_statuses", payload.text.strip())
    return {"ok": True, "items": crm.client_statuses()}


@router.post("/clients/{cid}/status")
def set_client_status(cid: int, payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    if not crm.get_client(cid):
        raise HTTPException(status_code=404, detail="Клиент не найден")
    crm.set_client_status(cid, payload.get("status") or "")
    return {"ok": True}


@router.get("/clients/export.xlsx")
def export_clients(user: dict = Depends(current_user)):  # noqa: B008
    from fastapi.responses import Response as RawResponse
    data = crm_ext.export_clients_xlsx()
    name = f"nova-clients-{crm._now()[:10]}.xlsx"
    return RawResponse(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/clients")
def add_client(payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm.save_client(None, payload)
    except ValueError as exc:
        _bad(exc)


@router.post("/clients/import")
def import_clients(user: dict = Depends(current_user)):  # noqa: B008
    return {"added": crm.import_clients()}


@router.get("/clients/{cid}")
def get_client(cid: int, user: dict = Depends(current_user)):  # noqa: B008
    c = crm.get_client(cid)
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    return c


def _push_client_bg(cid: int, who: str, only_booking: int | None = None) -> None:
    import threading

    def go():
        try:
            r = rc_push.push_client(cid, who, only_booking)
            if r.get("errors"):
                logger.warning("client %s → RC: %s", cid, r["errors"])
        except Exception:  # noqa: BLE001
            logger.exception("client → RC push failed")
    if not config.DEMO_MODE:
        threading.Thread(target=go, daemon=True).start()


@router.put("/clients/{cid}")
def put_client(cid: int, payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        c = crm.save_client(cid, payload)
    except ValueError as exc:
        _bad(exc)
    _push_client_bg(cid, user["name"], payload.get("booking_id"))
    c["rc_push"] = not config.DEMO_MODE and crm.get_setting("rc_sync_clients", "1") == "1"
    return c


@router.post("/clients/{cid}/push")
def push_client_now(cid: int, user: dict = Depends(current_user)):  # noqa: B008
    """«В календарь»: send the guest card to RealtyCalendar right now and report."""
    try:
        return rc_push.push_client(cid, user["name"])
    except rc_push.RCError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


@router.delete("/clients/{cid}")
def del_client(cid: int, user: dict = Depends(admin_user)):  # noqa: B008
    crm.delete_client(cid)
    return {"ok": True}


@router.post("/clients/{cid}/chat")
def client_chat(cid: int, payload: dict | None = None, user: dict = Depends(current_user)):  # noqa: B008
    """Open (or start) a chat with the client in WhatsApp (default) or Telegram ({"channel": "tg"})."""
    channel = (payload or {}).get("channel") or "wa"
    group = ("tg", "wztg") if channel == "tg" else inbox.WA_LIKE
    c = crm.get_client(cid)
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    for ch in crm.client_chats(cid):  # an existing conversation in that messenger
        if ch["channel"] in group:
            return {"chat_id": ch["id"]}
    if not c.get("phone"):
        raise HTTPException(status_code=400, detail="У клиента нет телефона")
    try:
        chat = inbox.start_chat(c["phone"], c.get("name") or "", channel)  # QR bridge / Wazzup / Telegram account
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=424, detail=f"Не удалось открыть чат: {exc}") from exc
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_clients SET chat_id = COALESCE(chat_id, ?) WHERE id = ?", (chat["id"], cid))
    return {"chat_id": chat["id"]}


# ---- push notifications (phones & computers) ----------------------------------------
@router.get("/push")
def push_status(user: dict = Depends(current_user)):  # noqa: B008
    return docs_push_status(user)


def docs_push_status(user: dict) -> dict:
    from .. import webpush
    st = webpush.status(user["id"])
    st.update({"delay": crm.get_setting("notify_delay", "2"), "escalate": crm.get_setting("notify_escalate", "10"),
               "push": crm.get_setting("notify_push", "1") == "1", "telegram": crm.get_setting("notify_telegram", "1") == "1"})
    return st


@router.post("/push/subscribe")
def push_subscribe(payload: dict, request: Request, user: dict = Depends(current_user)):  # noqa: B008
    from .. import webpush
    try:
        n = webpush.subscribe(user["id"], payload.get("subscription") or payload, request.headers.get("user-agent", ""))
    except ValueError as exc:
        _bad(exc)
    return {"ok": True, "mine": n}


@router.post("/push/unsubscribe")
def push_unsubscribe(payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    from .. import webpush
    webpush.unsubscribe(user["id"], payload.get("endpoint") or "")
    return {"ok": True}


@router.post("/push/test")
def push_test(user: dict = Depends(current_user)):  # noqa: B008
    from .. import webpush
    n = webpush.send_to([user["id"]], "Nova Home CRM", "Уведомления на этом устройстве работают", "/crm/#messages", "nh-test")
    return {"sent": n}


class NotifyIn(BaseModel):
    delay: int | None = None
    escalate: int | None = None
    push: bool | None = None
    telegram: bool | None = None


@router.post("/push/settings")
def push_settings(payload: NotifyIn, user: dict = Depends(admin_user)):  # noqa: B008
    if payload.delay is not None:
        crm.set_setting("notify_delay", str(max(0, min(payload.delay, 120))))
    if payload.escalate is not None:
        crm.set_setting("notify_escalate", str(max(0, min(payload.escalate, 240))))
    if payload.push is not None:
        crm.set_setting("notify_push", "1" if payload.push else "0")
    if payload.telegram is not None:
        crm.set_setting("notify_telegram", "1" if payload.telegram else "0")
    return {"ok": True}


# ---- documents (PDF) & email ----------------------------------------------------
class SelectorsIn(BaseModel):
    text: str


@router.post("/channels/booking_ext/selectors")
def set_ext_selectors(payload: SelectorsIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return booking_ext.set_selectors(payload.text)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"JSON не разобран: {exc}") from exc


@router.get("/company")
def get_company(user: dict = Depends(current_user)):  # noqa: B008
    return docs.company()


@router.post("/company")
def set_company(payload: dict, user: dict = Depends(admin_user)):  # noqa: B008
    return docs.set_company(payload)


@router.get("/bookings/{bid}/documents")
def booking_documents(bid: int, user: dict = Depends(current_user)):  # noqa: B008
    return docs.documents(booking_id=bid)


@router.post("/bookings/{bid}/documents")
def make_document(bid: int, payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return docs.create_document(bid, payload.get("kind") or "invoice", user["id"], payload.get("lang") or "ru", payload.get("amount"))
    except ValueError as exc:
        _bad(exc)


@router.get("/documents/{did}.pdf")
def document_pdf(did: int, user: dict = Depends(current_user)):  # noqa: B008
    d = docs.get_document(did)
    if not d:
        raise HTTPException(status_code=404, detail="Документ не найден")
    try:
        pdf = docs.render_pdf(d)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PDF не собрался: {exc}") from exc
    from fastapi.responses import Response as RawResponse
    return RawResponse(pdf, media_type="application/pdf",
                       headers={"Content-Disposition": f"inline; filename=\"{d['number']}.pdf\""})


@router.delete("/documents/{did}")
def del_document(did: int, user: dict = Depends(current_user)):  # noqa: B008
    docs.delete_document(did)
    return {"ok": True}


class EmailIn(BaseModel):
    to: str
    subject: str = ""
    text: str = ""
    doc_id: int | None = None
    booking_id: int | None = None
    client_id: int | None = None


@router.get("/email")
def email_status(user: dict = Depends(current_user)):  # noqa: B008
    return docs.email_status()


@router.post("/email")
def email_send(payload: EmailIn, user: dict = Depends(current_user)):  # noqa: B008
    att = []
    if payload.doc_id:
        d = docs.get_document(payload.doc_id)
        if not d:
            raise HTTPException(status_code=404, detail="Документ не найден")
        att.append((f"{d['number']}.pdf", docs.render_pdf(d), "application/pdf"))
    try:
        return docs.send_email(payload.to, payload.subject, payload.text, att, user["name"],
                               payload.booking_id, payload.client_id, payload.doc_id)
    except ValueError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


# ---- deals --------------------------------------------------------------------
class LinkIn(BaseModel):
    chat_id: int


@router.get("/deals")
def get_deals(pipeline: int | None = None, q: str = "", user: dict = Depends(current_user)):  # noqa: B008
    return crm.deals(pipeline, q=q)


@router.get("/kanban")
def kanban(pipeline: int, q: str = "", user: dict = Depends(current_user)):  # noqa: B008
    return crm_amo.kanban(pipeline, q)


@router.post("/deals")
def add_deal(payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        d = crm.save_deal(None, payload, user["id"])
    except ValueError as exc:
        _bad(exc)
    crm_amo.autolink(d["id"])
    return d


@router.get("/deals/{did}")
def get_deal(did: int, user: dict = Depends(current_user)):  # noqa: B008
    d = crm.get_deal(did)
    if not d:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    d["chats"] = crm_amo.deal_chats(did)
    with database.get_conn() as conn:
        d["log"] = [dict(r) for r in conn.execute(
            "SELECT * FROM crm_auto_log WHERE dedupe LIKE ? ORDER BY id DESC LIMIT 10", (f"stage:%:d{did}:%",)).fetchall()]
    return d


class NoteIn(BaseModel):
    text: str


@router.post("/deals/{did}/notes")
def deal_note(did: int, payload: NoteIn, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm_amo.add_note(payload.text, user["name"], deal_id=did)
    except ValueError as exc:
        _bad(exc)


@router.delete("/notes/{nid}")
def del_note(nid: int, user: dict = Depends(current_user)):  # noqa: B008
    crm_amo.delete_note(nid)
    return {"ok": True}


@router.post("/clients/{cid}/notes")
def client_note(cid: int, payload: NoteIn, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm_amo.add_note(payload.text, user["name"], client_id=cid)
    except ValueError as exc:
        _bad(exc)


@router.post("/deals/{did}/chats")
def deal_link_chat(did: int, payload: LinkIn, user: dict = Depends(current_user)):  # noqa: B008
    crm_amo.link_chat(did, payload.chat_id)
    return {"ok": True}


@router.delete("/deals/{did}/chats/{chat_id}")
def deal_unlink_chat(did: int, chat_id: int, user: dict = Depends(current_user)):  # noqa: B008
    crm_amo.unlink_chat(did, chat_id)
    return {"ok": True}


class DealSendIn(BaseModel):
    chat_id: int
    text: str


@router.post("/deals/{did}/send")
def deal_send(did: int, payload: DealSendIn, user: dict = Depends(current_user)):  # noqa: B008
    try:
        m = inbox.send(payload.chat_id, user["name"], payload.text[:4000])
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    crm_amo.link_chat(did, payload.chat_id)
    return m


@router.get("/unsorted")
def get_unsorted(user: dict = Depends(current_user)):  # noqa: B008
    return crm_amo.unsorted()


class AcceptIn(BaseModel):
    pipeline_id: int | None = None


@router.post("/unsorted/{chat_id}/accept")
def accept_unsorted(chat_id: int, payload: AcceptIn, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm_amo.accept(chat_id, user["id"], payload.pipeline_id)
    except ValueError as exc:
        _bad(exc)


@router.post("/unsorted/{chat_id}/reject")
def reject_unsorted(chat_id: int, user: dict = Depends(current_user)):  # noqa: B008
    crm_amo.reject(chat_id)
    return {"ok": True}


@router.get("/chats/{chat_id}/deal")
def chat_deal(chat_id: int, user: dict = Depends(current_user)):  # noqa: B008
    return {"deal_id": crm_amo.deal_for_chat(chat_id)}


@router.patch("/deals/{did}")
def patch_deal(did: int, payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm.save_deal(did, payload, user["id"])
    except ValueError as exc:
        _bad(exc)


@router.delete("/deals/{did}")
def del_deal(did: int, user: dict = Depends(current_user)):  # noqa: B008
    crm.delete_deal(did)
    return {"ok": True}


@router.post("/bookings/{bid}/deal")
def booking_deal(bid: int, user: dict = Depends(current_user)):  # noqa: B008
    try:
        d = crm.deal_for_booking(bid, user["id"])
    except ValueError as exc:
        _bad(exc)
    crm_amo.autolink(d["id"])
    return d


# ---- tasks --------------------------------------------------------------------
@router.get("/tasks")
def get_tasks(mine: int = 0, status: str = "open", booking: int = 0, group: str = "", user: dict = Depends(current_user)):  # noqa: B008
    if group == "booking":
        return crm.tasks_by_booking(user["id"] if mine else None, status)
    return crm.tasks(user["id"] if mine else None, status if status != "all" else None, booking_id=booking or None)


@router.post("/tasks")
def add_task(payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm.save_task(None, payload, user["id"])
    except ValueError as exc:
        _bad(exc)


@router.patch("/tasks/{tid}")
def patch_task(tid: int, payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm.save_task(tid, payload, user["id"])
    except ValueError as exc:
        _bad(exc)


@router.post("/tasks/{tid}/send")
def send_task_message(tid: int, user: dict = Depends(current_user)):  # noqa: B008
    """A checklist message task: send it to the guest now and close the task."""
    try:
        return crm_ext.send_task_message(tid, user["name"], force=True)
    except ValueError as exc:
        _bad(exc)


@router.delete("/tasks/{tid}")
def del_task(tid: int, user: dict = Depends(current_user)):  # noqa: B008
    crm.delete_task(tid)
    return {"ok": True}


# ---- bookings -----------------------------------------------------------------
@router.get("/bookings/{bid}/card")
def booking_card(bid: int, user: dict = Depends(current_user)):  # noqa: B008
    b = crm_ext.booking_card(bid)
    if not b:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    return b


@router.get("/bookings/{bid}/tasks")
def booking_tasks(bid: int, user: dict = Depends(current_user)):  # noqa: B008
    return crm.tasks(booking_id=bid)


@router.post("/bookings/{bid}/checklist")
def booking_checklist(bid: int, payload: dict | None = None, user: dict = Depends(current_user)):  # noqa: B008
    """Create the checklist tasks for a booking from the template (Интеграции → Чек-лист брони)."""
    payload = payload or {}
    try:
        return crm.create_checklist(bid, user["id"], payload.get("assignee_uid") or None, force=True)
    except ValueError as exc:
        _bad(exc)


@router.get("/checklist")
def checklist_settings(user: dict = Depends(current_user)):  # noqa: B008
    text = crm.get_setting("booking_checklist", crm.DEFAULT_CHECKLIST)
    return {"text": text, "default": crm.DEFAULT_CHECKLIST, "items": crm.parse_checklist(text),
            "auto": crm.get_setting("auto_checklist", "1") == "1"}


class ChecklistIn(BaseModel):
    text: str | None = None
    auto: bool | None = None


@router.post("/checklist")
def set_checklist(payload: ChecklistIn, user: dict = Depends(admin_user)):  # noqa: B008
    if payload.text is not None:
        crm.set_setting("booking_checklist", payload.text.strip())
    if payload.auto is not None:
        crm.set_setting("auto_checklist", "1" if payload.auto else "0")
    text = crm.get_setting("booking_checklist", crm.DEFAULT_CHECKLIST)
    return {"ok": True, "items": crm.parse_checklist(text)}


@router.patch("/bookings/{bid}")
def edit_booking(bid: int, payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    """Edit a booking: the RC fields go to RealtyCalendar (and the booking is
    re-read to confirm); everything else stays in the CRM."""
    try:
        return rc_push.update_booking(bid, payload, user["name"])
    except rc_push.RCError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


@router.post("/bookings/{bid}/chats")
def link_chat(bid: int, payload: LinkIn, user: dict = Depends(current_user)):  # noqa: B008
    try:
        crm_ext.link_chat(bid, payload.chat_id, user["name"])
    except ValueError as exc:
        _bad(exc)
    return {"ok": True}


@router.delete("/bookings/{bid}/chats/{chat_id}")
def unlink_chat(bid: int, chat_id: int, user: dict = Depends(current_user)):  # noqa: B008
    crm_ext.unlink_chat(bid, chat_id)
    return {"ok": True}


@router.get("/chats/search")
def chats_search(q: str = "", user: dict = Depends(current_user)):  # noqa: B008
    return [{k: c.get(k) for k in ("id", "title", "phone", "channel", "channel_name", "last_text", "last_at")}
            for c in inbox.chat_list(q, "all", "", 30)]


@router.get("/revenue")
def revenue(month: str = "", user: dict = Depends(current_user)):  # noqa: B008
    from datetime import date as _d
    return crm_ext.revenue(month[:7] if month else _d.today().strftime("%Y-%m"))


# ---- auto-message rules ---------------------------------------------------------
@router.get("/auto")
def get_rules(user: dict = Depends(current_user)):  # noqa: B008
    return {"rules": crm_ext.rules(), "triggers": crm_ext.TRIGGERS, "log": crm_ext.auto_log(100)}


@router.post("/auto")
def add_rule(payload: dict, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm_ext.save_rule(None, payload)
    except ValueError as exc:
        _bad(exc)


@router.put("/auto/{rid}")
def put_rule(rid: int, payload: dict, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm_ext.save_rule(rid, payload)
    except ValueError as exc:
        _bad(exc)


class ToggleIn(BaseModel):
    enabled: bool


@router.patch("/auto/{rid}")
def toggle_rule(rid: int, payload: ToggleIn, user: dict = Depends(admin_user)):  # noqa: B008
    crm_ext.toggle_rule(rid, payload.enabled)
    return {"ok": True}


@router.delete("/auto/{rid}")
def del_rule(rid: int, user: dict = Depends(admin_user)):  # noqa: B008
    crm_ext.delete_rule(rid)
    return {"ok": True}


@router.post("/auto/preview")
def preview_rules(user: dict = Depends(current_user)):  # noqa: B008
    """What the enabled booking rules would send right now (nothing is sent)."""
    return crm_ext.run_auto_rules(dry=True)


@router.post("/auto/run")
def run_rules(user: dict = Depends(admin_user)):  # noqa: B008
    return crm_ext.run_auto_rules()


@router.get("/bookings/day")
def bookings_day(date: str, user: dict = Depends(current_user)):  # noqa: B008
    return crm.bookings_day(date[:10])


@router.get("/bookings/grid")
def bookings_grid(start: str, days: int = 30, user: dict = Depends(current_user)):  # noqa: B008
    return crm.bookings_grid(start[:10], max(7, min(days, 90)))


# ---- quick commands -------------------------------------------------------------
class CommandIn(BaseModel):
    command: str = ""
    title: str = ""
    text: str


@router.get("/commands")
def get_commands(user: dict = Depends(current_user)):  # noqa: B008
    return crm.commands()


@router.post("/commands")
def add_command(payload: CommandIn, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm.save_command(None, payload.command, payload.title, payload.text)
    except ValueError as exc:
        _bad(exc)


@router.put("/commands/{tid}")
def put_command(tid: int, payload: CommandIn, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm.save_command(tid, payload.command, payload.title, payload.text)
    except ValueError as exc:
        _bad(exc)


@router.delete("/commands/{tid}")
def del_command(tid: int, user: dict = Depends(current_user)):  # noqa: B008
    crm.delete_command(tid)
    return {"ok": True}


# ---- channels & integrations ------------------------------------------------------
@router.get("/channels")
def channels(user: dict = Depends(current_user)):  # noqa: B008
    st = inbox.bridge_status()
    if user["role"] != "admin":
        st.pop("qr", None)
    with database.get_conn() as conn:
        counts = {r[0] or "wa": r[1] for r in conn.execute(
            "SELECT channel, COUNT(*) FROM inbox_chats GROUP BY channel").fetchall()}
    return {
        "wazzup": wazzup.status(),
        "booking_ext": booking_ext.status(),
        "whatsapp": {**st, "chats": counts.get("wa", 0)},
        "whatsapp_cloud": {**meta_api.wa_status(), "chats": counts.get("wac", 0)},
        "instagram": {**meta_api.ig_status(), "chats": counts.get("ig", 0)},
        "meta_webhook": {"url": meta_api.webhook_url(), "verify_token_set": bool(config.META_VERIFY_TOKEN),
                         "app_secret_set": bool(config.META_APP_SECRET)},
        "telegram": {**tg_channels.user_status(), "chats": counts.get("tg", 0)},
        "telegram_guest_bot": {**tg_channels.bot_status(), "chats": counts.get("tgbot", 0)},
        "telegram_bot": {"configured": bool(config.BOT_TOKEN), "notify_targets": config.inbox_notify_targets()},
    }


@router.post("/channels/wazzup/webhook")
def wazzup_register(user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return wazzup.register_webhook()
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


class TgPhoneIn(BaseModel):
    phone: str


class TgCodeIn(BaseModel):
    code: str = ""
    password: str = ""


@router.post("/channels/telegram/send_code")
def tg_send_code(payload: TgPhoneIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return tg_channels.user_send_code(payload.phone)
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


@router.post("/channels/telegram/sign_in")
def tg_sign_in(payload: TgCodeIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return tg_channels.user_sign_in(payload.code, payload.password)
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


@router.post("/channels/telegram/logout")
def tg_logout(user: dict = Depends(admin_user)):  # noqa: B008
    tg_channels.user_logout()
    return {"ok": True}


@router.post("/channels/whatsapp/logout")
def wa_logout(user: dict = Depends(admin_user)):  # noqa: B008
    try:
        inbox.bridge_logout()
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/integrations")
def integrations(user: dict = Depends(current_user)):  # noqa: B008
    with database.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM bookings WHERE COALESCE(is_delete, 0) = 0").fetchone()[0]
    return {
        "realtycalendar": {"configured": bool(config.RC_TOKEN), "demo": config.DEMO_MODE, "last_sync": database.last_sync(),
                           "bookings": n, "interval_min": config.SYNC_INTERVAL_MINUTES, "apartments": len(config.APARTMENTS),
                           "push_log": rc_push.recent_log(10)},
        "healthchecks": {"configured": bool(config.HEARTBEAT_URL_SERVER or config.HEARTBEAT_URL_BOT)},
        "telegram": {"configured": bool(config.BOT_TOKEN), "webapp_url": config.WEBAPP_URL},
        "settings": {"auto_tasks": crm.get_setting("auto_tasks", "1") == "1",
                     "auto_deal": crm.get_setting("auto_deal", "1") == "1",
                     "booking_flow": crm.get_setting("booking_flow", "1") == "1",
                     "rc_sync_clients": crm.get_setting("rc_sync_clients", "1") == "1",
                     "checkin_time": crm.default_times()[0], "checkout_time": crm.default_times()[1],
                     "auto_checklist": crm.get_setting("auto_checklist", "1") == "1",
                     "checklist": crm.get_setting("booking_checklist", crm.DEFAULT_CHECKLIST)},
    }


class SettingsIn(BaseModel):
    auto_tasks: bool | None = None
    auto_checklist: bool | None = None
    checklist: str | None = None
    auto_deal: bool | None = None
    booking_flow: bool | None = None
    rc_sync_clients: bool | None = None
    checkin_time: str | None = None
    checkout_time: str | None = None


@router.post("/integrations/settings")
def set_settings(payload: SettingsIn, user: dict = Depends(admin_user)):  # noqa: B008
    if payload.auto_tasks is not None:
        crm.set_setting("auto_tasks", "1" if payload.auto_tasks else "0")
    if payload.auto_checklist is not None:
        crm.set_setting("auto_checklist", "1" if payload.auto_checklist else "0")
    if payload.auto_deal is not None:
        crm.set_setting("auto_deal", "1" if payload.auto_deal else "0")
    if payload.booking_flow is not None:
        crm.set_setting("booking_flow", "1" if payload.booking_flow else "0")
    if payload.rc_sync_clients is not None:
        crm.set_setting("rc_sync_clients", "1" if payload.rc_sync_clients else "0")
    import re as _re
    for key, val in (("checkin_time", payload.checkin_time), ("checkout_time", payload.checkout_time)):
        if val is not None:
            if not _re.match(r"^\d{1,2}:\d{2}$", val.strip()):
                raise HTTPException(status_code=400, detail="Время в формате 14:00")
            crm.set_setting(key, val.strip())
    if payload.checklist is not None:
        crm.set_setting("booking_checklist", payload.checklist.strip())
    return {"ok": True, "items": crm.parse_checklist(crm.get_setting("booking_checklist", crm.DEFAULT_CHECKLIST))}


@router.post("/integrations/sync")
async def sync_now(user: dict = Depends(current_user)):  # noqa: B008
    try:
        n = await asyncio.get_event_loop().run_in_executor(None, rc_sync.sync_to_db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=424, detail=f"Синхронизация не удалась: {exc}") from exc
    try:
        await asyncio.get_event_loop().run_in_executor(None, crm.auto_tasks)
        await asyncio.get_event_loop().run_in_executor(None, crm.auto_checklists)
        await asyncio.get_event_loop().run_in_executor(None, crm_ext.sync_deals_from_bookings)
    except Exception:  # noqa: BLE001
        logger.exception("post-sync CRM refresh failed")
    return {"bookings": n, "last_sync": database.last_sync()}
