"""Discipline log: fines and bonuses per staff member. Owner-only."""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import auth, config, database

router = APIRouter(prefix="/penalties", tags=["penalties"], dependencies=[Depends(auth.owner_guard)])


class PenaltyIn(BaseModel):
    kind: str  # "fine" | "bonus"
    staff: str
    amount: float
    reason: str = ""
    at: str | None = None  # ISO datetime; default now


@router.get("")
def list_penalties():
    names = set(config.STAFF.values())
    for r in database.all_staff():
        names.add(f"@{r['username']}" if r.get("username") else (r.get("name") or ""))
    names.discard("")
    return {
        "records": database.all_penalties(),
        "staff": sorted(names),
    }


@router.post("")
def add_penalty(body: PenaltyIn):
    kind = body.kind if body.kind in ("fine", "bonus") else "fine"
    at = body.at or datetime.now().isoformat(timespec="minutes")
    pid = database.add_penalty(kind, body.staff.strip(), body.amount, body.reason.strip(), at)
    return {"success": True, "id": pid}


@router.delete("/{penalty_id}")
def remove_penalty(penalty_id: int):
    database.delete_penalty(penalty_id)
    return {"success": True}
