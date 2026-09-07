from datetime import date

from fastapi import APIRouter, Depends, Query

from .. import auth, services

router = APIRouter(tags=["occupancy"])


@router.get("/occupancy")
def occupancy(
    date_str: str | None = Query(None, alias="date"),
    days: int = Query(7, ge=1, le=14),
):
    target = date.fromisoformat(date_str) if date_str else date.today()
    return services.build_occupancy(target, span=days)


@router.get("/guests", dependencies=[Depends(auth.owner_guard)])
def guests():
    return services.build_guests(date.today())
