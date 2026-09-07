from datetime import date, timedelta

from fastapi import APIRouter

from .. import services

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/today")
def dashboard_today():
    return services.build_day(date.today())


@router.get("/tomorrow")
def dashboard_tomorrow():
    return services.build_day(date.today() + timedelta(days=1))
