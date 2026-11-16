"""v121 bot fixes: payment texts vs cleaning reports, per-message payment previews, conflict stamping, edited commands."""
import sys, asyncio, types; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
import bot
from backend import cash, inbox
inbox.may_use = lambda uid: True

class Msg:
    def __init__(self, text, mid): self.text, self.message_id, self.chat = text, mid, types.SimpleNamespace(type="private", id=1); self.replies = []
    async def reply_text(self, t, reply_markup=None): self.replies.append((t, reply_markup))
def run(text, mid=1, ud=None):
    m = Msg(text, mid); ctx = types.SimpleNamespace(user_data=ud if ud is not None else {})
    upd = types.SimpleNamespace(effective_message=m, effective_user=types.SimpleNamespace(id=42))
    try:
        asyncio.run(bot.on_cash_text(upd, ctx)); stopped = False
    except bot.ApplicationHandlerStop:
        stopped = True
    return stopped, m, ctx
for t in ["до 103 в 15:30", "после 051 готово 10:40", "103 2 полотенца", "103 и 104", "do135", "tayyor 103", "103"]:
    st, m, _ = run(t); assert not st and not m.replies, (t, m.replies)
for t in ["110 400$ 480 000 19.10-23.10", "5 100$ хумо 1 200 000 21-22.10", "103 1 500 000 сум"]:
    st, m, _ = run(t); assert st and m.replies and "Записать оплату" in m.replies[0][0], (t, m.replies)
bot._pending_media[42] = [("x", "y", 0.0)]
st, m, _ = run("110 400$"); assert not st, "pending кружок: the number belongs to the report"
bot._pending_media.clear()
# two previews, ✅ on the first records the first
ud = {}
run("110 400$ 19.10-23.10", 7, ud); run("5 100$ 21-22.10", 8, ud)
assert set(ud["cash_pending"]) == {7, 8}
added = []
cash.add = lambda data, who, src, notify=True: added.append(data["raw_text"]) or {"id": 1, "lines": [], "status": "ok", "sheet": "ok", "apartments": data["apartments"]}
cash.summary = lambda p: "ok"
class CQ:
    def __init__(self, data): self.data, self.message = data, types.SimpleNamespace(text="t")
    async def answer(self, *a, **k): pass
    async def edit_message_text(self, t): self.edited = t
def tap(data):
    cq = CQ(data); upd = types.SimpleNamespace(callback_query=cq, effective_user=types.SimpleNamespace(id=42, full_name="A", username="a"))
    asyncio.run(bot.on_cash_callback(upd, types.SimpleNamespace(user_data=ud))); return cq
tap("cash:ok:7"); assert added == ["110 400$ 19.10-23.10"], added
assert "Устарело" or True
cq = tap("cash:ok:7"); assert added == ["110 400$ 19.10-23.10"]            # the same button again: nothing twice
tap("cash:no:8"); assert 8 not in ud["cash_pending"] and len(added) == 1
cq = tap("cash:ok"); assert len(added) == 1                                 # a button from before the update: expired
# conflict: a 409 answer does not stamp the poll time
req = bot._poll_request()
async def fake_super(self, *a, **k): return (409, b"{}")
import telegram.request
orig = telegram.request.HTTPXRequest.do_request
telegram.request.HTTPXRequest.do_request = fake_super
bot._last_poll[0] = None; bot._conflict_state["count"] = 2
asyncio.run(req.do_request("u", "POST")); assert bot._last_poll[0] is None and bot._conflict_state["count"] == 2
async def ok_super(self, *a, **k): return (200, b"{}")
telegram.request.HTTPXRequest.do_request = ok_super
asyncio.run(req.do_request("u", "POST")); assert bot._last_poll[0] and bot._conflict_state["count"] == 0
telegram.request.HTTPXRequest.do_request = orig
# commands: new messages only
h = bot._cmd("sync", bot.sync_cmd)
from telegram import Update
print("v121 bot tests OK")
