from fastapi import APIRouter, Query

from .. import database, services

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.get("")
def list_bookings(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    apartment: str | None = Query(None),
    status: str | None = Query(None),
    has_debt: bool | None = Query(None),
):
    rows = database.get_bookings(
        date_from=date_from,
        date_to=date_to,
        apartment=apartment,
        status=status,
        has_debt=has_debt,
    )
    return {"count": len(rows), "bookings": [services._booking_view(b) for b in rows]}
