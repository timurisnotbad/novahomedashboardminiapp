from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel

from .. import database, notify, reminders, services

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskIn(BaseModel):
    title: str
    apartment: str | None = None
    deadline: str | None = None       # YYYY-MM-DD
    deadline_time: str | None = None  # HH:MM


class StatusIn(BaseModel):
    status: str  # open | done


def _label(apartment, title, deadline=None, deadline_time=None):
    line = (f"{apartment}: " if apartment else "") + title
    if deadline:
        line += f"\nСрок: {deadline}" + (f" {deadline_time}" if deadline_time else "")
    return line


@router.get("")
def get_tasks():
    return services.build_tasks(date.today())


@router.post("")
def create_task(payload: TaskIn):
    title = (payload.title or "").strip()
    if not title:
        return {"ok": False, "error": "empty_title"}
    apartment = payload.apartment or None
    # only accept a real apartment code, otherwise treat as a general task
    if apartment and apartment not in services.apartment_names():
        apartment = None
    dtime = (payload.deadline_time or "").strip() or None
    tid = database.add_task(apartment, title, payload.deadline or None, dtime)
    reminders.prime_task(tid)  # suppress reminders whose window already passed
    notify.send("🆕 Новая задача\n" + _label(apartment, title, payload.deadline, dtime))
    return {"ok": True, "id": tid}


@router.patch("/{task_id}")
def update_task(task_id: int, payload: StatusIn):
    status = "done" if payload.status == "done" else "open"
    database.set_task_status(task_id, status)
    if status == "done":
        t = database.get_task(task_id)
        if t:
            notify.send("✅ Задача выполнена\n" + _label(t.get("apartment_name"), t.get("title")))
    return {"ok": True}


@router.delete("/{task_id}")
def remove_task(task_id: int):
    database.delete_task(task_id)
    return {"ok": True}
