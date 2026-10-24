import sys, asyncio, types; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import database, crm, crm_ext, inbox, alerts, config, notify, crm_bot, bg
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
config.BOT_TOKEN = "111:dash"; config.CRM_BOT_TOKEN = "222:crm"; config.OWNER_TELEGRAM_IDS = {1}
config.INBOX_NOTIFY_CHAT_IDS = [-100]
bg.run = lambda fn, *a, **k: fn(*a, **k)  # synchronous for the test
CALLS = []
class R:
    status_code = 200
    def __init__(self, url, payload): self.url, self.payload = url, payload
    def json(self): return {"ok": True, "result": {"message_id": len(CALLS)}}
import requests
requests.post = lambda url, json=None, timeout=10, **kw: CALLS.append((url, json)) or R(url, json)
# notify: token override
notify.send("hi", targets=[1]); assert "bot111:dash" in CALLS[-1][0]
notify.send("hi", targets=[1], token=config.crm_bot_token()); assert "bot222:crm" in CALLS[-1][0]
# users: admin (tg 5001) linked, owner id 1 also linked as a user
admin = next(u for u in crm.list_users() if u["role"] == "admin")
with database.get_conn() as conn:
    conn.execute("UPDATE crm_users SET tg_id = 1 WHERE id = ?", (admin["id"],))
CALLS.clear(); alerts.broadcast("bookings", "новая бронь", base=[1, -100])
urls = [(c[0].split("/bot")[1].split("/")[0], c[1]["chat_id"]) for c in CALLS]
assert sorted(urls) == [("111:dash", -100), ("222:crm", 1)], urls  # owner once, from the CRM bot
# inbox alert: work chat via dashboard bot, the linked user via the CRM bot, reply map for both
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at, last_text, last_dir, unread) VALUES ('wa', 'al@c.us', '998901234111', 'Гость', '2026-10-24T10:00:00', 'помогите', 'in', 1)")
    cid = conn.execute("SELECT id FROM inbox_chats WHERE jid = 'al@c.us'").fetchone()[0]
CALLS.clear(); inbox._alert(cid, {"kind": "text", "text": "помогите"}, [1])
urls = sorted((c[0].split("/bot")[1].split("/")[0], c[1]["chat_id"]) for c in CALLS)
assert urls == [("111:dash", -100), ("222:crm", 1)], urls
with database.get_conn() as conn:
    assert conn.execute("SELECT COUNT(*) FROM inbox_tg_map WHERE chat_id = ?", (cid,)).fetchone()[0] == 2
# crm bot handlers
SAID = []
crm_bot._api = lambda method, timeout=35, **kw: SAID.append((method, kw.get("json"))) or {"message_id": 1}
inbox.may_use = lambda uid: True
inbox.send = lambda chat_id, author, text, *a, **k: SAID.append(("send", chat_id, author, text)) or {"id": 1}
user = {"id": 9001, "first_name": "Диёр", "last_name": "Х"}
assert crm_bot.handle({"chat": {"type": "private", "id": 9001}, "from": user, "text": "/start"}) == "start" and "привязать" in SAID[-1][1]["text"]
code = crm.link_code(admin["id"])
assert crm_bot.handle({"chat": {"type": "private", "id": 9001}, "from": user, "text": f"/link {code}"}) == "linked"
assert crm.get_user(admin["id"])["tg_id"] == 9001
assert crm_bot.handle({"chat": {"type": "private", "id": 9001}, "from": user, "text": "/link BAD"}) == "link-failed"
config.WEBAPP_URL = "https://crm.example.com"
assert crm_bot.handle({"chat": {"type": "private", "id": 9001}, "from": user, "text": "/оплата"}) == "page" and "/cash/?v=" in SAID[-1][1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
with database.get_conn() as conn:
    conn.execute("INSERT OR REPLACE INTO inbox_tg_map VALUES (9001, 77, ?, '2026-10-24T10:00:00')", (cid,))
r = crm_bot.handle({"chat": {"type": "private", "id": 9001}, "from": user, "message_id": 78, "text": "Сейчас помогу", "reply_to_message": {"message_id": 77}})
assert r == "replied" and ("send", cid, inbox._agent_name(9001), "Сейчас помогу") in SAID, (r, SAID[-3:])
assert crm_bot.handle({"chat": {"type": "group", "id": -5}, "from": user, "text": "x"}) is None
assert crm_bot.handle({"chat": {"type": "private", "id": 9001}, "from": user, "text": "привет"}) == "hint"
# dashboard bot /link redirects to the CRM bot
import bot
sent = []
class Msg(types.SimpleNamespace):
    async def reply_text(self, text, **kw): sent.append(text)
asyncio.run(bot.link_cmd(types.SimpleNamespace(effective_user=types.SimpleNamespace(id=9001, full_name="X"), effective_message=Msg(text="/link ABC")), None))
assert "отдельного бота" in sent[-1], sent
# fallback: no CRM token -> everything through the dashboard bot, as before
config.CRM_BOT_TOKEN = ""
CALLS.clear(); alerts.broadcast("bookings", "x", base=[-100]); assert all("bot111:dash" in c[0] for c in CALLS) and len(CALLS) == 2
assert not crm_bot.configured()
print("v100 tests OK")
