import sys, json; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from datetime import date, timedelta
from backend import database, crm, rc_sync, config
database.init_db(); crm.init_db()
config.RC_BASE_URL = "https://rc.test"; config.APARTMENTS = {1: "A-001"}
d = lambda n: (date.today() + timedelta(days=n)).isoformat()
class Resp:
    def __init__(self, code, data): self.status_code, self._d = code, data
    def json(self): return self._d
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)
# RC that hides check_out guests unless asked explicitly, and ignores unknown status values (returns everything)
EV = [{"id": 1, "status": "paid", "begin_date": d(-10), "end_date": d(-8)}, {"id": 2, "status": "check_out", "begin_date": d(-20), "end_date": d(-18)}]
CALLS = []
def fake_get(url, params=None, headers=None, timeout=30):
    CALLS.append(params.get("statuses[]"))
    st = params.get("statuses[]")
    if st is None: evs = [e for e in EV if e["status"] in rc_sync.KNOWN_STATUSES]
    elif set(st) & {"check_out"}: evs = [e for e in EV if e["status"] in st]
    elif set(st) <= set(rc_sync.KNOWN_STATUSES): evs = [e for e in EV if e["status"] in st]
    elif st == ["cancelled"]: evs = []
    else: evs = [e for e in EV if e["status"] in rc_sync.KNOWN_STATUSES]  # unknown value ignored
    return Resp(200, {"items": [{"apartment_id": 1, "events": evs}]})
import requests; requests.get = fake_get
assert rc_sync.statuses_to_request() is None
rows = rc_sync.fetch_bookings(date.today() - timedelta(days=30), date.today()); assert [r["id"] for r in rows] == [1]  # before the probe: the hidden one is missing
rep = rc_sync.probe_statuses(45)
assert "check_out" in rep["discovered"] and "checkin" not in rep["discovered"], rep["discovered"]
assert rc_sync.statuses_to_request() == rc_sync.KNOWN_STATUSES + ["check_out"], rc_sync.statuses_to_request()
rows = rc_sync.fetch_bookings(date.today() - timedelta(days=30), date.today()); assert sorted(r["id"] for r in rows) == [1, 2], rows
assert CALLS[-1] == rc_sync.KNOWN_STATUSES + ["check_out"]
print("v108 tests OK")
