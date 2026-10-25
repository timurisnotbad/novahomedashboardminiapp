import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import database, crm, crm_ext, inbox, ai, alerts, config, notify
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db(); ai.init_db()
SENT = []
notify.send = lambda text, targets=None, topic=None, token=None: SENT.append((text, list(targets or [])))
config.OWNER_TELEGRAM_IDS = {1}; config.INBOX_NOTIFY_CHAT_IDS = [-100]
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', 'q@c.us', '77712000707', 'Данаш', '2026-10-25T10:00:00')")
    cid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'q@c.us'").fetchone()[0]
    conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, kind, text, status, at, rev) VALUES (?, 'g1', 'in', 'text', 'Ок бронируем', 'read', '2026-10-25T10:00:00', 1)", (cid,))
assert ai._guest_quote(cid) == "\n«Ок бронируем»"
# nobody linked: the assistant's notifications go nowhere (not to the group, not to the owner)
alerts.broadcast("ai", "🙋 test", base=[]); assert SENT == [], SENT
# a linked user with «ai» on gets it from the CRM bot only
admin = next(u for u in crm.list_users() if u["role"] == "admin")
with database.get_conn() as conn:
    conn.execute("UPDATE crm_users SET tg_id = 5001 WHERE id = ?", (admin["id"],))
alerts.broadcast("ai", "🙋 test", base=[]); assert SENT == [("🙋 test", [5001])], SENT
alerts.set_prefs(admin["id"], {"ai": False}); SENT.clear(); alerts.broadcast("ai", "🙋 test", base=[]); assert SENT == []
print("v102 tests OK")
