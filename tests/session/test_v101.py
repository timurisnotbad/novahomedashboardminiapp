import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date, timedelta
from backend import database, crm, crm_ext, inbox, rc_sync, config
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
d = lambda n: (date.today() + timedelta(days=n)).isoformat()
# --- name keys
assert inbox.name_key("Nabeel  Arshad") == inbox.name_key("arshad nabeel") == "arshad nabeel"
assert inbox.name_key("Гость") == "" and inbox.name_key("Nabeel") == "" and inbox.name_key("Svetlana Rohrich (2 чел)") == "rohrich svetlana"
# --- booking without phone, chat with the same name
with database.get_conn() as conn:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()]
    row = {"id": 198253547, "apartment_id": 278, "apartment_name": "A-278", "begin_date": d(-1), "end_date": d(0), "client_name": "Nabeel Arshad", "client_phone": None,
           "status": "paid", "is_delete": 0, "amount": 125, "days_count": 1}
    conn.execute(f"INSERT OR REPLACE INTO bookings ({','.join(row)}) VALUES ({','.join('?'*len(row))})", list(row.values()))
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, push_name, last_at) VALUES ('wa', 'nabeel@c.us', '447878149604', 'Nabeel Arshad', 'Nabeel', ?)", (d(0) + "T10:00:00",))
    cid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'nabeel@c.us'").fetchone()[0]
inbox._phone_cache.update(at=0); inbox._name_cache.update(at=0); inbox._pin_cache.update(at=0)
c = inbox.get_chat(cid)
assert c["booking"] and c["booking"]["id"] == 198253547 and c["booking"]["by"] == "name" and c["booking"]["when"] == "now" and not c["booking"]["pinned"], c["booking"]
assert [x["id"] for x in c["stays"]] == [198253547]
# a phone match still wins over the name
with database.get_conn() as conn:
    row.update(id=198253548, apartment_name="B-005", client_phone="+447878149604", begin_date=d(3), end_date=d(5))
    conn.execute(f"INSERT OR REPLACE INTO bookings ({','.join(row)}) VALUES ({','.join('?'*len(row))})", list(row.values()))
inbox._phone_cache.update(at=0); inbox._name_cache.update(at=0)
c = inbox.get_chat(cid); assert c["booking"]["id"] == 198253548 and c["booking"]["by"] == "phone", c["booking"]
assert sorted(x["id"] for x in c["stays"]) == [198253548]  # by phone: only phone matches are listed
# booking card lists the chat by name when the booking has no phone
card = crm_ext.booking_card(198253547)
assert any(ch["id"] == cid and ch.get("by") == "name" for ch in card["chats"]), card["chats"]
# generic names never match
with database.get_conn() as conn:
    conn.execute("UPDATE bookings SET client_name = 'Гость' WHERE id = 198253547")
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', 'g@c.us', '971500000009', 'Гость', ?)", (d(0) + "T10:00:00",))
inbox._name_cache.update(at=0)
with database.get_conn() as conn:
    gid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'g@c.us'").fetchone()[0]
g = inbox.get_chat(gid); assert g["booking"] is None
# --- RC fetch: all statuses kept except cancelled; 422 -> fallback with the known list
import requests
CALLS = []
class Resp:
    def __init__(self, code, data): self.status_code, self._d = code, data
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)
    def json(self): return self._d
events = [{"id": 1, "status": "paid", "begin_date": d(0), "end_date": d(1), "client": {"fio": "A B"}},
          {"id": 2, "status": "check_in", "begin_date": d(0), "end_date": d(1), "client": {}},
          {"id": 3, "status": "cancelled", "begin_date": d(0), "end_date": d(1), "client": {}},
          {"id": 4, "status": "declined", "begin_date": d(0), "end_date": d(1), "client": {}, "is_delete": True}]
def fake_get(url, params=None, headers=None, timeout=30):
    CALLS.append(params)
    return Resp(200, {"items": [{"apartment_id": 278, "events": events}]})
requests.get = fake_get
config.RC_BASE_URL = "https://rc.test"
rows = rc_sync.fetch_bookings(date.today(), date.today() + timedelta(days=1))
assert [r["id"] for r in rows] == [1, 2] and "statuses[]" not in CALLS[-1], (rows, CALLS[-1])
def fake_get2(url, params=None, headers=None, timeout=30):
    CALLS.append(params)
    return Resp(422, {}) if "statuses[]" not in params else Resp(200, {"items": [{"apartment_id": 278, "events": events[:1]}]})
requests.get = fake_get2
rows = rc_sync.fetch_bookings(date.today(), date.today() + timedelta(days=1))
assert [r["id"] for r in rows] == [1] and CALLS[-1]["statuses[]"] == rc_sync.KNOWN_STATUSES
print("v101 tests OK")
