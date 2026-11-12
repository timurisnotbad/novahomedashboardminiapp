import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import datetime, timedelta
from backend import database, inbox, crm, analytics
database.init_db(); inbox.init_db(); crm.init_db()
now = datetime.now().replace(second=0, microsecond=0)
T = lambda m: (now - timedelta(minutes=m)).isoformat()
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at, unread, last_dir) VALUES ('wa', 'a1@c.us', '1', 'A', ?, 1, 'in')", (T(5),))
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at, unread, last_dir) VALUES ('wa', 'a2@c.us', '2', 'B', ?, 0, 'out')", (T(5),))
    c1 = conn.execute("SELECT id FROM inbox_chats WHERE jid='a1@c.us'").fetchone()[0]; c2 = conn.execute("SELECT id FROM inbox_chats WHERE jid='a2@c.us'").fetchone()[0]
    msgs = [(c1, "in", None, T(300)), (c1, "in", None, T(290)), (c1, "out", "Диёр", T(280)),  # burst of 2 → Диёр answered in 20 min
            (c1, "in", None, T(200)), (c1, "out", "ИИ-ассистент", T(199)),                    # assistant answered in 1 min
            (c1, "in", None, T(5)),                                                             # unanswered now
            (c2, "in", None, T(1000)), (c2, "out", "Аня", T(880)), (c2, "out", "Аня", T(870)),  # Аня in 120 min, second reply is not a response
            (c2, "out", "Диёр", T(100))]                                                       # a message without a guest burst: reply, no response time
    for i, (cid, d, a, at) in enumerate(msgs):
        conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, status, at, rev) VALUES (?, ?, ?, ?, 'text', 'x', 'read', ?, 1)", (cid, f"m{i}", d, a, at))
s = analytics.manager_stats(7)
by = {m["name"]: m for m in s["managers"]}
assert by["Диёр"]["replies"] == 2 and by["Диёр"]["n"] == 1 and by["Диёр"]["median"] == 20.0 and by["Диёр"]["chats"] == 2, by["Диёр"]
assert by["Аня"]["replies"] == 2 and by["Аня"]["n"] == 1 and by["Аня"]["median"] == 120.0 and by["Аня"]["over60"] == 1 and by["Аня"]["within15"] == 0
assert by["ИИ-ассистент"]["system"] and by["ИИ-ассистент"]["median"] == 1.0
t = s["totals"]; assert t["guest_messages"] == 5 and t["bursts"] == 4 and t["answered"] == 3 and t["unanswered_now"] == 1 and t["people_replies"] == 4 and t["replies"] == 5, t
assert s["managers"][0]["name"] in ("Диёр", "Аня") and s["managers"][-1]["system"]
assert by["Диёр"]["first_reply_avg"] and len(by["Диёр"]["hours"]) == 24 and sum(by["Диёр"]["hours"]) == 2
print("v111 tests OK")
