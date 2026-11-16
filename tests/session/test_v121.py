"""v121: the Booking.com «disconnected» warning replaces itself instead of piling up in Telegram."""
import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import datetime, timedelta
from backend import database, inbox, crm, booking_ext, notify, config, bg, alerts
database.init_db(); inbox.init_db(); crm.init_db(); booking_ext.init_db()
config.BOT_TOKEN = "dash"; config.notify_targets = lambda: [-100123, 555]
config.crm_bot_token = lambda: "crmbot"
alerts.targets = lambda kind, uids=None: [777]            # one linked CRM user with «system» on
bg.run = lambda fn, *a, **k: fn(*a)                        # run in place
CALLS = []; MID = [100]
def fake(method, payload, token=None):
    CALLS.append((method, payload.get("chat_id"), payload.get("message_id"), token or "dash", (payload.get("text") or "")))
    if method == "sendMessage":
        MID[0] += 1; return {"message_id": MID[0]}
    return True
notify._call = fake
database.get_topic = lambda chat_id, role: None
clock = [datetime(2026, 11, 16, 0, 35)]
class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None): return clock[0]
booking_ext.datetime = FakeDT
def ping(session): booking_ext.ping({"session": session, "page": "x", "version": "1"})
def methods(): return [(c[0], c[1], c[3]) for c in CALLS]

ping("ok"); ping("ok")
assert not CALLS
# the tab closes: one warning per chat, each from its own bot
ping("notab")
assert sorted(methods()) == sorted([("sendMessage", -100123, "dash"), ("sendMessage", 555, "dash"), ("sendMessage", 777, "crmbot")]), CALLS
first = {c[1]: MID for c in CALLS}
ids1 = {r["chat_id"]: r["message_id"] for r in database.bot_messages_for("booking_session")}
assert set(ids1) == {-100123, 555} and [r["chat_id"] for r in database.bot_messages_for("booking_session:crm")] == [777]
# pings within the hour: nothing
CALLS.clear()
for m in (5, 20, 59):
    clock[0] = datetime(2026, 11, 16, 0, 35) + timedelta(minutes=m); ping("notab")
assert not CALLS, CALLS
# an hour later: the old warning is deleted, a new one sent (rings) — still one per chat
clock[0] = datetime(2026, 11, 16, 1, 36); ping("notab")
dels = [c for c in CALLS if c[0] == "deleteMessage"]; sends = [c for c in CALLS if c[0] == "sendMessage"]
assert len(dels) == 3 and len(sends) == 3, CALLS
assert {(c[1], c[2]) for c in dels if c[3] == "dash"} == set(ids1.items()), (dels, ids1)
assert [c[3] for c in dels if c[1] == 777] == ["crmbot"]
assert all("(с 00:35, уже 1 ч 1 мин)" in c[4] for c in sends), sends
# each delete happens before its send, in the same chat
for chat in (-100123, 555, 777):
    seq = [c[0] for c in CALLS if c[1] == chat]; assert seq == ["deleteMessage", "sendMessage"], (chat, seq)
# five hours of night: still one warning per chat
CALLS.clear()
for h in range(2, 7):
    clock[0] = datetime(2026, 11, 16, h, 37); ping("notab")
assert len([c for c in CALLS if c[0] == "sendMessage"]) == 15 and len([c for c in CALLS if c[0] == "deleteMessage"]) == 15
assert len(database.bot_messages_for("booking_session")) == 2
# «expired» (the extension reloads the page) does not count as fixed
CALLS.clear(); clock[0] = datetime(2026, 11, 16, 7, 0); ping("expired"); assert not CALLS
# back: the last warning is edited into «✅» (an edit does not ring), nothing new is sent
CALLS.clear(); clock[0] = datetime(2026, 11, 16, 9, 12); ping("ok")
assert {c[0] for c in CALLS} == {"editMessageText"} and len(CALLS) == 3, CALLS
assert all(c[4].startswith("✅ Booking.com снова на связи") for c in CALLS), CALLS
full = [c for c in CALLS if c[1] == 555][0]
CALLS.clear(); ping("ok"); assert not any(c[0] != "getUpdates" for c in CALLS if c[0] in ("sendMessage", "editMessageText", "deleteMessage")), CALLS
# the next outage removes the «✅» line too: the chat never holds more than one Booking line
clock[0] = datetime(2026, 11, 17, 0, 10); ping("login")
seq = [(c[0], c[1]) for c in CALLS if c[1] == 555]
assert seq == [("deleteMessage", 555), ("sendMessage", 555)] and "разлогинился" in [c for c in CALLS if c[0] == "sendMessage"][0][4], CALLS
# another kind of problem while one is open → sent at once (replacing)
CALLS.clear(); clock[0] = datetime(2026, 11, 17, 0, 15); ping("notab")
assert len([c for c in CALLS if c[0] == "sendMessage"]) == 3
# the usual broadcast (no replace) is unchanged: plain sends, nothing stored
CALLS.clear(); alerts.broadcast("system", "обычное"); assert [c[0] for c in CALLS] == ["sendMessage"] * 3

# ---- the same for «bot down» and «calendar sync down»
from backend import watchdog, services
watchdog.revive_bot = lambda: "перезапустил"; watchdog.log_tail = lambda *a, **k: ""
config.OWNER_TELEGRAM_IDS = [555]
t0 = datetime(2026, 11, 16, 10, 0); watchdog._STARTED = t0 - timedelta(hours=1)
HB = [(t0, "ok")]; watchdog.read_heartbeat = lambda: HB[0]
CALLS.clear()
LAST = [t0]
def upto(t):  # the watchdog runs every minute: walk there minute by minute, return what was sent
    out = [];
    while LAST[0] < t:
        LAST[0] += timedelta(minutes=1); r = watchdog.check_bot(LAST[0]); out += [r] if r else []
    return out
assert upto(t0 + timedelta(minutes=1)) == []
r = upto(t0 + timedelta(minutes=52))
assert len(r) == 2 and r[0].startswith("🔴") and r[1].startswith("⚠️"), r   # dead: first alert, then after three revives
# the computer slept: no check for 2 hours → no revive / alert at once, a few minutes of grace


sends = [c for c in CALLS if c[0] == "sendMessage" and c[1] == 555]; dels = [c for c in CALLS if c[0] == "deleteMessage" and c[1] == 555]
assert len(sends) == 2 and len(dels) == 1, CALLS                                   # the second replaced the first
upto(t0 + timedelta(minutes=70)); HB[0] = (t0 + timedelta(minutes=70), "ok"); CALLS.clear()
r = upto(t0 + timedelta(minutes=71)); assert r and r[0].startswith("🟢"), r
assert [c[0] for c in CALLS if c[1] == 555] == ["editMessageText"] and "до 11:11" in CALLS[0][4], CALLS
SS = [{"minutes": 90, "fails": 4, "last_ok": "2026-11-16T08:00:00", "error": "401"}]
services.sync_staleness = lambda: SS[0]; services.fmt_local = lambda v: "16.11 08:00"
CALLS.clear(); assert watchdog.check_sync().startswith("🔴") and [c[0] for c in CALLS] == ["sendMessage"] * 2   # owner 555 + the CRM user
assert watchdog.check_sync() is None
SS[0] = {"minutes": 3, "fails": 0, "last_ok": "x", "error": ""}; CALLS.clear()
assert watchdog.check_sync().startswith("🟢") and {c[0] for c in CALLS} == {"editMessageText"}, CALLS
print("v121 tests OK")
if "-v" in sys.argv:
    CALLS.clear(); clock[0] = datetime(2026, 11, 17, 2, 20); ping("notab"); print(CALLS[-1][4])
    CALLS.clear(); clock[0] = datetime(2026, 11, 17, 3, 5); ping("ok"); print(CALLS[-1][4])
# the computer slept 2 hours: the first check after waking neither revives nor alerts; a few minutes of grace
watchdog._state.update(alerted=False, revived_at=None, revives=0, second_alert=False, checked_at=datetime(2026, 11, 16, 20, 0))
HB[0] = (datetime(2026, 11, 16, 20, 0), "ok"); CALLS.clear()
assert watchdog.check_bot(datetime(2026, 11, 16, 22, 0)) is None and watchdog.revive_bot  # woke
LAST[0] = datetime(2026, 11, 16, 22, 0)
assert upto(datetime(2026, 11, 16, 22, 3)) == [] and not CALLS
HB[0] = (datetime(2026, 11, 16, 22, 10), "ok")     # the bot writes its mark every minute after waking
assert upto(datetime(2026, 11, 16, 22, 10)) == [] and not CALLS
print("v121 sleep test OK")
