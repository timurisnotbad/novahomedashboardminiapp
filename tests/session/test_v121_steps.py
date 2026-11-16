"""v121: stay-step tasks follow booking date changes; AI stops spending past the budget."""
import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date, timedelta
from backend import config, database, crm, inbox, crm_ext, ai
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db(); ai.init_db()
u = crm.create_user("A", "a121@x.uz", "secret123", "admin") if not crm.list_users() else crm.list_users()[0]
ci = (date.today() + timedelta(days=5)).isoformat(); co = (date.today() + timedelta(days=8)).isoformat()
with database.get_conn() as c:
    c.execute("INSERT INTO bookings (id, apartment_id, apartment_name, begin_date, end_date, status, client_name, client_phone, is_delete) VALUES (777121,1,'B-005',?,?,'booked','Ivan','998901234567',0)", (ci, co))
crm.create_checklist(777121, u["id"], u["id"])
def dues():
    with database.get_conn() as c:
        return {r["title"]: r["due"] for r in c.execute("SELECT title, due FROM crm_tasks WHERE booking_id = 777121")}
d1 = dues(); instr = [k for k in d1 if "инструкц" in k.lower()][0]
assert d1[instr].startswith(ci), d1[instr]
ci2 = (date.today() + timedelta(days=15)).isoformat(); co2 = (date.today() + timedelta(days=18)).isoformat()
with database.get_conn() as c:
    c.execute("UPDATE bookings SET begin_date = ?, end_date = ? WHERE id = 777121", (ci2, co2))
crm.auto_checklists()
d2 = dues()
assert d2[instr].startswith(ci2), d2[instr]
rem = (date.fromisoformat(co2) - timedelta(days=1)).isoformat()  # «Напомнить о выезде» = the day before check-out
assert all(v.startswith(rem) for k, v in d2.items() if "напомн" in k.lower()), d2
# a done task keeps its date
with database.get_conn() as c:
    c.execute("UPDATE crm_tasks SET status = 'done' WHERE booking_id = 777121 AND title = ?", (instr,))
    c.execute("UPDATE bookings SET begin_date = ? WHERE id = 777121", (ci,))
crm.auto_checklists(); assert dues()[instr].startswith(ci2)
# the AI: budget used up → no model call unless the button is pressed
calls = []
ai._triage = lambda ctx: calls.append("triage") or {"needs_reply": True}
ai.chat_mode = lambda chat: "draft"
ai._context = lambda cid: {"chat": {"id": cid}, "history": [{"id": 5, "direction": "in"}], "last_in": "x"}
ai.setting = (lambda orig: (lambda k: "10" if k == "ai_budget" else orig(k)))(ai.setting)
ai.month_cost = lambda: 12.0
st = []; ai._status = lambda cid, t: st.append(t)
assert ai.handle(1)["status"] == "budget" and not calls and "бюджет" in st[-1]
ai.month_cost = lambda: 1.0
ai._reply = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here"))
try: ai.handle(1)
except RuntimeError: pass
assert calls == ["triage"], calls
print("v121 steps/ai tests OK")
