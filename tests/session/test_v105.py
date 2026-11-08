import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import database, crm, crm_ext, inbox, rc_sync
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
assert rc_sync.HISTORY_DAYS >= 1000
with database.get_conn() as conn:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()]
    def mk(i, **kw):
        row = {"id": i, "apartment_id": 5, "apartment_name": "B-005", "begin_date": "2024-03-01", "end_date": "2024-03-04", "status": "paid", "is_delete": 0, "amount": 100, "days_count": 3}
        row.update(kw); row = {k: v for k, v in row.items() if k in cols}
        conn.execute(f"INSERT OR REPLACE INTO bookings ({','.join(row)}) VALUES ({','.join('?'*len(row))})", list(row.values()))
    mk(880001, client_name="Old Guest Phone", client_phone="+998909998877", client_email="old@mail.com", client_phone2="+998901112200")
    mk(880002, client_name="Email Only", client_phone=None, client_email="x.123@guest.booking.com")
    mk(880003, client_name="Email Only", client_phone=None, client_email="x.123@guest.booking.com")  # the same guest twice -> one card
    mk(880004, client_name="Nobody", client_phone=None, client_email=None)
n = crm.import_clients()
with database.get_conn() as conn:
    a = conn.execute("SELECT name, email, phone2 FROM crm_clients WHERE phone LIKE '%909998877'").fetchone()
    assert a and a["name"] == "Old Guest Phone" and a["email"] == "old@mail.com" and a["phone2"] == "998901112200", dict(a) if a else None
    e = conn.execute("SELECT COUNT(*), MIN(name) FROM crm_clients WHERE email = 'x.123@guest.booking.com'").fetchone()
    assert e[0] == 1 and e[1] == "Email Only", tuple(e)
    assert conn.execute("SELECT COUNT(*) FROM crm_clients WHERE name = 'Nobody'").fetchone()[0] == 0
n2 = crm.import_clients()
with database.get_conn() as conn:
    assert conn.execute("SELECT COUNT(*) FROM crm_clients WHERE email = 'x.123@guest.booking.com'").fetchone()[0] == 1  # idempotent
print("v105 tests OK", n, n2)
