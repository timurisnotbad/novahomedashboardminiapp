"""Booking.com price monitor API (owner-only)."""
import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, Depends

from .. import auth, booking_prices

router = APIRouter(prefix="/prices", tags=["prices"], dependencies=[Depends(auth.owner_guard)])


def _parse(checkin: str, checkout: str):
    try:
        ci = date.fromisoformat(checkin) if checkin else date.today() + timedelta(days=1)
    except ValueError:
        ci = date.today() + timedelta(days=1)
    try:
        co = date.fromisoformat(checkout) if checkout else ci + timedelta(days=1)
    except ValueError:
        co = ci + timedelta(days=1)
    if co <= ci:
        co = ci + timedelta(days=1)
    return ci, co


@router.get("")
async def get_prices(checkin: str = "", checkout: str = "", refresh: int = 0):
    """Scrape (30–90 s) and return prices for our object + competitors.
    Runs the blocking scrape in a worker thread so the event loop stays free."""
    ci, co = _parse(checkin, checkout)
    data = await asyncio.to_thread(
        booking_prices.scrape_prices, ci, co, not bool(refresh)
    )
    return data
