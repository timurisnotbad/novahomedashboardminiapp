from datetime import date

from fastapi import APIRouter, Depends, Header, Query

from .. import auth, services

router = APIRouter(tags=["occupancy"])


@router.get("/occupancy")
def occupancy(
    date_str: str | None = Query(None, alias="date"),
    days: int = Query(7, ge=1, le=14),
    x_telegram_init_data: str = Header(default=""),  # noqa: B008
    x_owner_key: str = Header(default=""),  # noqa: B008
):
    try:
        target = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        target = date.today()
    data = services.build_occupancy(target, span=days)
    if not auth.is_owner(x_telegram_init_data, x_owner_key):
        for a in data.get("apartments", []):
            for d in a.get("days", []):
                d["amount_usd"] = None  # staff see who is in, not what they paid
    return data


@router.get("/guests", dependencies=[Depends(auth.owner_guard)])
def guests():
    return services.build_guests(date.today())
