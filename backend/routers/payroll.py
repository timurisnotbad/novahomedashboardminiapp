"""Payroll (табель): per-staff terms, month summary, payments. Owner-only."""
from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import auth, config, database

router = APIRouter(prefix="/payroll", tags=["payroll"], dependencies=[Depends(auth.owner_guard)])


def _month(ym: str | None) -> str:
    if ym and len(ym) == 7 and ym[4] == "-":
        return ym
    return date.today().strftime("%Y-%m")


@router.get("")
def payroll(month: str = ""):
    ym = _month(month)
    terms = {t["staff"]: t for t in database.all_staff_terms()}
    payments = database.salary_payments_month(ym)
    penalties = database.penalties_month(ym)
    attendance = database.attendance_month(ym)

    # union of names: terms + this month's activity + registry (suggestions)
    names = set(terms)
    names.update(p["staff"] for p in payments)
    names.update(p["staff"] for p in penalties)
    names.update(a["staff_name"] for a in attendance if a.get("staff_name"))
    registry = set()
    for r in database.all_staff():
        registry.add(f"@{r['username']}" if r.get("username") else (r.get("name") or ""))
    registry.discard("")

    rows = []
    for name in sorted(names, key=str.lower):
        t = terms.get(name, {})
        fines = sum(p["amount"] or 0 for p in penalties if p["staff"] == name and p["kind"] == "fine")
        bonus = sum(p["amount"] or 0 for p in penalties if p["staff"] == name and p["kind"] == "bonus")
        paid = sum(p["amount"] or 0 for p in payments if p["staff"] == name)
        att = [a for a in attendance if a.get("staff_name") == name]
        salary = t.get("salary") or 0
        due = salary + bonus - fines
        rows.append({
            "staff": name,
            "salary": salary,
            "pay_note": t.get("pay_note") or "",
            "fines": fines,
            "bonuses": bonus,
            "paid": paid,
            "due": due,
            "balance": due - paid,
            "days": len(att),
            "lates": sum(1 for a in att if not a.get("on_time")),
        })
    return {
        "month": ym,
        "rows": rows,
        "payments": payments,
        "names": sorted(names | registry, key=str.lower),
    }


class TermsIn(BaseModel):
    staff: str
    salary: float = 0
    pay_note: str = ""


@router.post("/terms")
def set_terms(body: TermsIn):
    database.set_staff_terms(body.staff.strip(), body.salary, body.pay_note.strip())
    return {"success": True}


class PaymentIn(BaseModel):
    staff: str
    amount: float
    note: str = ""
    at: str | None = None


@router.post("/pay")
def add_payment(body: PaymentIn):
    at = body.at or datetime.now().isoformat(timespec="minutes")
    pid = database.add_salary_payment(body.staff.strip(), body.amount, body.note.strip(), at)
    return {"success": True, "id": pid}


class PaymentEdit(BaseModel):
    staff: str
    amount: float
    note: str = ""


@router.patch("/pay/{payment_id}")
def edit_payment(payment_id: int, body: PaymentEdit):
    database.update_salary_payment(payment_id, body.staff.strip(), body.amount, body.note.strip())
    return {"success": True}


@router.delete("/pay/{payment_id}")
def del_payment(payment_id: int):
    database.delete_salary_payment(payment_id)
    return {"success": True}
