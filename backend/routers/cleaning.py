from datetime import date

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import database, notify, services

router = APIRouter(prefix="/cleaning", tags=["cleaning"])

_CLEAN_MSG = {
    "in_progress": "🧹 Уборка начата: {apt}",
    "done": "✅ Уборка завершена: {apt}",
}


@router.get("")
def list_cleaning(
    date_str: str | None = Query(None, alias="date"),
    days: int = Query(2, ge=1, le=7),
):
    target = date.fromisoformat(date_str) if date_str else date.today()
    return services.build_cleaning(target, days=days)


class CleaningStatusUpdate(BaseModel):
    status: str
    date: str | None = None


@router.patch("/{apartment}/status")
def update_cleaning_status(apartment: str, body: CleaningStatusUpdate):
    cleaning_date = body.date or date.today().isoformat()
    # only today's cleanings can be marked started/done
    if cleaning_date != date.today().isoformat():
        return {"ok": False, "error": "only_today", "apartment": apartment,
                "cleaning_date": cleaning_date}
    database.set_cleaning_status(apartment, cleaning_date, body.status)
    msg = _CLEAN_MSG.get(body.status)
    if msg:
        notify.send(msg.format(apt=apartment))
    return {
        "apartment": apartment,
        "cleaning_date": cleaning_date,
        "status": body.status,
    }
