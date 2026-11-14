"""v120: a file (PDF instruction) on a quick reply and on a stay step goes to the guest with the text."""
import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
import os, tempfile
os.environ.setdefault("INBOX_MEDIA_DIR", tempfile.mkdtemp())
from backend import database, inbox, crm, crm_ext, ai, config
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db(); ai.init_db(); inbox.init_db()
CALLS = []; SEQ = [0]; FAIL_MEDIA = [False]
def fake(channel, payload):
    if payload.get("media") and FAIL_MEDIA[0]:
        CALLS.append(("FAIL", payload.get("file_name"))); raise inbox.BridgeError("файл не принят")
    CALLS.append((payload.get("text") or "")[:20] + ("|" + payload["file_name"] if payload.get("media") else "")); SEQ[0] += 1
    return {"id": "x120-%d" % SEQ[0]}
inbox._dispatch_send = fake; inbox.send_receipt = lambda *a, **k: None
with database.get_conn() as conn:
    conn.execute("INSERT INTO inbox_chats (channel, jid, phone, name, last_at) VALUES ('wa', '998900000120@s.whatsapp.net', '998900000120', 'G120', '2026-11-14T10:00:00')")
    chat = conn.execute("SELECT id FROM inbox_chats WHERE jid = '998900000120@s.whatsapp.net'").fetchone()[0]

# stored file
f = inbox.store_upload(b"%PDF-1.4 test", "Инструкция Nova Home.pdf", "application/octet-stream")
assert f["mime"] == "application/pdf" and f["file_name"] == "Инструкция Nova Home.pdf" and inbox.valid_media(f["media"]), f
assert not inbox.valid_media("../secrets.json") and not inbox.valid_media("nope.pdf") and not inbox.valid_media("")
o = inbox.file_out(**f); assert o["url"].endswith("?s=" + inbox.media_sig(f["media"])) and o["file_name"].endswith(".pdf")

# short text = caption: one message
m = inbox.send_with_file(chat, "Оператор", "Инструкция во вложении", **f)
assert CALLS == ["Инструкция во вложен|Инструкция Nova Home.pdf"] and m["kind"] == "document" and m["text"] == "Инструкция во вложении", (CALLS, m)
# long text: text first, the file after it
CALLS.clear(); long = "Здравствуйте! " + "очень длинная инструкция " * 60
m = inbox.send_with_file(chat, "Оператор", long, client_id="c120", **f)
assert len(CALLS) == 2 and "|" not in CALLS[0] and CALLS[1] == "|Инструкция Nova Home.pdf" and m["kind"] == "document" and m["client_id"] == "c120-f", (CALLS, m)
# the browser repeats the request: nothing is sent again
CALLS.clear(); m2 = inbox.send_with_file(chat, "Оператор", long, client_id="c120", **f)
assert CALLS == [] and m2["id"] == m["id"], (CALLS, m2)
# the text went, the file did not: no exception, the reason is in the answer, the file waits with a red mark
CALLS.clear(); FAIL_MEDIA[0] = True
m = inbox.send_with_file(chat, "Оператор", long, client_id="c121", **f)
assert m["kind"] == "text" and m["file_error"] == "файл не принят" and len(CALLS) == 2, (m, CALLS)
with database.get_conn() as conn:
    bad = conn.execute("SELECT * FROM inbox_messages WHERE client_id = 'c121-f'").fetchone()
assert bad["status"] == "failed" and bad["media"] == f["media"]
FAIL_MEDIA[0] = False; r = inbox.retry(bad["id"], "Оператор"); assert r["status"] == "sent" and r["kind"] == "document"
# without a file — the plain send
CALLS.clear(); inbox.send_with_file(chat, "Оператор", "просто текст"); assert CALLS == ["просто текст"]

# quick reply with a file
t = crm.save_command(None, "instr", "Инструкция заезда", "Инструкция по заселению — в файле", file=f)
assert t["file"]["file_name"] == "Инструкция Nova Home.pdf" and t["media"] == f["media"], t
t = crm.save_command(t["id"], "instr", "Инструкция заезда", "новый текст", file={"media": None}); assert t["file"] and t["text"] == "новый текст"
assert [x for x in inbox.templates() if x["id"] == t["id"]][0]["file"]["url"]
t = crm.save_command(t["id"], "instr", "Инструкция заезда", "новый текст", file={"media": ""}); assert t["file"] is None and t["media"] is None
try: crm.save_command(t["id"], "instr", "x", "y", file={"media": "../../.env"}); raise SystemExit("bad media accepted")
except ValueError: pass
t = inbox.set_template_file(t["id"], f); assert t["file"]; assert inbox.set_template_file(t["id"], None)["file"] is None

# stay step with a file
steps = [dict(x) for x in crm.DEFAULT_STEPS]
i = next(k for k, x in enumerate(steps) if "инструкц" in x["title"].lower())
steps[i].update(media=f["media"], mime=f["mime"], file_name=f["file_name"])
steps[3].update(media=f["media"], file_name="x.pdf")       # a manager's task (no text): the file is dropped
steps[0].update(media="../../.env", file_name="e")          # not a stored file: dropped
saved = crm.save_steps(steps)
assert saved[i]["media"] == f["media"] and saved[i]["file_url"] and "media" not in saved[3] and "media" not in saved[0], saved[i]
assert crm.steps()[i]["file_name"] == "Инструкция Nova Home.pdf"
task = {"src_key": "cl:77:" + crm.step_key(steps[i]["title"]), "auto_text": "текст"}
assert crm.task_file(task)["media"] == f["media"]
assert crm.task_file({"src_key": "cl:77:" + crm.step_key(steps[1]["title"]), "auto_text": "t"}) is None
assert crm.task_file({"src_key": "cl:77:" + crm.step_key(steps[i]["title"]), "auto_text": None}) is None
CALLS.clear()
c = inbox.get_chat(chat)
st, err, reached = crm_ext._send_everywhere(None, c, lambda ch: "Ждём вас сегодня", "Nova", file=crm.task_file(task))
assert st == "sent" and err is None and CALLS == ["Ждём вас сегодня|Инструкция Nova Home.pdf"], (st, err, CALLS)
CALLS.clear(); FAIL_MEDIA[0] = True
st, err, reached = crm_ext._send_everywhere(None, c, lambda ch: long, "Nova", file=crm.task_file(task))
assert st == "sent" and "файл" in err and "нет" in err, (st, err)
FAIL_MEDIA[0] = False
# the file is replaced later: tasks made earlier send the new one
f2 = inbox.store_upload(b"%PDF-1.4 new", "Инструкция v2.pdf", "application/pdf")
steps[i].update(media=f2["media"], mime=f2["mime"], file_name=f2["file_name"]); crm.save_steps(steps)
assert crm.task_file(task)["file_name"] == "Инструкция v2.pdf"
steps[i].pop("media"); crm.save_steps(steps); assert crm.task_file(task) is None
print("v120 tests OK")
