from fastapi import APIRouter, Depends, Query

from .. import auth, config, services, sheets

router = APIRouter(prefix="/finance", tags=["finance"], dependencies=[Depends(auth.owner_guard)])


@router.get("/balances")
def finance_balances(force: bool = Query(False)):
    """Cash balance per apartment + grand total, read from the Google Sheet."""
    if not config.FINANCE_ENABLED:
        return {"ok": False, "configured": False, "apartments": [], "total_usd": None}
    try:
        data = dict(sheets.fetch_balances(force=force))
    except Exception as exc:  # noqa: BLE001 - surface a friendly error to the UI
        return {"ok": False, "configured": True, "error": str(exc),
                "apartments": [], "total_usd": None}
    # which of the app's units the sheet did not report — tells the owner at a
    # glance that a new apartment is missing from «Счета» (or from the script)
    try:
        present = {str(a.get("code") or "").strip().upper() for a in data.get("apartments") or []}
        data["missing"] = [n for n in services.apartment_names() if n.upper() not in present]
    except Exception:  # noqa: BLE001
        data["missing"] = []
    return data
