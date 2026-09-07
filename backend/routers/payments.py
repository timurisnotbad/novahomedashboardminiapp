from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import auth, database

router = APIRouter(prefix="/payments", tags=["payments"], dependencies=[Depends(auth.owner_guard)])


class PaymentIn(BaseModel):
    apartment_name: str
    amount: float
    currency: str = "UZS"
    method: str = "наличные"
    booking_id: int | None = None
    paid_at: str | None = None
    note: str = ""


@router.post("")
def record_payment(body: PaymentIn):
    payment = body.model_dump()
    if not payment.get("paid_at"):
        payment["paid_at"] = date.today().isoformat()
    payment_id = database.add_payment(payment)
    return {"success": True, "payment_id": payment_id, **payment}
