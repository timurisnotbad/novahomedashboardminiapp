from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from .. import auth, database, notify, services

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
    try:
        target = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        target = date.today()
    return services.build_cleaning(target, days=days)


class CleaningStatusUpdate(BaseModel):
    status: str
    date: str | None = None


@router.patch("/{apartment}/status", dependencies=[Depends(auth.owner_guard)])
def update_cleaning_status(apartment: str, body: CleaningStatusUpdate):
    """Manual override by the owner (the normal path is the bot's до/после
    reports)."""
    cleaning_date = body.date or date.today().isoformat()
    # only today's cleanings can be marked started/done
    if cleaning_date != date.today().isoformat():
        return {"ok": False, "error": "only_today", "apartment": apartment,
                "cleaning_date": cleaning_date}
    if body.status not in ("pending", "in_progress", "done"):
        return {"ok": False, "error": "bad_status"}
    database.set_cleaning_status(apartment, cleaning_date, body.status)
    msg = _CLEAN_MSG.get(body.status)
    if msg:
        notify.send(msg.format(apt=apartment), topic="cleaning")
    return {
        "apartment": apartment,
        "cleaning_date": cleaning_date,
        "status": body.status,
    }
