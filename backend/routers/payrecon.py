"""Payments reconciliation: channel posts vs PMS bookings."""
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import auth, database, services

router = APIRouter(prefix="/payrecon", tags=["payrecon"], dependencies=[Depends(auth.pay_guard)])


def _month(ym: str | None) -> str:
    if ym and len(ym) == 7 and ym[4] == "-":
        return ym
    return date.today().strftime("%Y-%m")


@router.get("")
def recon(month: str = ""):
    ym = _month(month)
    bookings = database.bookings_month(ym)
    by_id = {b["id"]: b for b in bookings}
    payments = database.channel_payments_month(ym)
    linked_ids = {p["booking_id"] for p in payments if p["booking_id"]}
    today = date.today().isoformat()

    def brief(b):
        return {
            "id": b["id"],
            "apartment": b.get("apartment_name"),
            "guest": b.get("client_name") or "Гость",
            "checkin": b.get("begin_date"),
            "checkout": b.get("end_date"),
            "amount_usd": b.get("amount"),
            "debt_usd": round(b.get("debt") or 0, 2),
            "source": services.source_name(b.get("source_id") if "source_id" in b.keys() else b.get("source")),
        }

    no_payment = [
        brief(b) for b in bookings
        if b["begin_date"] <= today and b["id"] not in linked_ids
    ]
    matched = []
    unmatched = []
    for p in payments:
        row = {
            "chat_id": p["chat_id"],
            "msg_id": p["msg_id"],
            "at": p["at"],
            "amount": p["amount"],
            "currency": p["currency"],
            "method": p["method"],
            "apartment": p["apartment"],
            "checkin": p["checkin"],
            "raw": (p["raw_text"] or "")[:120],
        }
        if p["booking_id"]:
            b = by_id.get(p["booking_id"])
            row["booking"] = brief(b) if b else {"id": p["booking_id"]}
            matched.append(row)
        else:
            unmatched.append(row)
    return {
        "month": ym,
        "no_payment": no_payment,
        "matched": matched,
        "unmatched": unmatched,
    }


class PayEdit(BaseModel):
    chat_id: int
    msg_id: int
    amount: float | None = None
    method: str | None = None


@router.patch("/item")
def edit_item(body: PayEdit):
    """Manual completion: fill in a missing amount or payment method."""
    database.update_channel_payment_fields(
        body.chat_id, body.msg_id, body.amount,
        body.method.strip() if body.method else None,
    )
    return {"success": True}
