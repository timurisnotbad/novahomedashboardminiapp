import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from backend import database, crm, inbox, uptime, alerts, logsetup
database.init_db(); crm.init_db(); inbox.init_db(); uptime.init_db()
tmp = Path(tempfile.mkdtemp()); logsetup.LOG_DIR = tmp; uptime.HEARTBEAT = tmp / "server-heartbeat.txt"
SENT = []; alerts.broadcast = lambda kind, text, **k: SENT.append((kind, text))
SAMPLE = """Event[0]:
  Log Name: System
  Source: Microsoft-Windows-WER-SystemErrorReporting
  Date: 2026-11-10T03:15:02.000
  Event ID: 1001
  Level: Error
  Description:
Компьютер был перезагружен после критической ошибки. Код ошибки: 0x0000009f (0x0000000000000003, 0xffff).

Event[1]:
  Log Name: System
  Source: Microsoft-Windows-Kernel-Power
  Date: 2026-11-10T03:14:40.000
  Event ID: 41
  Description:
Система перезагрузилась, не завершив полностью работу.
"""
ev = uptime.parse_events(SAMPLE); assert [e["id"] for e in ev] == [1001, 41] and ev[0]["date"] == "2026-11-10T03:15:02", ev
kind, reason = uptime.explain(ev); assert kind == "bsod" and "0x0000009f" in reason, (kind, reason)
assert uptime.explain(ev[1:])[0] == "power"
assert uptime.explain([{"id": 1074, "source": "User32", "date": "", "text": "Процесс MoUsoCoreWorker.exe инициировал перезагрузку: обновление операционной системы"}])[0] == "planned"
assert "обновления" in uptime.explain([{"id": 1074, "source": "User32", "date": "", "text": "MoUsoCoreWorker.exe restart"}])[1]
assert uptime.explain([{"id": 42, "source": "Kernel-Power", "date": "", "text": "сон"}])[0] == "sleep" and uptime.explain([])[0] == "unknown"
# first start: no mark → nothing; then a 5-hour gap → an outage with a notice
now = datetime.now().replace(microsecond=0)
assert uptime.on_start(now - timedelta(hours=6)) is None and uptime.last_beat() == now - timedelta(hours=6)
uptime.beat(now - timedelta(hours=5))
uptime.windows_events = lambda since, until=None: ev
r = uptime.on_start(now); assert r and r["kind"] == "bsod" and r["minutes"] == 300 and SENT and "5 ч 00 мин" in SENT[-1][1] and SENT[-1][0] == "system", (r, SENT)
# a plain restart 1 minute later: nothing; a restart with a fresh crash log: recorded
uptime.beat(now); n = len(SENT)
assert uptime.on_start(now + timedelta(minutes=1)) is None and len(SENT) == n
(tmp / "server-error.log").write_text("Traceback...\nsqlite3.OperationalError: database is locked\n", encoding="utf-8")
uptime.beat(datetime.now()); r = uptime.on_start(datetime.now() + timedelta(minutes=1)); assert r and r["kind"] == "crash" and "database is locked" in r["details"], r
st = uptime.status(); assert st["month"]["count"] == 2 and st["outages"][0]["kind"] == "crash" and st["month"]["availability"] < 100, st["month"]
print("v112 tests OK")
