import sys, os; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date, timedelta
from backend import database, crm, crm_ext, inbox, docs, config
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db(); docs.init_db()
d = lambda n: (date.today() + timedelta(days=n)).isoformat()
with database.get_conn() as conn:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()]
    row = {"id": 770001, "apartment_id": 3, "apartment_name": "B-003", "begin_date": d(5), "end_date": d(11), "client_name": "Reham Osman", "client_phone": "+971505200873",
           "status": "booked", "is_delete": 0, "amount": 570, "days_count": 6, "prepayment": 200}
    row = {k: v for k, v in row.items() if k in cols}
    conn.execute(f"INSERT OR REPLACE INTO bookings ({','.join(row)}) VALUES ({','.join('?'*len(row))})", list(row.values()))
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', 'reham@c.us', '971505200873', 'Reham Abdalla', ?)", (d(0) + "T10:00:00",))
    cid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'reham@c.us'").fetchone()[0]
SENT = []
inbox.send = lambda chat_id, author, text="", media=None, mime=None, file_name=None, quoted_id=None: SENT.append((chat_id, author, text, media, mime, file_name)) or {"id": len(SENT)}
inbox._phone_cache.update(at=0); inbox._pin_cache.update(at=0)
r = docs.send_to_chat(770001, cid, ["receipt", "confirmation"], 1, "Тимур")
assert len(r["sent"]) == 2 and not r["errors"] and r["lang"] == "en", r
assert SENT[0][3].startswith("doc") and SENT[0][3].endswith(".pdf") and SENT[0][4] == "application/pdf" and SENT[0][5].endswith(".pdf"), SENT[0]
assert SENT[0][2].startswith("Payment receipt No.") and SENT[1][2].startswith("Reservation confirmation No. 770001"), [s[2] for s in SENT]
assert (config.INBOX_MEDIA_DIR / SENT[0][3]).exists() and (config.INBOX_MEDIA_DIR / SENT[0][3]).read_bytes()[:4] == b"%PDF"
assert cid in crm_ext.chats_for_booking(770001)  # pinned by the send
r = docs.send_to_chat(770001, cid, ["invoice"], 1, "Тимур", lang="ru"); assert SENT[-1][2].startswith("Счёт на оплату №"), SENT[-1]
try: docs.send_to_chat(770001, cid, ["nope"], 1, "x"); raise AssertionError("bad kind")
except ValueError: pass
# a failed send leaves no orphan document
def boom(*a, **k): raise inbox.BridgeError("мост не запущен")
inbox.send = boom
before = len(docs.documents(booking_id=770001))
r = docs.send_to_chat(770001, cid, ["receipt"], 1, "Тимур"); assert r["errors"] and not r["sent"] and len(docs.documents(booking_id=770001)) == before, r
inbox.send = lambda chat_id, author, text="", media=None, mime=None, file_name=None, quoted_id=None: SENT.append((chat_id, author, text, media, mime, file_name)) or {"id": len(SENT)}
# route
config.DEV_MODE = True; config.BOT_TOKEN = ""
from fastapi.testclient import TestClient
from backend.main import app
with TestClient(app) as cl:
    r = cl.post(f"/api/inbox/chats/{cid}/documents", json={"kinds": ["receipt"]}); assert r.status_code == 200 and r.json()["sent"][0]["kind"] == "receipt", r.text
    with database.get_conn() as conn:
        conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', 'nob@c.us', '971500000111', 'Nobody Here', ?)", (d(0) + "T10:00:00",))
        nid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'nob@c.us'").fetchone()[0]
    inbox._name_cache.update(at=0)
    r = cl.post(f"/api/inbox/chats/{nid}/documents", json={"kinds": ["receipt"]}); assert r.status_code == 400 and "привяжите" in r.json()["detail"], r.text
print("v109 tests OK")
