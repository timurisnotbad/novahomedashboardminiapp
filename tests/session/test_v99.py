import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import database, crm, crm_ext, inbox, config
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
SENT = []
inbox.send = lambda chat_id, author, text, *a, **k: SENT.append((chat_id, author, text)) or {"id": 1}
assert inbox.guest_language({"id": 0, "phone": "998901234567"}) == "ru"
assert inbox.guest_language({"id": 0, "phone": "971524507923"}) == "en"
assert inbox.guest_language({"id": 0, "phone": "79161234567"}) == "ru"
r = inbox.on_missed_call("971524507923@s.whatsapp.net", "CALL1", "timeout")
assert r["replied"] and r["lang"] == "en" and SENT[-1][1] == "Автоответ" and SENT[-1][2].startswith("Hello! We missed your call"), (r, SENT)
cid = r["chat_id"]
c = inbox.get_chat(cid); assert c["unread"] == 1 and c["last_text"].startswith("📞") and c["last_dir"] == "in", c
with database.get_conn() as conn:
    assert conn.execute("SELECT kind, text FROM inbox_messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()["kind"] == "call"
    # the fake send() stored nothing: emulate the auto-reply row for the cooldown check
    conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, status, at, rev) VALUES (?, 'x1', 'out', 'Автоответ', 'text', 'hi', 'sent', ?, 1)", (cid, inbox._now()))
assert inbox.on_missed_call("971524507923@s.whatsapp.net", "CALL1", "timeout").get("dup")  # same call twice
n = len(SENT); r = inbox.on_missed_call("971524507923@s.whatsapp.net", "CALL2", "reject"); assert not r["replied"] and len(SENT) == n  # cooldown 2 h
# russian number, custom text, switch off
crm.set_setting("missed_call_ru", "Перезвоним!")
r = inbox.on_missed_call("998901234567@s.whatsapp.net", "CALL3", "timeout"); assert r["lang"] == "ru" and SENT[-1][2] == "Перезвоним!", (r, SENT[-1])
# card language wins over the number
with database.get_conn() as conn:
    conn.execute("INSERT INTO crm_clients (name, phone, lang, created_at) VALUES ('Гость', '998907777777', 'EN', '2026-10-23')")
r = inbox.on_missed_call("998907777777@s.whatsapp.net", "CALL4", "timeout"); assert r["lang"] == "en", r
crm.set_setting("missed_call_reply", "0")
n = len(SENT); r = inbox.on_missed_call("971500000001@s.whatsapp.net", "CALL5", "timeout"); assert not r["replied"] and len(SENT) == n and inbox.get_chat(r["chat_id"])["unread"] == 1
# hook route
crm.set_setting("missed_call_reply", "1")
from fastapi.testclient import TestClient
from backend.main import app
with TestClient(app) as cl:
    r = cl.post("/api/inbox/hook", json={"event": "call", "jid": "971500000002@s.whatsapp.net", "id": "CALL6", "status": "timeout"}, headers={"X-Inbox-Secret": config.INBOX_SECRET})
    assert r.status_code == 200, r.text
    assert SENT[-1][2].startswith("Hello!")
    r = cl.get("/api/crm/integrations", cookies={}) ; assert r.status_code in (401, 403)
print("v99 tests OK")
