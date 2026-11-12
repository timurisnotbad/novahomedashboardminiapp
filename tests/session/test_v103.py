import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date, timedelta, datetime
from backend import database, crm, crm_ext, inbox
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
d = lambda n: (date.today() + timedelta(days=n)).isoformat()
with database.get_conn() as conn:
    row = {"id": 777001, "apartment_id": 5, "apartment_name": "B-005", "begin_date": d(1), "end_date": d(3), "client_name": "Test Person", "client_phone": "+998901239990",
           "status": "booked", "is_delete": 0, "amount": 100, "days_count": 2}
    conn.execute(f"INSERT OR REPLACE INTO bookings ({','.join(row)}) VALUES ({','.join('?'*len(row))})", list(row.values()))
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', 'tg1@c.us', '998901239990', 'Test Person', ?)", (d(0) + "T10:00:00",))
    cid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'tg1@c.us'").fetchone()[0]
inbox._phone_cache.update(at=0); inbox._name_cache.update(at=0); inbox._pin_cache.update(at=0)
def _b():
    with database.get_conn() as conn:
        return crm._booking_out(dict(conn.execute("SELECT * FROM bookings WHERE id = 777001").fetchone()))
b = _b()
rule = {"channel": "auto", "only_if_chat": True}
# chat matched by phone only -> waits for confirmation
chat, why = crm_ext._chat_for_booking(b, rule)
assert chat is None and why.startswith(crm_ext.UNCONFIRMED) and "по номеру" in why, (chat, why)
assert inbox.get_chat(cid)["booking"]["by"] == "phone" and not inbox.get_chat(cid)["booking"]["pinned"]
# a step message waits without burning a try
with database.get_conn() as conn:
    conn.execute("INSERT INTO crm_tasks (title, due, status, created_at, booking_id, auto_text) VALUES ('Напомнить', ?, 'open', ?, 777001, '{имя}, привет')",
                 (datetime.now().isoformat(timespec="minutes"), crm._now()))
    tid = conn.execute("SELECT id FROM crm_tasks WHERE booking_id = 777001").fetchone()[0]
r = crm_ext.send_task_message(tid)
assert r["status"] == "skipped" and r["error"].startswith(crm_ext.UNCONFIRMED), r
with database.get_conn() as conn:
    t = conn.execute("SELECT auto_tries, auto_status, status FROM crm_tasks WHERE id = ?", (tid,)).fetchone()
assert (t["auto_tries"] or 0) == 0 and t["status"] == "open" and t["auto_status"].startswith("skipped:" + crm_ext.UNCONFIRMED), dict(t)
# a person confirms (pins) -> the chat is used
crm_ext.link_chat(777001, cid, "Тимур"); inbox._pin_cache.update(at=0)
chat, why = crm_ext._chat_for_booking(b, rule); assert chat and chat["id"] == cid and why == ""
assert inbox.get_chat(cid)["booking"]["pinned"]
SENT = []; inbox.send = lambda chat_id, author, text, *a, **k: SENT.append((chat_id, text)) or {"id": 1}
assert crm_ext.send_task_message(tid)["status"] == "sent" and SENT[-1][0] == cid
# the setting off: the phone chat is used right away
crm_ext.unlink_chat(777001, cid); inbox._pin_cache.update(at=0)
crm.set_setting("auto_require_confirmed", "0")
chat, why = crm_ext._chat_for_booking(b, rule); assert chat and chat["id"] == cid
crm.set_setting("auto_require_confirmed", "1")
# name match when the booking has no phone
with database.get_conn() as conn:
    conn.execute("UPDATE bookings SET client_phone = NULL WHERE id = 777001")
b2 = _b()
found = inbox.booking_chats_auto(b2); assert found and found[0]["id"] == cid and found[0]["by"] == "name", found
chat, why = crm_ext._chat_for_booking(b2, rule); assert chat is None and "по имени" in why, why
# no match at all: the chat stays without a booking (nothing is guessed)
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', 'nomatch@c.us', '971500000777', 'Some Body', ?)", (d(0) + "T10:00:00",))
    nid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'nomatch@c.us'").fetchone()[0]
inbox._name_cache.update(at=0)
assert inbox.get_chat(nid)["booking"] is None and inbox.get_chat(nid)["stays"] == []
print("v103 tests OK")
