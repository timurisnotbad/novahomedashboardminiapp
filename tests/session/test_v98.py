import sys, json; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import datetime, timedelta
from backend import database, crm, crm_ext, inbox, alerts, config, notify
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
SENT = []
notify.send = lambda text, targets=None, topic=None, token=None: SENT.append((text, list(targets or []), topic))
config.OWNER_TELEGRAM_IDS = {1}
users = crm.list_users(); admin = next(u for u in users if u["role"] == "admin")
with database.get_conn() as conn:
    conn.execute("UPDATE crm_users SET tg_id = 5001 WHERE id = ?", (admin["id"],))
    conn.execute("INSERT INTO crm_users (name, email, pass_hash, role, active, tver, created_at, tg_id) VALUES ('Менеджер', 'm@x.uz', 'x', 'manager', 1, 1, '2026-01-01', 5002)")
    mid = conn.execute("SELECT id FROM crm_users WHERE email = 'm@x.uz'").fetchone()[0]
# prefs: default all on; admin-only kinds skip managers
assert sorted(alerts.targets("tasks")) == [5001, 5002] and alerts.targets("cash") == [5001]
alerts.set_prefs(mid, {"tasks": False}); assert alerts.targets("tasks") == [5001]
assert alerts.targets("messages", [mid]) == [5002]
n0 = len(SENT); alerts.broadcast("bookings", "новая бронь", base=[1])
assert sorted(sum((x[1] for x in SENT[n0:]), [])) == [1, 5001, 5002], SENT[n0:]
n0 = len(SENT); alerts.broadcast("cash", "оплата", base=[1]); assert sorted(sum((x[1] for x in SENT[n0:]), [])) == [1, 5001]
st = alerts.status(mid); assert st["linked"] and st["prefs"]["tasks"] is False and st["admin"] is False and len(st["kinds"]) == 6
# task reminders: due in 20 min for the manager -> 5002 only; auto_text tasks are not reminded; once
due = (datetime.now() + timedelta(minutes=20)).isoformat(timespec="minutes")
with database.get_conn() as conn:
    conn.execute("INSERT INTO crm_tasks (title, due, assignee_uid, status, created_at) VALUES ('Позвонить гостю', ?, ?, 'open', '2026-10-22')", (due, mid))
    conn.execute("INSERT INTO crm_tasks (title, due, assignee_uid, status, created_at, auto_text) VALUES ('Авто', ?, ?, 'open', '2026-10-22', 'x')", (due, mid))
alerts.set_prefs(mid, {"tasks": True})
n0 = len(SENT); assert alerts.task_reminders() >= 1
hit = [x for x in SENT[n0:] if "Позвонить" in x[0]]; assert len(hit) == 1 and hit[0][1] == [5002] and "Через" in hit[0][0], SENT[n0:]
assert not any("Авто" in x[0] for x in SENT[n0:])
n1 = len(SENT); assert alerts.task_reminders() == 0 and len(SENT) == n1
# --- edit / delete own messages
CH = []
inbox._dispatch_change = lambda channel, action, payload: CH.append((channel, action, payload))
inbox.bridge_status = lambda: {"status": "connected"}
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at, last_text) VALUES ('wa', 'edit-test@c.us', '998901111222', 'Гость', '2026-10-22T10:00:00', 'ok')")
    cid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'edit-test@c.us'").fetchone()[0]
    conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, status, at, rev) VALUES (?, 'ABC123', 'out', 'Диёр', 'text', 'Привет', 'sent', '2026-10-22T10:00:00', 1)", (cid,))
    m1 = conn.execute("SELECT id FROM inbox_messages WHERE wa_id = 'ABC123'").fetchone()[0]
    conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, status, at, rev) VALUES (?, 'IN1', 'in', NULL, 'text', 'Здравствуйте', 'read', '2026-10-22T09:00:00', 1)", (cid,))
    conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, status, at, rev) VALUES (?, 'local-x', 'out', 'Диёр', 'text', 'ждёт', 'pending', '2026-10-22T10:01:00', 1)", (cid,))
staff = {"uid": 7, "name": "Диёр", "owner": False}; other = {"uid": 8, "name": "Аня", "owner": False}; owner = {"uid": 1, "name": "Тимур", "owner": True}
for who, mid_, msg in ((other, m1, "Чужое"), (staff, m1 + 1, "только свои"), (staff, m1 + 2, "не доставлено")):
    try: inbox.edit_sent(mid_, "x", who); raise AssertionError(msg)
    except inbox.BridgeError as exc: assert msg.lower() in str(exc).lower(), (msg, exc)
m = inbox.edit_sent(m1, "Привет!", staff)
assert m["text"] == "Привет!" and m["edited_at"] and CH[-1] == ("wa", "edit", {"jid": "edit-test@c.us", "id": "ABC123", "text": "Привет!", "has_media": False}), (m, CH)
m = inbox.delete_sent(m1, owner)
assert m["deleted_at"] and CH[-1][1] == "delete"
try: inbox.edit_sent(m1, "y", owner); raise AssertionError("edited deleted")
except inbox.BridgeError as exc: assert "удалено" in str(exc)
# unsupported channel
with database.get_conn() as conn:
    conn.execute("UPDATE inbox_chats SET channel = 'wz' WHERE id = ?", (cid,))
    conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, status, at, rev) VALUES (?, 'WZ1', 'out', 'Диёр', 'text', 'x', 'sent', '2026-10-22T10:02:00', 1)", (cid,))
    m3 = conn.execute("SELECT id FROM inbox_messages WHERE wa_id = 'WZ1'").fetchone()[0]
try: inbox.delete_sent(m3, owner); raise AssertionError("wz")
except inbox.BridgeError as exc: assert "не даёт" in str(exc)
# telegram id parsing
from backend import tg_channels
assert tg_channels._tg_ids({"jid": "tg:12345", "id": "tg:12345:678"}) == (12345, 678)
# routes (dev mode)
config.DEV_MODE = True; config.BOT_TOKEN = ""
from fastapi.testclient import TestClient
from backend.main import app
with database.get_conn() as conn:
    conn.execute("UPDATE inbox_chats SET channel = 'tg' WHERE id = ?", (cid,))
    conn.execute("INSERT INTO inbox_messages (chat_id, wa_id, direction, author, kind, text, status, at, rev) VALUES (?, 'tg:1:9', 'out', 'Оператор', 'text', 'r', 'sent', '2026-10-22T10:03:00', 1)", (cid,))
    m4 = conn.execute("SELECT id FROM inbox_messages WHERE wa_id = 'tg:1:9'").fetchone()[0]
with TestClient(app) as cl:
    r = cl.patch(f"/api/inbox/messages/{m4}", json={"text": "r2"}); assert r.status_code == 200 and r.json()["text"] == "r2", r.text
    r = cl.delete(f"/api/inbox/messages/{m4}"); assert r.status_code == 200 and r.json()["deleted_at"], r.text
    r = cl.patch(f"/api/inbox/messages/{m4}", json={"text": "r3"}); assert r.status_code == 424, r.text
    assert CH[-2][0] == "tg" and CH[-1] == ("tg", "delete", {"jid": "edit-test@c.us", "id": "tg:1:9"})
print("v98 tests OK")
