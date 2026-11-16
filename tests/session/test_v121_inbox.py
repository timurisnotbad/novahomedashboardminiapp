"""v121 inbox fixes: echo filter, failed reply keeps the chat unanswered, retry in place, Booking re-offer, leads per thread."""
import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import database, inbox, crm, crm_ext, ai, booking_ext
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db(); ai.init_db(); booking_ext.init_db()
inbox.send_receipt = lambda chat_id, why="": inbox._mark_read_local(chat_id) if hasattr(inbox, "_mark_read_local") else None
SEQ = [0]; MODE = ["ok"]
def disp(ch, p):
    if MODE[0] == "fail": raise inbox.BridgeError("WhatsApp не подключён")
    SEQ[0] += 1; return {"id": "e-%d" % SEQ[0]}
inbox._dispatch_send = disp
def msg(i, out, text="", media=None, jid="998900000121@s.whatsapp.net"):
    return {"id": i, "jid": jid, "from_me": out, "push_name": "Guest", "at": inbox._now(), "kind": "image" if media else "text",
            "text": text, "media": media, "channel": "wa"}
inbox.store_message(msg("g1", False, "Здравствуйте"))
chat = inbox.find_chat_by_jid("998900000121@s.whatsapp.net")["id"] if hasattr(inbox, "find_chat_by_jid") else None
with database.get_conn() as conn:
    chat = conn.execute("SELECT id FROM inbox_chats WHERE jid = ?", ("998900000121@s.whatsapp.net",)).fetchone()[0]
def count(where="1=1"):
    with database.get_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM inbox_messages WHERE chat_id = ? AND {where}", (chat,)).fetchone()[0]
# phone sends two caption-less photos and «Ок» twice: four messages
for i, (t, md) in enumerate([("", "a.jpg"), ("", "b.jpg"), ("Ок", None), ("Ок", None)]):
    inbox.store_message(msg(f"p{i}", True, t, md))
assert count("direction = 'out'") == 4, count("direction = 'out'")
# a CRM reply and its echo from WhatsApp under another id: one message
inbox.send(chat, "Оператор", "Заезд с 14:00")
inbox.store_message(msg("echo1", True, "Заезд с 14:00"))
assert count("text = 'Заезд с 14:00'") == 1
# a failed reply leaves the chat «нужен ответ»
inbox.store_message(msg("g2", False, "Вы где?"))
with database.get_conn() as conn:
    before = dict(conn.execute("SELECT unread, notify_stage FROM inbox_chats WHERE id = ?", (chat,)).fetchone())
assert before["unread"] >= 1, before
MODE[0] = "fail"
try: inbox.send(chat, "Оператор", "Сейчас", client_id="r1"); raise SystemExit("no error")
except inbox.BridgeError: pass
with database.get_conn() as conn:
    after = dict(conn.execute("SELECT unread, notify_stage FROM inbox_chats WHERE id = ?", (chat,)).fetchone())
assert after["unread"] == before["unread"] and after["notify_stage"] == before["notify_stage"], (before, after)
# «повторить» keeps the client_id: the phone's repeat of the same reply finds it
with database.get_conn() as conn:
    fid = conn.execute("SELECT id FROM inbox_messages WHERE client_id = 'r1'").fetchone()[0]
MODE[0] = "ok"; n0 = SEQ[0]
new = inbox.retry(fid, "Оператор")
again = inbox.send(chat, "Оператор", "Сейчас", client_id="r1")
assert new["id"] == again["id"] and SEQ[0] == n0 + 1, (new, again, SEQ[0] - n0)
with database.get_conn() as conn:
    assert conn.execute("SELECT status FROM inbox_messages WHERE id = ?", (fid,)).fetchone()[0] == "retried"
# Booking: a reply taken by the extension and never confirmed is not offered again
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_ext_outbox (chat_id, text, status, taken_at) VALUES (?, 'hi', 'taken', '2020-01-01T00:00:00')", (chat,))
marked = []
inbox.mark_failed = lambda wa_id, err: marked.append(wa_id)
booking_ext.outbox()
with database.get_conn() as conn:
    st = conn.execute("SELECT status FROM inbox_ext_outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
assert st == "failed" and marked, (st, marked)
assert not [r for r in booking_ext.outbox() if r["text"] == "hi"]
# leads are per thread
import threading
inbox._leads().append((1, "", "", "", "", "")); seen = []
t = threading.Thread(target=lambda: seen.append(len(inbox._leads()))); t.start(); t.join()
assert seen == [0] and len(inbox._leads()) == 1; inbox._leads().clear()
print("v121 inbox tests OK")
