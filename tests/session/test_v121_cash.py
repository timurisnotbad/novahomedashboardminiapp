"""v121 payments: failed rewrites/cancels retried in place, no duplicate appends, safe repeats, review claimed once."""
import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import config, database, crm, cash, inbox, crm_ext
config.FINANCE_ENABLED = True
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db(); cash.init_db()
SHEET = {}; KEYS = {}; nrow = [100]; fail = {"update": False, "add_lost": False, "debt": False}; calls = []
def fake(action, payload=None, timeout=40):
    calls.append(action)
    if action == "cash_meta": raise RuntimeError("no")
    if action == "cash_update":
        if fail["update"]: raise RuntimeError("quota")
        SHEET[payload["row"]] = payload["values"][5]; return {}
    if action == "cash_add":   # the script with keys: a line written before is not appended again
        rows = []
        for r, k in zip(payload["rows"], payload.get("keys") or [None] * len(payload["rows"])):
            if k and k in KEYS: rows.append(KEYS[k]); continue
            nrow[0] += 1; SHEET[nrow[0]] = r[5]; rows.append(nrow[0]); KEYS[k] = nrow[0]
        if fail["add_lost"]: raise RuntimeError("timeout")   # written, but the answer never came
        return {"rows": rows}
    if action == "cash_debt":
        if fail["debt"]: raise RuntimeError("quota")
        k = payload.get("key")
        if k in KEYS: return {"row": KEYS[k]}
        nrow[0] += 1; KEYS[k] = nrow[0]; SHEET[nrow[0]] = ("debt", payload["row"][3]); return {"row": nrow[0]}
    return {}
cash._sheet_call = fake; cash._notify = lambda *a, **k: None
owner = {"uid": 1, "name": "O", "owner": True}
total = lambda: round(sum(v for v in SHEET.values() if isinstance(v, (int, float))), 2)
# edit with a failing rewrite: no extra rows, the retry fixes the rows in place
p = cash.add({"apartments": ["B-005"], "lines": [{"amount": 100, "method": "Нал USD"}, {"amount": 50, "method": "Нал USD"}]}, owner)
assert total() == 150
fail["update"] = True; p = cash.edit(p["id"], {"lines": [{"amount": 120, "method": "Нал USD"}]}, owner)
assert total() == 150 and len(SHEET) == 2, SHEET              # nothing appended, the sheet still has the old values
fail["update"] = False; cash.retry_pending()
assert total() == 120 and len(SHEET) == 2, SHEET
# cancel with a failing rewrite is retried
q = cash.add({"apartments": ["B-005"], "lines": [{"amount": 70, "method": "Нал USD"}]}, owner)
fail["update"] = True; cash.cancel(q["id"], "ошибка", owner); assert total() == 190
fail["update"] = False; cash.retry_pending(); assert total() == 120, SHEET
# the answer of an append is lost: the retry does not append the same lines again
fail["add_lost"] = True; r = cash.add({"apartments": ["B-005"], "lines": [{"amount": 30, "method": "Нал USD"}]}, owner)
assert total() == 150 and r["lines"][0]["sheet_status"] == "error"
fail["add_lost"] = False; cash.retry_pending(); assert total() == 150 and len(SHEET) == 4, SHEET
assert cash.get(r["id"])["lines"][0]["sheet_status"] == "ok"
# the page repeats «Записать» after a timeout: one payment
a1 = cash.add({"apartments": ["B-005"], "lines": [{"amount": 10, "method": "Нал USD"}], "client_id": "form-1"}, owner)
a2 = cash.add({"apartments": ["B-005"], "lines": [{"amount": 10, "method": "Нал USD"}], "client_id": "form-1"}, owner)
assert a1["id"] == a2["id"] and total() == 160
# joint payment: double tap writes the debt once; a failed write can be pressed again without doubling
j = cash.add({"apartments": ["B-005", "B-006"], "lines": [{"amount": 200, "method": "Нал USD"}]}, owner)
assert j["status"] == "review"
fail["debt"] = True
try: cash.resolve_review(j["id"], owner, True); raise SystemExit("no error")
except RuntimeError: pass
assert cash.get(j["id"])["status"] == "review"
fail["debt"] = False; cash.resolve_review(j["id"], owner, True)
try: cash.resolve_review(j["id"], owner, True); raise SystemExit("second resolve allowed")
except ValueError: pass
assert len([v for v in SHEET.values() if isinstance(v, tuple)]) == 1, SHEET
print("v121 cash tests OK")
