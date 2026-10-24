import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date, timedelta
from backend import database, inbox, crm
database.init_db(); inbox.init_db(); crm.init_db()
today = date.today()
d = lambda n: (today + timedelta(days=n)).isoformat()
with database.get_conn() as conn:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()]
    base = {c: None for c in cols}
    def mk(i, apt, b, e, phone="998901234999"):
        row = dict(base, id=i, apartment_name=apt, begin_date=b, end_date=e, client_name="Тест Гость", client_phone="+" + phone, status="booked", is_delete=0, apartment_id=i, amount=100, days_count=2)
        row = {k: v for k, v in row.items() if k in cols}
        conn.execute(f"INSERT OR REPLACE INTO bookings ({','.join(row)}) VALUES ({','.join('?'*len(row))})", list(row.values()))
    mk(9000001, "B-008", d(1), d(6)); mk(9000002, "B-110", d(1), d(6)); mk(9000003, "A-066", d(10), d(12)); mk(9000004, "B-005", d(-30), d(-25))
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', 'stays-test@c.us', '998901234999', 'Тест', ?)", (d(0) + "T10:00:00",))
    cid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'stays-test@c.us'").fetchone()[0]
inbox._phone_cache.update(at=0); inbox._pin_cache.update(at=0)
st = inbox._stays_for_chat(cid, "998901234999")
assert [x["apartment"] for x in st] == ["B-008", "B-110", "A-066"] and all(x["when"] == "next" for x in st), st
# a past-only guest: the latest past stay
with database.get_conn() as conn:
    conn.execute("UPDATE bookings SET begin_date = ?, end_date = ? WHERE id IN (9000001, 9000002, 9000003)", (d(-10), d(-8)))
inbox._phone_cache.update(at=0)
st = inbox._stays_for_chat(cid, "998901234999"); assert len(st) == 1 and st[0]["when"] == "past" and st[0]["apartment"] in ("B-008", "B-110"), st
# pinned booking of another phone is included
with database.get_conn() as conn:
    conn.execute("UPDATE bookings SET begin_date = ?, end_date = ? WHERE id = 9000003", (d(2), d(4)))
    conn.execute("INSERT OR IGNORE INTO crm_booking_chats (booking_id, chat_id) VALUES (9000003, ?)", (cid,))
inbox._phone_cache.update(at=0); inbox._pin_cache.update(at=0)
st = inbox._stays_for_chat(cid, "")
assert [x["apartment"] for x in st] == ["A-066"], st
rows = inbox.chats_list(limit=5, q="Тест") if hasattr(inbox, "chats_list") else None
print("stays OK")
