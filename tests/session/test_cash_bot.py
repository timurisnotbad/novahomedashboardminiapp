"""Bot: quick text -> preview -> ✅ records; channel post -> payment; edited post -> edit."""
import asyncio, sys, types, datetime
sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import cash, config, database, crm, inbox
import bot
database.init_db(); crm.init_db(); inbox.init_db(); cash.init_db()
config.OWNER_TELEGRAM_IDS = {1}; config.PAY_CHANNEL_IDS = {-100500}
config.FINANCE_ENABLED = True
CALLS = []
def fake_call(action, payload=None, timeout=40):
    CALLS.append(action)
    if action == "cash_meta": return {"methods": [], "rates": {}}
    if action == "cash_add": return {"rows": [500 + i for i in range(len(payload["rows"]))]}
    return {"ok": True}
cash._sheet_call = fake_call
cash._notify = lambda *a, **k: None
inbox.may_use = lambda uid: True
bot._register = lambda u: True

class Obj(types.SimpleNamespace):
    pass
sent = []
class Msg(Obj):
    async def reply_text(self, text, **kw): sent.append(("reply", text, kw.get("reply_markup")))
class CQ(Obj):
    async def answer(self, *a, **k): sent.append(("answer", a))
    async def edit_message_text(self, text, **kw): sent.append(("edit", text))
user = Obj(id=777, full_name="Диёр Х", first_name="Диёр", username="diyor")
ctx = Obj(user_data={}, bot=Obj(username="novabot", id=42))

# 1. plain text that is not a payment: nothing happens
upd = Obj(effective_message=Msg(text="привет, как дела", chat=Obj(type="private")), effective_user=user)
asyncio.run(bot.on_cash_text(upd, ctx)); assert not sent and "cash_pending" not in ctx.user_data
upd = Obj(effective_message=Msg(text="5", chat=Obj(type="private")), effective_user=user)  # cleaning report number
asyncio.run(bot.on_cash_text(upd, ctx)); assert not sent
# 2. a payment line -> preview with buttons, handler stops the update
upd = Obj(effective_message=Msg(text="110 400$ 480 000 19.10-23.10", chat=Obj(type="private")), effective_user=user)
try:
    asyncio.run(bot.on_cash_text(upd, ctx)); raise AssertionError("no stop")
except bot.ApplicationHandlerStop:
    pass
assert sent[-1][0] == "reply" and "Квартира: 110" in sent[-1][1] and "400 $ (Нал USD) + 480 000 сум (Нал UZS)" in sent[-1][1] and "≈ 440 $" in sent[-1][1], sent[-1][1]
assert ctx.user_data["cash_pending"]["q"]["stay"] == "19.10-23.10"
# 3. ✅ -> recorded
cq = CQ(data="cash:ok", message=Obj(text="x"))
asyncio.run(bot.on_cash_callback(Obj(callback_query=cq, effective_user=user), ctx))
assert sent[-1][0] == "edit" and "записана" in sent[-1][1] and "строки 500, 501" in sent[-1][1], sent[-1]
p = cash.list_payments()[0]
assert p["source"] == "bot" and p["who_uid"] == 777 and p["who_name"] == "Диёр Х" and p["total_usd"] == 440.0 and p["raw_text"] == "110 400$ 480 000 19.10-23.10"
# 4. ✖ / stale
cq = CQ(data="cash:no", message=Obj(text="x"))
asyncio.run(bot.on_cash_callback(Obj(callback_query=cq, effective_user=user), ctx)); assert sent[-1] == ("edit", "x\n\n✖ Не записано") or sent[-1][0] == "edit"
# 5. channel post -> payment with the post date; edit -> edit
database.all_active_bookings = lambda: []
database.upsert_channel_payment = lambda *a, **k: None
bot._apartment_names = lambda: []
post = Obj(chat_id=-100500, message_id=9, caption="110 / 300$ / 19.10-23.10", text=None, date=datetime.datetime(2026, 10, 19, 10, 0, tzinfo=datetime.timezone.utc))
asyncio.run(bot.on_channel_payment(Obj(channel_post=post, edited_channel_post=None), ctx))
c = cash.by_ref("-100500:9")
assert c and c["source"] == "channel" and c["day"] == "2026-10-19" and c["total_usd"] == 300.0 and c["who_name"] == "канал Оплаты", c
asyncio.run(bot.on_channel_payment(Obj(channel_post=post, edited_channel_post=None), ctx))
assert len([x for x in cash.list_payments() if x.get("channel_ref") == "-100500:9"]) == 1  # no duplicate
post2 = Obj(chat_id=-100500, message_id=9, caption="110 / 350$ / 19.10-23.10", text=None, date=post.date)
asyncio.run(bot.on_channel_payment(Obj(channel_post=None, edited_channel_post=post2), ctx))
c = cash.by_ref("-100500:9"); assert c["total_usd"] == 350.0 and [a["action"] for a in cash.audit(payment_id=c["id"])][0] == "edit"
# other channel: ignored
post3 = Obj(chat_id=-100999, message_id=1, caption="110 / 350$", text=None, date=post.date)
asyncio.run(bot.on_channel_payment(Obj(channel_post=post3, edited_channel_post=None), ctx)); assert cash.by_ref("-100999:1") is None
# 6. /оплата in a group -> hint; private -> WebApp button with the personal key
config.WEBAPP_URL = "https://crm.example.com/?x=1"
m = Msg(text="/оплата", chat=Obj(type="group"))
asyncio.run(bot.cash_cmd(Obj(effective_message=m, effective_user=user, effective_chat=Obj(type="group")), ctx)); assert "личного чата" in sent[-1][1]
m = Msg(text="/оплата", chat=Obj(type="private"))
asyncio.run(bot.cash_cmd(Obj(effective_message=m, effective_user=user, effective_chat=Obj(type="private")), ctx))
kb = sent[-1][2].inline_keyboard
assert kb[0][0].web_app.url.startswith("https://crm.example.com/cash/?v=") and "&k=" in kb[0][0].web_app.url, kb[0][0].web_app.url
print("bot cash tests OK")
