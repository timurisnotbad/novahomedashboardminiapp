import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import database, inbox, crm, wazzup, config
database.init_db(); inbox.init_db(); crm.init_db(); wazzup.init_db()
config.WAZZUP_API_KEY = "k"; config.WEBAPP_URL = "https://crm.example.com"
wazzup.configured = lambda: True
wazzup.default_channel = lambda t: "ch1"
CALLS = []
wazzup._api = lambda method, path, timeout=30, json=None: CALLS.append(json) or {"messageId": "m%d" % len(CALLS)}
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wz', '998901234567@c.us', '998901234567', 'G', '2026-11-09T10:00:00')")
    cid = conn.execute("SELECT id FROM inbox_chats WHERE jid = '998901234567@c.us'").fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO wazzup_chats (chat_id, channel_id, chat_type, ext_chat_id) VALUES (?, 'ch1', 'whatsapp', '998901234567')", (cid,))
r = wazzup.send({"jid": "998901234567@c.us", "media": "RCP2026.pdf", "mime": "application/pdf", "file_name": "RCP-2026.pdf", "text": "Квитанция № RCP-2026"})
assert r["id"] == "wz:m1" and len(CALLS) == 2, (r, CALLS)
assert "contentUri" in CALLS[0] and "text" not in CALLS[0] and CALLS[0]["contentUri"].endswith("/media/RCP2026.pdf?s=" + inbox.media_sig("RCP2026.pdf")), CALLS[0]
assert CALLS[1] == {"channelId": "ch1", "chatType": "whatsapp", "chatId": "998901234567", "text": "Квитанция № RCP-2026"}, CALLS[1]
CALLS.clear(); wazzup.send({"jid": "998901234567@c.us", "text": "привет"}); assert len(CALLS) == 1 and CALLS[0]["text"] == "привет"
CALLS.clear(); wazzup.send({"jid": "998901234567@c.us", "media": "img1.jpg", "mime": "image/jpeg"}); assert len(CALLS) == 1 and "text" not in CALLS[0]
print("v110 tests OK")
