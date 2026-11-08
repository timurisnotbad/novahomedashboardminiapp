import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date, timedelta
from backend import database, crm, rc_sync, config
database.init_db(); crm.init_db()
config.DEMO_MODE = False
CALLS = []
rc_sync.refresh_apartments = lambda: None
rc_sync.fetch_bookings = lambda a, b, apartment_ids=None: CALLS.append((a, b)) or []
database.replace_window_bookings = lambda rows, d0, d1: CALLS.append(("window", d0, d1)) or {"written": 0, "removed": 0, "missing": 0}
database.count_bookings = lambda: 0
crm.set_setting("rc_history_synced", "")
today = date.today()
rc_sync.sync_to_db()
w = [c for c in CALLS if c[0] == "window"][0]
assert w[1] == (today - timedelta(days=62)).isoformat() and w[2] == (today + timedelta(days=120)).isoformat(), w
chunks = [c for c in CALLS if c[0] != "window" and c[1] < today]
assert chunks and chunks[0][0] == today - timedelta(days=400) and chunks[-1][1] == today - timedelta(days=63) and all((b - a).days <= 59 for a, b in chunks), chunks
assert crm.get_setting("rc_history_synced")[:10] == today.isoformat()
# a failing chunk keeps the day unmarked, the others still land
crm.set_setting("rc_history_synced", ""); CALLS.clear()
def flaky(a, b, apartment_ids=None):
    CALLS.append((a, b))
    if len(CALLS) == 3: raise RuntimeError("timeout")
    return []
rc_sync.fetch_bookings = flaky
rc_sync._sync_history(today)
assert crm.get_setting("rc_history_synced") == "" and len(CALLS) >= 6
print("v103b tests OK")
