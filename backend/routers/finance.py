from fastapi import APIRouter, Depends, Query

from .. import auth, config, sheets

router = APIRouter(prefix="/finance", tags=["finance"], dependencies=[Depends(auth.owner_guard)])


@router.get("/balances")
def finance_balances(force: bool = Query(False)):
    """Cash balance per apartment + grand total, read from the Google Sheet."""
    if not config.FINANCE_ENABLED:
        return {"ok": False, "configured": False, "apartments": [], "total_usd": None}
    try:
        return sheets.fetch_balances(force=force)
    except Exception as exc:  # noqa: BLE001 - surface a friendly error to the UI
        return {"ok": False, "configured": True, "error": str(exc),
                "apartments": [], "total_usd": None}
