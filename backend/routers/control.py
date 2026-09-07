"""«Контроль» (owner-only): attendance statistics, cleaning-session
statistics (durations, travel time between apartments) and the shopping list."""
from datetime import date, datetime
from statistics import median

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import auth, database, supplies

router = APIRouter(prefix="/control", tags=["control"], dependencies=[Depends(auth.owner_guard)])


def _month(ym: str | None) -> str:
    if ym and len(ym) == 7 and ym[4] == "-":
        return ym
    return date.today().strftime("%Y-%m")


def _hhmm(ts: str | None) -> str | None:
    return (ts or "")[11:16] or None


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------
def _att_status(r: dict) -> str:
    st = r.get("status")
    if st:
        return st
    if not r.get("arrived_at"):
        return "absent"
    return "ok" if r.get("on_time") else "late"


@router.get("/attendance")
def attendance_stats(month: str = ""):
    ym = _month(month)
    rows = database.attendance_month(ym)
    per: dict[str, dict] = {}
    for r in rows:
        name = r.get("staff_name") or str(r.get("staff_id"))
        p = per.setdefault(name, {"staff": name, "ok": 0, "late": 0, "late_min": 0,
                                  "absent": 0, "arrivals": []})
        st = _att_status(r)
        p[st if st in ("ok", "late", "absent") else "ok"] += 1
        if st == "late":
            p["late_min"] += int(r.get("late_minutes") or 0)
        if r.get("arrived_at"):
            t = r["arrived_at"][11:16]
            try:
                hh, mm = (int(x) for x in t.split(":"))
                p["arrivals"].append(hh * 60 + mm)
            except ValueError:
                pass
    summary = []
    for p in sorted(per.values(), key=lambda x: x["staff"].lower()):
        avg = None
        if p["arrivals"]:
            m = int(sum(p["arrivals"]) / len(p["arrivals"]))
            avg = f"{m // 60:02d}:{m % 60:02d}"
        summary.append({
            "staff": p["staff"], "days": p["ok"] + p["late"], "ok": p["ok"],
            "late": p["late"], "late_min": p["late_min"], "absent": p["absent"],
            "avg_arrival": avg,
        })
    log = [{
        "date": r.get("work_date"), "staff": r.get("staff_name") or str(r.get("staff_id")),
        "time": _hhmm(r.get("arrived_at")), "status": _att_status(r),
        "late_minutes": int(r.get("late_minutes") or 0),
    } for r in rows]
    log.sort(key=lambda x: (x["date"] or "", x["time"] or "99:99"), reverse=True)
    return {"month": ym, "rows": summary, "log": log}


# ---------------------------------------------------------------------------
# Cleaning sessions
# ---------------------------------------------------------------------------
def _stats(values: list[int]) -> dict:
    if not values:
        return {"avg": None, "med": None, "min": None, "max": None}
    return {
        "avg": int(round(sum(values) / len(values))),
        "med": int(round(median(values))),
        "min": min(values), "max": max(values),
    }


@router.get("/cleaning")
def cleaning_stats(month: str = ""):
    ym = _month(month)
    sessions = database.cleaning_sessions_month(ym)
    by_apt: dict[str, dict] = {}
    by_staff: dict[str, dict] = {}
    for s in sessions:
        apt = s.get("apartment") or "—"
        who = s.get("staff_name") or str(s.get("staff_id") or "—")
        dur = s.get("duration_min")
        trav = s.get("travel_min")
        a = by_apt.setdefault(apt, {"apartment": apt, "count": 0, "durations": []})
        a["count"] += 1
        if dur is not None and not s.get("forced"):
            a["durations"].append(int(dur))
        p = by_staff.setdefault(who, {"staff": who, "count": 0, "durations": [], "travels": [],
                                      "no_before": 0, "forced": 0})
        p["count"] += 1
        if dur is not None and not s.get("forced"):
            p["durations"].append(int(dur))
        if trav is not None:
            p["travels"].append(int(trav))
        if s.get("no_before"):
            p["no_before"] += 1
        if s.get("forced"):
            p["forced"] += 1
    apts = []
    for a in sorted(by_apt.values(), key=lambda x: x["apartment"]):
        apts.append({"apartment": a["apartment"], "count": a["count"], **_stats(a["durations"])})
    staff = []
    for p in sorted(by_staff.values(), key=lambda x: x["staff"].lower()):
        st = _stats(p["durations"])
        tr = _stats(p["travels"])
        staff.append({
            "staff": p["staff"], "count": p["count"], "timed": len(p["durations"]),
            "avg": st["avg"], "med": st["med"], "min": st["min"], "max": st["max"],
            "travel_avg": tr["avg"], "travel_max": tr["max"], "travel_n": len(p["travels"]),
            "no_before": p["no_before"], "forced": p["forced"],
        })
    log = [{
        "id": s.get("id"), "date": s.get("work_date"), "apartment": s.get("apartment"),
        "staff": s.get("staff_name") or "", "start": _hhmm(s.get("started_at")),
        "end": _hhmm(s.get("finished_at")), "duration_min": s.get("duration_min"),
        "travel_min": s.get("travel_min"), "no_before": bool(s.get("no_before")),
        "forced": bool(s.get("forced")), "open": s.get("finished_at") is None,
    } for s in sessions]
    all_d = [int(s["duration_min"]) for s in sessions
             if s.get("duration_min") is not None and not s.get("forced")]
    return {"month": ym, "total": len(sessions), **_stats(all_d),
            "apartments": apts, "staff": staff, "log": log}


# ---------------------------------------------------------------------------
# Supplies
# ---------------------------------------------------------------------------
@router.get("/supplies")
def list_supplies():
    open_rows = database.open_supplies()
    return {
        "open": open_rows,
        "summary": supplies.aggregate(open_rows),
        "bought": database.bought_supplies(),
    }


class SupplyIn(BaseModel):
    item: str
    qty: int = 1
    apartment: str | None = None


@router.post("/supplies")
def add_supply(body: SupplyIn):
    item = body.item.strip()
    if not item:
        return {"success": False}
    sid = database.add_supply(body.apartment or None, item, body.qty, "владелец",
                              datetime.now().isoformat(timespec="minutes"))
    return {"success": True, "id": sid}


class SupplyPatch(BaseModel):
    bought: bool


@router.patch("/supplies/{supply_id}")
def patch_supply(supply_id: int, body: SupplyPatch):
    database.set_supply_bought(supply_id, body.bought)
    return {"success": True}


@router.delete("/supplies/{supply_id}")
def delete_supply(supply_id: int):
    database.delete_supply(supply_id)
    return {"success": True}
