"""Nova Home CRM API (see backend/crm.py). Cookie session; admin-only routes
for users, pipelines, fields and integrations."""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import config, crm, database, inbox, rc_sync

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
    return {"user": u, "setup_needed": not crm.has_users(), "version": config.APP_VERSION}


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
    d = crm.home(user["id"])
    d["wa"] = inbox.bridge_status().get("status")
    return d


# ---- pipelines & fields -------------------------------------------------------
class PipelineIn(BaseModel):
    name: str
    stages: list[dict]


@router.get("/pipelines")
def get_pipelines(user: dict = Depends(current_user)):  # noqa: B008
    return crm.pipelines()


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


@router.get("/fields")
def get_fields(user: dict = Depends(current_user)):  # noqa: B008
    return crm.fields()


@router.post("/fields")
def add_field(payload: FieldIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm.save_field(None, payload.name, payload.type, payload.options)
    except ValueError as exc:
        _bad(exc)


@router.put("/fields/{fid}")
def put_field(fid: int, payload: FieldIn, user: dict = Depends(admin_user)):  # noqa: B008
    try:
        return crm.save_field(fid, payload.name, payload.type, payload.options)
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
def get_clients(q: str = "", user: dict = Depends(current_user)):  # noqa: B008
    return crm.clients(q)


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


@router.put("/clients/{cid}")
def put_client(cid: int, payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm.save_client(cid, payload)
    except ValueError as exc:
        _bad(exc)


@router.delete("/clients/{cid}")
def del_client(cid: int, user: dict = Depends(admin_user)):  # noqa: B008
    crm.delete_client(cid)
    return {"ok": True}


@router.post("/clients/{cid}/chat")
def client_chat(cid: int, user: dict = Depends(current_user)):  # noqa: B008
    """Open (or start) the WhatsApp chat of a client."""
    c = crm.get_client(cid)
    if not c:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    if c.get("chat_id"):
        return {"chat_id": c["chat_id"]}
    if not c.get("phone"):
        raise HTTPException(status_code=400, detail="У клиента нет телефона")
    try:
        chat = inbox.start_chat(c["phone"], c.get("name") or "")
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    with database.get_conn() as conn:
        conn.execute("UPDATE crm_clients SET chat_id = ? WHERE id = ?", (chat["id"], cid))
    return {"chat_id": chat["id"]}


# ---- deals --------------------------------------------------------------------
@router.get("/deals")
def get_deals(pipeline: int | None = None, q: str = "", user: dict = Depends(current_user)):  # noqa: B008
    return crm.deals(pipeline, q=q)


@router.post("/deals")
def add_deal(payload: dict, user: dict = Depends(current_user)):  # noqa: B008
    try:
        return crm.save_deal(None, payload, user["id"])
    except ValueError as exc:
        _bad(exc)


@router.get("/deals/{did}")
def get_deal(did: int, user: dict = Depends(current_user)):  # noqa: B008
    d = crm.get_deal(did)
    if not d:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    return d


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
        return crm.deal_for_booking(bid, user["id"])
    except ValueError as exc:
        _bad(exc)


# ---- tasks --------------------------------------------------------------------
@router.get("/tasks")
def get_tasks(mine: int = 0, status: str = "open", user: dict = Depends(current_user)):  # noqa: B008
    return crm.tasks(user["id"] if mine else None, status if status != "all" else None)


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


@router.delete("/tasks/{tid}")
def del_task(tid: int, user: dict = Depends(current_user)):  # noqa: B008
    crm.delete_task(tid)
    return {"ok": True}


# ---- bookings -----------------------------------------------------------------
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
        chats = conn.execute("SELECT COUNT(*) FROM inbox_chats").fetchone()[0]
    return {"whatsapp": {**st, "chats": chats},
            "telegram_bot": {"configured": bool(config.BOT_TOKEN), "notify_targets": config.inbox_notify_targets()}}


@router.post("/channels/whatsapp/logout")
def wa_logout(user: dict = Depends(admin_user)):  # noqa: B008
    try:
        inbox.bridge_logout()
    except inbox.BridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/integrations")
def integrations(user: dict = Depends(current_user)):  # noqa: B008
    with database.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM bookings WHERE COALESCE(is_delete, 0) = 0").fetchone()[0]
    return {
        "realtycalendar": {"configured": bool(config.RC_TOKEN), "demo": config.DEMO_MODE, "last_sync": database.last_sync(),
                           "bookings": n, "interval_min": config.SYNC_INTERVAL_MINUTES, "apartments": len(config.APARTMENTS)},
        "sheet": {"configured": config.FINANCE_ENABLED},
        "healthchecks": {"configured": bool(config.HEARTBEAT_URL_SERVER or config.HEARTBEAT_URL_BOT)},
        "telegram": {"configured": bool(config.BOT_TOKEN), "webapp_url": config.WEBAPP_URL},
        "settings": {"auto_tasks": crm.get_setting("auto_tasks", "1") == "1"},
    }


class SettingsIn(BaseModel):
    auto_tasks: bool | None = None


@router.post("/integrations/settings")
def set_settings(payload: SettingsIn, user: dict = Depends(admin_user)):  # noqa: B008
    if payload.auto_tasks is not None:
        crm.set_setting("auto_tasks", "1" if payload.auto_tasks else "0")
    return {"ok": True}


@router.post("/integrations/sync")
async def sync_now(user: dict = Depends(current_user)):  # noqa: B008
    try:
        n = await asyncio.get_event_loop().run_in_executor(None, rc_sync.sync_to_db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Синхронизация не удалась: {exc}") from exc
    try:
        await asyncio.get_event_loop().run_in_executor(None, crm.auto_tasks)
    except Exception:  # noqa: BLE001
        logger.exception("auto tasks failed")
    return {"bookings": n, "last_sync": database.last_sync()}
