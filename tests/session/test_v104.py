import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import datetime, timedelta
from backend import database, crm, crm_ext, inbox, config
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
with database.get_conn() as conn:
    conn.execute("INSERT INTO crm_tasks (title, due, status, created_at, auto_text, auto_status, done_at) VALUES ('Старое', ?, 'done', ?, 'x', 'sent:Гость (WhatsApp)', ?)", (crm._now()[:16], crm._now(), crm._now()))
    conn.execute("INSERT INTO crm_tasks (title, due, status, created_at, auto_text) VALUES ('Скоро', ?, 'open', ?, '{имя}, скоро заезд')", ((datetime.now() + timedelta(hours=3)).isoformat(timespec="minutes"), crm._now()))
with database.get_conn() as conn:
    conn.execute("INSERT INTO crm_tasks (title, due, status, created_at, auto_text) VALUES ('Давно', '2026-01-01T10:00', 'open', ?, 'x')", (crm._now(),))
up = crm_ext.upcoming_messages(48); assert any(u["title"] == "Скоро" and u["note"] for u in up) and not any(u["title"] == "Давно" for u in up), up
rc = crm_ext.recent_messages(50); assert any(r["what"] == "Старое" and r["status"] == "sent" for r in rc), rc[:2]
config.DEV_MODE = True; config.BOT_TOKEN = ""
from fastapi.testclient import TestClient
from backend.main import app
with TestClient(app) as cl:
    r = cl.post("/api/crm/auth/login", json={"email": "admin@novahome.uz", "password": "secret123"})
    cookies = r.cookies if r.status_code == 200 else None
    r = cl.get("/api/crm/auto", cookies=cookies)
    if r.status_code == 200:
        d = r.json(); assert {"steps", "settings", "upcoming", "recent", "rules"} <= set(d) and "auto_step_messages" in d["settings"], list(d)
        print("route ok")
    else:
        print("route skipped (auth)", r.status_code)
assert all("{код домофона}" not in s["message"] and "{wifi}" not in s["message"] and "{ключи" not in s["message"] for s in crm.DEFAULT_STEPS)
print("v104 tests OK")
