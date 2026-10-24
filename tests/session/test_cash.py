"""Cash receipts: parsing, sheet rows, joint payments, edit/cancel audit, retry, routes."""
import json, os, sys
sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date
from backend import cash, config, database, crm, inbox, util

database.init_db(); crm.init_db(); inbox.init_db(); cash.init_db()
config.SHEET_API_URL = "https://script.google.com/macros/s/x/exec"; config.SHEET_API_TOKEN = "t"; config.FINANCE_ENABLED = True
config.OWNER_TELEGRAM_IDS = set()

CALLS = []
SHEET = {"rows": [], "debt": [], "fail": False}
def fake_call(action, payload=None, timeout=40):
    CALLS.append((action, payload))
    if SHEET["fail"]:
        raise RuntimeError("HTTP 500 sheet down")
    if action == "cash_meta":
        return {"methods": [{"name": "Нал USD", "currency": "USD"}, {"name": "Нал UZS", "currency": "UZS"}, {"name": "Карта Хумо 4980", "currency": "UZS"},
                            {"name": "В приложении", "currency": "USD"}, {"name": "RUB", "currency": "RUB"}, {"name": "Нал EUR", "currency": "EUR"}],
                "rates": {"UZS": 12000, "EUR": 0.9, "RUB": 84}}
    if action == "cash_add":
        start = 100 + len(SHEET["rows"])
        SHEET["rows"].extend(payload["rows"])
        return {"rows": list(range(start, start + len(payload["rows"])))}
    if action == "cash_update":
        SHEET["rows"][payload["row"] - 100] = payload["values"]; return {"ok": True}
    if action == "cash_debt":
        SHEET["debt"].append(payload["row"]); return {"ok": True}
    raise AssertionError(action)
cash._sheet_call = fake_call
NOTES = []
cash._notify = lambda p, who, action, before=None: NOTES.append((action, p["id"]))

staff = {"uid": 777, "name": "Диёр", "owner": False}
owner = {"uid": 1, "name": "Тимур", "owner": True}

# --- meta from the sheet
m = cash.meta(force=True)
assert m["source"] == "sheet" and m["rates"]["UZS"] == 12000 and m["quick"] == ["Нал USD", "Нал UZS", "Карта Хумо 4980"], m
assert cash.currency_of("Карта Хумо 4980") == "UZS" and cash.to_usd(480000, "UZS") == 40.0 and cash.to_usd(90, "EUR") == 100.0

# --- stay parsing
ref = date(2026, 10, 21)
assert cash.parse_stay("19.10-23.10", ref) == {"checkin": "2026-10-19", "checkout": "2026-10-23", "label": "19-23.10"}
assert cash.parse_stay("19-23.10", ref)["label"] == "19-23.10"
assert cash.parse_stay("30.12 - 02.01", ref) == {"checkin": "2026-12-30", "checkout": "2027-01-02", "label": "30.12-02.01"}, cash.parse_stay("30.12 - 02.01", ref)
assert cash.parse_stay("ерунда", ref) is None

# --- quick text
q = cash.parse_quick("110 / 400$ / 480 000 / 19.10-23.10", ref)
assert q["apartments"] == ["B-110"] and [(l["amount"], l["currency"]) for l in q["lines"]] == [(400.0, "USD"), (480000.0, "UZS")] and q["stay"] == "19.10-23.10" and not q["missing"], q
q = cash.parse_quick("5 100$ хумо 1 200 000 21.10-22.10", ref)
assert q["apartments"] == [cash.apartment_by_num(5)], q
assert any(l["method"] == "Карта Хумо 4980" and l["amount"] == 1200000 for l in q["lines"]), q
q = cash.parse_quick("привет", ref)
assert q["missing"] == ["квартира", "сумма"], q
q = cash.parse_quick("кв 66 и 67, 600$", ref)
A12, A14 = "A-066", "A-067"
assert q["apartments"] == [A12, A14] and q["lines"][0]["amount"] == 600, q

# --- add: single apartment, two currencies -> two rows, pushed at once
p = cash.add({"apartments": ["110"], "lines": [{"amount": "400", "method": "Нал USD"}, {"amount": "480 000", "method": "Нал UZS"}], "stay": "19.10-23.10", "day": "2026-10-21"}, staff, source="app")
assert p["status"] == "ok" and p["sheet"] == "ok" and [l["sheet_row"] for l in p["lines"]] == [100, 101], p
assert p["total_usd"] == 440.0 and p["stay_label"] == "19-23.10"
row = SHEET["rows"][0]
assert row == [2026, "октября", "21.10.2026", 110, "Бронь", 400.0, 400.0, "Нал USD", "USD", "19-23.10"], row
assert SHEET["rows"][1][5:9] == [480000.0, 40.0, "Нал UZS", "UZS"]
assert NOTES[-1] == ("add", p["id"])
assert cash.may_edit(p, staff) and cash.may_edit(p, {"uid": 5, "owner": True}) and not cash.may_edit(p, {"uid": 5, "owner": False})

# --- joint payment: rows only on the receiver, status review, shares
j = cash.add({"apartments": ["66", "67"], "receiver": "67", "lines": [{"amount": 600, "method": "Нал USD"}], "stay": "21-23.10"}, staff)
assert j["status"] == "review" and j["joint"] == 1 and j["receiver"] == A14 and j["apartments"][0] == A14, j
assert j["shares"] == {A14: 300.0, A12: 300.0}, j["shares"]
assert SHEET["rows"][2][3] == 67 and "платёж №%d за кв. 67+66" % j["id"] in SHEET["rows"][2][9], SHEET["rows"][2]
assert len(SHEET["rows"]) == 3  # no row for apartment 12
assert [x["id"] for x in cash.review_list()] == [j["id"]]

# --- edit within 30 min by the same person: rows rewritten in place
e = cash.edit(p["id"], {"lines": [{"amount": 450, "method": "Нал USD"}], "note": "сдача"}, staff)
assert [l["sheet_row"] for l in e["lines"]] == [100, 101] and e["sheet"] == "ok", e
assert SHEET["rows"][0][5] == 450.0 and SHEET["rows"][0][9] == "19-23.10 · сдача", SHEET["rows"][0]
assert SHEET["rows"][1][5] == 0 and SHEET["rows"][1][9].startswith("ОТМЕНА (правка) · было 480000.0 UZS"), SHEET["rows"][1]
a = cash.audit(payment_id=p["id"])
assert [x["action"] for x in a] == ["edit", "add"] and a[0]["details"]["before"]["lines"][0]["amount"] == 400.0 and a[0]["who_name"] == "Диёр", a
# a stranger may not edit
try:
    cash.edit(p["id"], {"note": "x"}, {"uid": 9, "name": "Кто-то", "owner": False}); raise AssertionError("edit allowed")
except PermissionError:
    pass
# too old for the author, fine for the owner
with database.get_conn() as c:
    c.execute("UPDATE cash_payments SET created_at = '2026-10-21T08:00:00' WHERE id = ?", (p["id"],))
assert not cash.may_edit(cash.get(p["id"]), staff) and cash.may_edit(cash.get(p["id"]), owner)

# --- cancel: nothing deleted, rows zeroed with the reason
try:
    cash.cancel(p["id"], "", owner); raise AssertionError("cancel without reason")
except ValueError:
    pass
cc = cash.cancel(p["id"], "гость передумал", owner)
assert cc["status"] == "cancelled" and SHEET["rows"][0][5] == 0 and SHEET["rows"][0][9].startswith("ОТМЕНА (гость передумал) · было 450.0 USD"), SHEET["rows"][0]
assert cash.get(p["id"])["lines"][0]["amount"] == 450.0  # the DB keeps the amount
assert [x["action"] for x in cash.audit(payment_id=p["id"])][0] == "cancel"
assert NOTES[-1] == ("cancel", p["id"])

# --- review decision: debt rows between apartments
try:
    cash.resolve_review(j["id"], staff, True); raise AssertionError("staff resolved")
except PermissionError:
    pass
r = cash.resolve_review(j["id"], owner, True)
assert r["status"] == "ok" and SHEET["debt"] == [[date.today().strftime("%d.%m.%Y"), 67, 66, 300.0, f"платёж №{j['id']} · 21-23.10"]], SHEET["debt"]
assert cash.review_list() == []

# --- sheet down: payment kept, pending; retry writes it later
SHEET["fail"] = True
w = cash.add({"apartments": ["7"], "lines": [{"amount": 1000000, "method": "Карта Хумо 4980"}]}, staff)
assert w["sheet"] == "error" and "500" in w["lines"][0]["sheet_error"] and w["status"] == "ok", w
assert cash.retry_pending() == 0
SHEET["fail"] = False
assert cash.retry_pending() == 1 and cash.get(w["id"])["sheet"] == "ok" and SHEET["rows"][-1][3] == 7 and SHEET["rows"][-1][6] == 83.33

# --- stats
st = cash.stats("2026-10")
assert st["count"] >= 2 and st["cancelled"] == 1 and st["review"] == 0 and any(m["method"] == "Карта Хумо 4980" for m in st["by_method"]), st
assert st["total_usd"] == round(600 + 83.33, 2), st["total_usd"]

# --- validation
for bad, msg in (({"apartments": [], "lines": [{"amount": 1, "method": "Нал USD"}]}, "квартира"), ({"apartments": ["5"], "lines": []}, "сумм"),
                 ({"apartments": ["5"], "lines": [{"amount": 5, "method": ""}]}, "счёт"), ({"apartments": ["5"], "lines": [{"amount": 5, "method": "Нал USD"}], "stay": "вчера"}, "19.10-23.10"), ({"apartments": ["999"], "lines": [{"amount": 5, "method": "Нал USD"}]}, "квартира")):
    try:
        cash.add(bad, staff); raise AssertionError(bad)
    except ValueError as exc:
        assert msg in str(exc), (bad, exc)

# --- routes (dev mode = owner)
config.DEV_MODE = True; config.BOT_TOKEN = ""
from fastapi.testclient import TestClient
from backend.main import app
with TestClient(app) as cl:
    r = cl.get("/api/cash/meta"); assert r.status_code == 200 and r.json()["apartments"][0]["label"] and r.json()["me"]["owner"], r.text
    r = cl.post("/api/cash/parse", json={"text": "31 200$ 1-3.11"}); assert r.json()["apartments"] == ["B-031"], r.text
    r = cl.post("/api/cash/payments", json={"apartments": ["31"], "lines": [{"amount": 200, "method": "Нал USD"}], "stay": "1-3.11"})
    assert r.status_code == 200 and r.json()["sheet"] == "ok" and r.json()["can_edit"], r.text
    pid = r.json()["id"]
    r = cl.get("/api/cash/payments?day=" + date.today().isoformat()); assert any(x["id"] == pid for x in r.json()["payments"])
    r = cl.patch(f"/api/cash/payments/{pid}", json={"lines": [{"amount": 250, "method": "Нал USD"}]}); assert r.json()["total_usd"] == 250, r.text
    r = cl.post(f"/api/cash/payments/{pid}/cancel", json={"reason": "ab"}); assert r.status_code == 400, r.text
    r = cl.post(f"/api/cash/payments/{pid}/cancel", json={"reason": "ошибка ввода"}); assert r.json()["status"] == "cancelled", r.text
    r = cl.get("/api/cash/stats"); assert r.status_code == 200 and "by_method" in r.json()
    r = cl.get("/api/cash/audit"); assert r.json()["audit"][0]["action"] == "cancel"
    r = cl.get(f"/api/cash/payments/{pid}"); assert len(r.json()["audit"]) == 3
    r = cl.get("/api/cash/review"); assert r.json() == {"payments": []}
    r = cl.get("/cash/"); assert r.status_code == 200 and "cash.js?v=" in r.text, r.status_code
print("cash tests OK", len(CALLS), "sheet calls")
