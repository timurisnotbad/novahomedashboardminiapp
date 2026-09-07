from datetime import date, timedelta

from fastapi import APIRouter, Header

from .. import auth, services

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_MONEY_KEYS = ("amount_usd", "debt_usd", "is_paid", "source", "source_id")


def _for_role(payload: dict, init: str, okey: str) -> dict:
    """Staff get the operational view only: no cash figures, no debtors. The
    frontend hides these blocks too, but hiding must happen server-side."""
    if auth.is_owner(init, okey):
        return payload
    payload = dict(payload)
    payload["finance"] = {"expected_today_usd": 0, "debtors_count": 0, "debtors_total_usd": 0}
    payload["debtors"] = []
    payload["checkins"] = [{k: v for k, v in b.items() if k not in _MONEY_KEYS}
                           for b in payload.get("checkins", [])]
    payload["new_bookings_24h"] = [{k: v for k, v in b.items() if k not in _MONEY_KEYS}
                                   for b in payload.get("new_bookings_24h", [])]
    return payload


@router.get("/today")
def dashboard_today(
    x_telegram_init_data: str = Header(default=""),  # noqa: B008
    x_owner_key: str = Header(default=""),  # noqa: B008
):
    return _for_role(services.build_day(date.today()), x_telegram_init_data, x_owner_key)


@router.get("/tomorrow")
def dashboard_tomorrow(
    x_telegram_init_data: str = Header(default=""),  # noqa: B008
    x_owner_key: str = Header(default=""),  # noqa: B008
):
    return _for_role(services.build_day(date.today() + timedelta(days=1)),
                     x_telegram_init_data, x_owner_key)
