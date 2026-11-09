import sys, json; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date, timedelta
from backend import database, crm
database.init_db(); crm.init_db()
d = lambda n: (date.today() + timedelta(days=n)).isoformat()
def row(i, b, e): return {"id": i, "apartment_id": 5, "apartment_name": "B-005", "begin_date": b, "end_date": e, "client_name": "G", "client_phone": None, "status": "paid", "is_delete": 0, "amount": 1, "days_count": 1}
# started stay (began 3 days ago) and a future one; the next fetch returns neither
database.replace_window_bookings([row(990001, d(-3), d(2)), row(990002, d(5), d(7))], d(-62), d(120))
for _ in range(3):
    database.replace_window_bookings([], d(-62), d(120))
with database.get_conn() as conn:
    ids = {r[0] for r in conn.execute("SELECT id FROM bookings WHERE id IN (990001, 990002)").fetchall()}
assert ids == {990001}, ids  # the started stay stays, the future one is treated as cancelled
# archive repair: a stay archived after it had begun comes back once
with database.get_conn() as conn:
    conn.execute("DELETE FROM crm_settings WHERE key = 'cancelled_restored'")
    conn.execute("INSERT OR REPLACE INTO bookings_cancelled (id, data, cancelled_at) VALUES (990003, ?, ?)", (json.dumps(row(990003, d(-10), d(-7))), d(-8) + "T10:00:00"))
    conn.execute("INSERT OR REPLACE INTO bookings_cancelled (id, data, cancelled_at) VALUES (990004, ?, ?)", (json.dumps(row(990004, d(3), d(5))), d(-1) + "T10:00:00"))  # cancelled before start: stays cancelled
n = database.restore_started_cancelled(); assert n == 1, n
with database.get_conn() as conn:
    assert conn.execute("SELECT 1 FROM bookings WHERE id = 990003").fetchone() and not conn.execute("SELECT 1 FROM bookings WHERE id = 990004").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM bookings_cancelled WHERE id = 990003").fetchone()[0] == 0
assert database.restore_started_cancelled() == 0  # once
print("v106 tests OK")
