import sys, json, hmac, hashlib; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import database, crm, crm_ext, inbox, meta_api, config, bg
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
config.FB_PAGE_TOKEN = "fbtok"; config.IG_PAGE_TOKEN = ""; config.META_APP_SECRET = "sec"; config.WEBAPP_URL = "https://crm.example.com"
bg.run = lambda fn, *a, **k: fn(*a, **k)
CALLS = []
FAIL_WINDOW = {"on": False}
def fake_graph(method, path, token, **kw):
    CALLS.append((method, path, token, kw.get("json") or kw.get("params")))
    if path == "PSID1": return {"first_name": "Aziz", "last_name": "Karimov"}
    if path == "me": return {"name": "Nova Home Apartments", "id": "100"}
    if path == "me/subscribed_apps" and method == "GET": return {"data": [{"subscribed_fields": ["messages", "message_echoes"]}]}
    if path == "me/subscribed_apps": return {"success": True}
    if path == "me/messages":
        body = kw["json"]
        if FAIL_WINDOW["on"] and body.get("messaging_type") == "RESPONSE":
            raise inbox.BridgeError("(#10) This message is sent outside of allowed window.")
        if FAIL_WINDOW["on"] == "both":
            raise inbox.BridgeError("(#10) permission")
        return {"message_id": "m_out_%d" % len(CALLS)}
    return {}
meta_api._graph = fake_graph
# incoming text from a page visitor
meta_api._handle({"object": "page", "entry": [{"id": "100", "messaging": [{"sender": {"id": "PSID1"}, "recipient": {"id": "100"}, "timestamp": 1764000000000,
                  "message": {"mid": "m_in_1", "text": "Здравствуйте, есть свободные даты?"}}]}]})
with database.get_conn() as conn:
    c = conn.execute("SELECT * FROM inbox_chats WHERE jid = 'fb:PSID1'").fetchone()
    assert c and c["channel"] == "fb" and (c["push_name"] or c["name"]) == "Aziz Karimov" and c["unread"] == 1, dict(c) if c else None
    m = conn.execute("SELECT * FROM inbox_messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (c["id"],)).fetchone()
    assert m["wa_id"] == "fb:m_in_1" and m["direction"] == "in" and m["text"].startswith("Здравствуйте")
cid = c["id"]
chat = inbox.get_chat(cid); assert chat["channel_name"] == "Facebook" and chat["title"] == "Aziz Karimov"
inbox._flush_new_clients()
with database.get_conn() as conn:
    cl = conn.execute("SELECT name, source FROM crm_clients WHERE chat_id = ?", (cid,)).fetchone()
assert cl and cl["source"] == "Facebook", dict(cl) if cl else None
# echo of a reply sent from Facebook itself, a location, a shared link, a postback
meta_api._handle({"object": "page", "entry": [{"messaging": [
    {"sender": {"id": "100"}, "recipient": {"id": "PSID1"}, "timestamp": 1764000060000, "message": {"mid": "m_echo", "is_echo": True, "text": "Да, есть"}},
    {"sender": {"id": "PSID1"}, "recipient": {"id": "100"}, "timestamp": 1764000120000, "message": {"mid": "m_loc", "attachments": [{"type": "location", "payload": {"coordinates": {"lat": 41.3, "long": 69.2}}}]}},
    {"sender": {"id": "PSID1"}, "recipient": {"id": "100"}, "timestamp": 1764000180000, "message": {"mid": "m_link", "attachments": [{"type": "fallback", "title": "Nest One", "payload": {"url": "https://example.com/x"}}]}},
    {"sender": {"id": "PSID1"}, "recipient": {"id": "100"}, "timestamp": 1764000240000, "postback": {"title": "Забронировать", "payload": "BOOK"}}]}]})
with database.get_conn() as conn:
    rows = {r["wa_id"]: dict(r) for r in conn.execute("SELECT * FROM inbox_messages WHERE chat_id = ?", (cid,)).fetchall()}
assert rows["fb:m_echo"]["direction"] == "out" and rows["fb:m_loc"]["kind"] == "location" and rows["fb:m_loc"]["lat"] == 41.3
assert "example.com/x" in rows["fb:m_link"]["text"] and any(r["text"] == "Забронировать" for r in rows.values())
# reply from the CRM
CALLS.clear(); m = inbox.send(cid, "Тимур", "Добрый день! Какие даты?")
sent = [c for c in CALLS if c[1] == "me/messages"]
assert len(sent) == 1 and sent[0][2] == "fbtok" and sent[0][3] == {"recipient": {"id": "PSID1"}, "message": {"text": "Добрый день! Какие даты?"}, "messaging_type": "RESPONSE"}, sent
assert m["wa_id"].startswith("fb:m_out_") and m["status"] == "sent"
# a file with a caption: attachment first, then the text
(config.INBOX_MEDIA_DIR).mkdir(parents=True, exist_ok=True); (config.INBOX_MEDIA_DIR / "pic1.jpg").write_bytes(b"\xff\xd8\xff")
CALLS.clear(); inbox.send(cid, "Тимур", "Фото квартиры", media="pic1.jpg", mime="image/jpeg")
sent = [c[3] for c in CALLS if c[1] == "me/messages"]
assert len(sent) == 2 and sent[0]["message"]["attachment"]["type"] == "image" and sent[0]["message"]["attachment"]["payload"]["url"].startswith("https://crm.example.com/api/inbox/media/pic1.jpg?s=") and sent[1]["message"] == {"text": "Фото квартиры"}, sent
# outside the 24 h window: retried as a human-agent reply; when that fails too — a clear message, the bubble is marked failed
FAIL_WINDOW["on"] = True; CALLS.clear(); inbox.send(cid, "Тимур", "Вы ещё планируете приезд?")
types = [c[3].get("messaging_type") for c in CALLS if c[1] == "me/messages"]; assert types == ["RESPONSE", "MESSAGE_TAG"] and [c[3].get("tag") for c in CALLS if c[1] == "me/messages"][1] == "HUMAN_AGENT", types
orig = fake_graph
def both_fail(method, path, token, **kw):
    if path == "me/messages": raise inbox.BridgeError("(#10) This message is sent outside of allowed window.")
    return orig(method, path, token, **kw)
meta_api._graph = both_fail
try: inbox.send(cid, "Тимур", "x"); raise AssertionError("sent outside the window")
except inbox.BridgeError as exc: assert "24 часов" in str(exc), exc
meta_api._graph = fake_graph; FAIL_WINDOW["on"] = False
# status + subscribe
st = meta_api.fb_status(); assert st == {"configured": True, "ok": True, "page": "Nova Home Apartments", "page_id": "100", "shared_token": True, "subscribed": True}, st
CALLS.clear(); meta_api.fb_subscribe(); assert CALLS[0][:2] == ("POST", "me/subscribed_apps") and "messages" in CALLS[0][3]["subscribed_fields"]
# object «instagram» still goes to Instagram; «page» without a Facebook token keeps the old route
config.IG_PAGE_TOKEN = "igtok"
meta_api._handle({"object": "instagram", "entry": [{"messaging": [{"sender": {"id": "IG1"}, "recipient": {"id": "9"}, "timestamp": 1764000300000, "message": {"mid": "ig_1", "text": "hi"}}]}]})
with database.get_conn() as conn:
    assert conn.execute("SELECT channel FROM inbox_chats WHERE jid = 'ig:IG1'").fetchone()["channel"] == "ig"
# the signed webhook route
from fastapi.testclient import TestClient
from backend.main import app
body = json.dumps({"object": "page", "entry": [{"messaging": [{"sender": {"id": "PSID2"}, "recipient": {"id": "100"}, "timestamp": 1764000400000, "message": {"mid": "m_in_2", "text": "Hello"}}]}]}).encode()
sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
with TestClient(app) as cl:
    r = cl.post("/api/inbox/meta/webhook", content=body, headers={"x-hub-signature-256": sig, "content-type": "application/json"}); assert r.status_code == 200, r.text
    r = cl.post("/api/inbox/meta/webhook", content=body, headers={"x-hub-signature-256": "sha256=bad", "content-type": "application/json"}); assert r.status_code == 403
    import time; time.sleep(0.5)
with database.get_conn() as conn:
    assert conn.execute("SELECT 1 FROM inbox_chats WHERE jid = 'fb:PSID2'").fetchone()
print("v113 tests OK")
