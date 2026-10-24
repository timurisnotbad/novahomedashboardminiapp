"""«Оплаты» — cash receipts written into the owner's Google Sheet.

Same access as the chats page: CRM cookie, personal link key from the bot
(`/оплата`), Telegram WebApp signature or the owner key. Everyone allowed
records payments; the review list, the audit and the month totals are the
owner's (CRM admin / OWNER_IDS)."""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from .. import cash, config
from .inbox import inbox_user, owner_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cash", tags=["cash"])


def _fail(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.exception("cash route failed")
    raise HTTPException(status_code=424, detail=config.redact(str(exc))[:300]) from exc


@router.get("/meta")
async def meta(force: int = 0, user: dict = Depends(inbox_user)):  # noqa: B008
    m = await asyncio.to_thread(cash.meta, bool(force))
    return {**m, "apartments": cash.apartment_list(), "me": {"name": user["name"], "owner": user["owner"]},
            "sheet": config.FINANCE_ENABLED, "edit_minutes": cash.EDIT_MINUTES, "article": cash.ARTICLE}


@router.get("/payments")
async def payments(day: str = "", month: str = "", mine: int = 0, user: dict = Depends(inbox_user)):  # noqa: B008
    rows = await asyncio.to_thread(cash.list_payments, day or None, month or None, user["uid"] if mine else None)
    for p in rows:
        p["can_edit"] = cash.may_edit(p, user)
    return {"payments": rows}


@router.post("/payments")
async def create(data: dict, user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        p = await asyncio.to_thread(cash.add, data, user, "crm" if user.get("crm") else "app", True)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    p["can_edit"] = True
    return p


@router.get("/payments/{pid}")
async def one(pid: int, user: dict = Depends(inbox_user)):  # noqa: B008
    p = await asyncio.to_thread(cash.get, pid)
    if not p:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    p["can_edit"] = cash.may_edit(p, user)
    p["audit"] = await asyncio.to_thread(cash.audit, 50, pid)
    return p


@router.patch("/payments/{pid}")
async def update(pid: int, data: dict, user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        p = await asyncio.to_thread(cash.edit, pid, data, user)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)
    p["can_edit"] = cash.may_edit(p, user)
    return p


@router.post("/payments/{pid}/cancel")
async def cancel(pid: int, data: dict, user: dict = Depends(inbox_user)):  # noqa: B008
    try:
        return await asyncio.to_thread(cash.cancel, pid, str(data.get("reason") or ""), user)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@router.post("/payments/{pid}/retry")
async def retry(pid: int, user: dict = Depends(inbox_user)):  # noqa: B008
    p = await asyncio.to_thread(cash.push_payment, pid)
    if not p:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    p["can_edit"] = cash.may_edit(p, user)
    return p


@router.post("/parse")
async def parse(data: dict, user: dict = Depends(inbox_user)):  # noqa: B008
    return await asyncio.to_thread(cash.parse_quick, str(data.get("text") or ""))


@router.get("/review")
async def review(user: dict = Depends(owner_user)):  # noqa: B008
    return {"payments": await asyncio.to_thread(cash.review_list)}


@router.post("/payments/{pid}/review")
async def resolve(pid: int, data: dict, user: dict = Depends(owner_user)):  # noqa: B008
    try:
        return await asyncio.to_thread(cash.resolve_review, pid, user, bool(data.get("as_debt")))
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@router.get("/stats")
async def stats(month: str = "", user: dict = Depends(owner_user)):  # noqa: B008
    return await asyncio.to_thread(cash.stats, month)


@router.get("/audit")
async def audit_log(limit: int = 100, user: dict = Depends(owner_user)):  # noqa: B008
    return {"audit": await asyncio.to_thread(cash.audit, min(max(limit, 1), 500))}
