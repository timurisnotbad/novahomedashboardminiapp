"""v119: a reply repeated by the browser (same client_id) is stored and sent once."""
import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
import threading, time
from backend import database, inbox, crm, crm_ext, ai
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db(); ai.init_db()
inbox.init_db()  # the migration is repeatable
CALLS = []; SEQ = [0]
def ok(channel, payload):
    CALLS.append(payload["text"]); time.sleep(0.4); SEQ[0] += 1; return {"id": "ext-%d" % SEQ[0]}
inbox._dispatch_send = ok
inbox.send_receipt = lambda *a, **k: None
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', '998900000119@s.whatsapp.net', '998900000119', 'G119', '2026-11-14T10:00:00')")
    chat = conn.execute("SELECT id FROM inbox_chats WHERE jid = '998900000119@s.whatsapp.net'").fetchone()[0]
def count():
    with database.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM inbox_messages WHERE chat_id = ?", (chat,)).fetchone()[0]

# 1. the same client_id twice → one message, one dispatch
a = inbox.send(chat, "Оператор", "Здравствуйте", client_id="cid-1")
b = inbox.send(chat, "Оператор", "Здравствуйте", client_id="cid-1")
assert a["id"] == b["id"] and a["status"] == "sent" and a["client_id"] == "cid-1", (a, b)
assert CALLS == ["Здравствуйте"] and count() == 1, (CALLS, count())

# 2. two requests at the same moment (the first is still on its way to WhatsApp)
CALLS.clear(); out = []
ts = [threading.Thread(target=lambda: out.append(inbox.send(chat, "Оператор", "Заезд в 14:00", client_id="cid-2"))) for _ in range(3)]
[t.start() for t in ts]; [t.join() for t in ts]
assert len({m["id"] for m in out}) == 1 and CALLS == ["Заезд в 14:00"] and count() == 2, (out, CALLS, count())
assert any(m["status"] == "pending" for m in out) or all(m["status"] == "sent" for m in out)

# 3. the channel refused: the repeat does not try again by itself, the red mark stays
def bad(channel, payload):
    CALLS.append("bad"); raise inbox.BridgeError("WhatsApp не подключён")
inbox._dispatch_send = bad; CALLS.clear()
try: inbox.send(chat, "Оператор", "ещё", client_id="cid-3"); raise SystemExit("no error")
except inbox.BridgeError: pass
r = inbox.send(chat, "Оператор", "ещё", client_id="cid-3")
assert r["status"] == "failed" and CALLS == ["bad"] and count() == 3, (r, CALLS)

# 4. without a client_id nothing changed: two sends = two messages
inbox._dispatch_send = ok; CALLS.clear()
inbox.send(chat, "Оператор", "раз"); inbox.send(chat, "Оператор", "раз")
assert len(CALLS) == 2 and count() == 5

# 5. odd characters are dropped, an empty id means «no id»
m = inbox.send(chat, "Оператор", "x", client_id="a b<>c'1"); assert m["client_id"] == "abc1", m
m = inbox.send(chat, "Оператор", "y", client_id="  "); assert m["client_id"] is None
m2 = inbox.send(chat, "Оператор", "y", client_id=""); assert m2["id"] != m["id"]

# 6. the «повторить» button of a failed message still makes a new message
inbox._dispatch_send = ok
n = inbox.retry(r["id"], "Оператор"); assert n["status"] == "sent" and n["id"] != r["id"]
print("v119 tests OK")
