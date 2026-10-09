"""The amoCRM-style core: the DEAL is the centre. Chats from any channel are
attached to deals; a chat nobody attached yet is «Неразобранное» (unsorted);
the deal card is a timeline — messages, notes, tasks — with a composer.
"""
import logging
from datetime import date, datetime

from . import crm, database, inbox

logger = logging.getLogger("nova.amo")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_deal_chats (
    deal_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    linked_at TEXT,
    PRIMARY KEY (deal_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_deal_chats_chat ON crm_deal_chats(chat_id);

CREATE TABLE IF NOT EXISTS crm_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER,
    client_id INTEGER,
    text TEXT NOT NULL,
    author TEXT,
    at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_deal ON crm_notes(deal_id);

-- unsorted chats the team dismissed (spam, suppliers, not a guest)
CREATE TABLE IF NOT EXISTS crm_unsorted_rejected (
    chat_id INTEGER PRIMARY KEY,
    at TEXT
);
"""


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Unsorted
# ---------------------------------------------------------------------------
def unsorted() -> list[dict]:
    """Chats with an incoming message that belong to no deal and were not dismissed."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT c.* FROM inbox_chats c WHERE c.last_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM crm_deal_chats l WHERE l.chat_id = c.id) "
            "AND NOT EXISTS (SELECT 1 FROM crm_unsorted_rejected r WHERE r.chat_id = c.id) "
            "AND EXISTS (SELECT 1 FROM inbox_messages m WHERE m.chat_id = c.id AND m.direction = 'in') "
            "ORDER BY c.last_at DESC LIMIT 100").fetchall()
    return [inbox._chat_out(r) for r in rows]  # noqa: SLF001


def _client_for_chat(chat: dict) -> int:
    src = chat.get("channel_name") or "WhatsApp"
    if chat.get("phone"):
        return crm.ensure_client(chat["phone"], chat.get("name") or chat.get("push_name") or "", src, chat["id"])
    return crm.ensure_client_for_chat(chat["id"], chat.get("name") or chat.get("push_name") or "", src)


def accept(chat_id: int, uid: int, pipeline_id: int | None = None) -> dict:
    """«Принять»: attach the chat to the client's open deal, or create a deal."""
    chat = inbox.get_chat(chat_id)
    if not chat:
        raise ValueError("Чат не найден")
    cid = _client_for_chat(chat)
    with database.get_conn() as conn:
        open_deal = conn.execute(
            "SELECT d.id FROM crm_deals d JOIN crm_stages s ON s.id = d.stage_id WHERE d.client_id = ? AND s.kind = 'open' "
            "ORDER BY d.updated_at DESC LIMIT 1", (cid,)).fetchone()
    if open_deal:
        did = open_deal["id"]
    else:
        b = chat.get("booking")
        data = {"client_id": cid, "title": chat["title"], "pipeline_id": pipeline_id}
        if b:
            data.update(apartment=b.get("apartment"), checkin=b.get("begin"), checkout=b.get("end"))
        did = crm.save_deal(None, data, uid)["id"]
    link_chat(did, chat_id)
    return deal_full(did)


def on_incoming(chat_id: int) -> None:
    """Incoming message → the chat lands in «Сделки» by itself: a new deal in the
    first stage (setting «auto_deal», on by default), or a bump of the deal the
    chat already belongs to, so the board shows who wrote last."""
    with database.get_conn() as conn:
        linked = [r[0] for r in conn.execute("SELECT deal_id FROM crm_deal_chats WHERE chat_id = ?", (chat_id,)).fetchall()]
        if linked:
            conn.execute(f"UPDATE crm_deals SET updated_at = ? WHERE id IN ({','.join('?' * len(linked))})", (crm._now(), *linked))
            return
        if conn.execute("SELECT 1 FROM crm_unsorted_rejected WHERE chat_id = ?", (chat_id,)).fetchone():
            return
    if crm.get_setting("auto_deal", "1") != "1":
        return
    if not inbox.get_chat(chat_id):
        return
    admins = [u for u in crm.list_users() if u["role"] == "admin" and u["active"]]
    uid = admins[0]["id"] if admins else 0
    d = accept(chat_id, uid)
    # a brand-new contact gets the first status from the list («Новый»)
    cid = d.get("client_id")
    if cid:
        c = crm.get_client(cid)
        if c and not c.get("status"):
            sts = crm.client_statuses()
            if sts:
                crm.set_client_status(cid, sts[0]["name"])


def reject(chat_id: int) -> None:
    with database.get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO crm_unsorted_rejected (chat_id, at) VALUES (?, ?)", (chat_id, crm._now()))


# ---------------------------------------------------------------------------
# Deal ↔ chats
# ---------------------------------------------------------------------------
def link_chat(did: int, chat_id: int) -> None:
    with database.get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO crm_deal_chats (deal_id, chat_id, linked_at) VALUES (?, ?, ?)", (did, chat_id, crm._now()))
        conn.execute("DELETE FROM crm_unsorted_rejected WHERE chat_id = ?", (chat_id,))
        conn.execute("UPDATE crm_deals SET updated_at = ? WHERE id = ?", (crm._now(), did))


def unlink_chat(did: int, chat_id: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_deal_chats WHERE deal_id = ? AND chat_id = ?", (did, chat_id))


def autolink(did: int) -> int:
    """Attach every chat of the deal's client (by phone, or the chat the card
    was created from). Called after a deal is created."""
    with database.get_conn() as conn:
        d = conn.execute("SELECT client_id, booking_id FROM crm_deals WHERE id = ?", (did,)).fetchone()
        if not d or not d["client_id"]:
            return 0
        c = conn.execute("SELECT phone, chat_id FROM crm_clients WHERE id = ?", (d["client_id"],)).fetchone()
        ids = set()
        if c and c["chat_id"]:
            ids.add(c["chat_id"])
        if c and c["phone"] and len(c["phone"]) >= 7:
            ids.update(r[0] for r in conn.execute("SELECT id FROM inbox_chats WHERE phone LIKE ?", ("%" + c["phone"][-9:],)).fetchall())
        if d["booking_id"]:
            ids.update(r[0] for r in conn.execute("SELECT chat_id FROM crm_booking_chats WHERE booking_id = ?", (d["booking_id"],)).fetchall())
    for cid in ids:
        link_chat(did, cid)
    return len(ids)


def deal_chats(did: int) -> list[dict]:
    with database.get_conn() as conn:
        ids = [r[0] for r in conn.execute("SELECT chat_id FROM crm_deal_chats WHERE deal_id = ?", (did,)).fetchall()]
    out = []
    for cid in ids:
        c = inbox.get_chat(cid)
        if c:
            out.append({k: c.get(k) for k in ("id", "title", "phone", "channel", "channel_name", "last_text", "last_at", "unread")})
    return out


def deal_for_chat(chat_id: int) -> int | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT deal_id FROM crm_deal_chats WHERE chat_id = ? ORDER BY linked_at DESC LIMIT 1", (chat_id,)).fetchone()
    return r["deal_id"] if r else None


# ---------------------------------------------------------------------------
# Notes & timeline
# ---------------------------------------------------------------------------
def add_note(text: str, author: str, deal_id: int | None = None, client_id: int | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустое примечание")
    with database.get_conn() as conn:
        nid = conn.execute("INSERT INTO crm_notes (deal_id, client_id, text, author, at) VALUES (?, ?, ?, ?, ?)",
                           (deal_id, client_id, text, author, crm._now())).lastrowid
        if deal_id:
            conn.execute("UPDATE crm_deals SET updated_at = ? WHERE id = ?", (crm._now(), deal_id))
        return dict(conn.execute("SELECT * FROM crm_notes WHERE id = ?", (nid,)).fetchone())


def delete_note(nid: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_notes WHERE id = ?", (nid,))


def timeline(did: int, client_id: int | None, chat_ids: list[int], limit: int = 150) -> list[dict]:
    items: list[dict] = []
    with database.get_conn() as conn:
        if chat_ids:
            qs = ",".join("?" * len(chat_ids))
            for m in conn.execute(f"SELECT m.*, c.channel, c.name, c.push_name, c.phone FROM inbox_messages m "  # noqa: S608
                                  f"JOIN inbox_chats c ON c.id = m.chat_id WHERE m.chat_id IN ({qs}) ORDER BY m.at DESC, m.id DESC LIMIT ?",
                                  (*chat_ids, limit)).fetchall():
                mm = inbox._msg_out(m)  # noqa: SLF001
                items.append({"type": "message", "at": mm["at"], "id": mm["id"], "chat_id": mm["chat_id"], "direction": mm["direction"],
                              "author": mm["author"], "kind": mm["kind"], "text": mm["text"], "media_url": mm.get("media_url"),
                              "file_name": mm.get("file_name"), "status": mm["status"], "wa_id": mm["wa_id"],
                              "channel": inbox.CHANNELS.get(m["channel"] or "wa", m["channel"]), "channel_code": m["channel"] or "wa",
                              "chat_title": m["name"] or m["push_name"] or (("+" + m["phone"]) if m["phone"] else "")})
        where = "deal_id = ?" + (" OR client_id = ?" if client_id else "")
        args = (did, client_id) if client_id else (did,)
        for n in conn.execute(f"SELECT * FROM crm_notes WHERE {where} ORDER BY id DESC LIMIT 100", args).fetchall():  # noqa: S608
            items.append({"type": "note", "at": n["at"], "id": n["id"], "text": n["text"], "author": n["author"]})
        names = crm.user_names()
        for t in conn.execute("SELECT * FROM crm_tasks WHERE deal_id = ? ORDER BY id DESC LIMIT 100", (did,)).fetchall():
            items.append({"type": "task", "at": t["created_at"] or "", "id": t["id"], "text": t["title"], "due": t["due"],
                          "status": t["status"], "done_at": t["done_at"], "assignee": names.get(t["assignee_uid"])})
        for l in conn.execute("SELECT * FROM crm_auto_log WHERE chat_id IN (SELECT chat_id FROM crm_deal_chats WHERE deal_id = ?) "
                              "AND status != 'sent' ORDER BY id DESC LIMIT 20", (did,)).fetchall():
            items.append({"type": "event", "at": l["at"], "text": f"Автосообщение «{l['rule_name']}»: {l['status']} {l['error'] or ''}".strip()})
        d = conn.execute("SELECT created_at, closed_at FROM crm_deals WHERE id = ?", (did,)).fetchone()
        if d:
            items.append({"type": "event", "at": d["created_at"], "text": "Сделка создана"})
            if d["closed_at"]:
                items.append({"type": "event", "at": d["closed_at"], "text": "Сделка закрыта"})
    items.sort(key=lambda x: (x.get("at") or "", x.get("id") or 0))
    return items[-limit:]


def deal_full(did: int) -> dict | None:
    d = crm.get_deal(did)
    if not d:
        return None
    d["client"] = crm.get_client(d["client_id"]) if d["client_id"] else None
    if d["client"]:
        d["client"].pop("deals", None)
        d["client"].pop("tasks", None)
    d["chats"] = deal_chats(did)
    d["timeline"] = timeline(did, d["client_id"], [c["id"] for c in d["chats"]])
    if d.get("booking_id"):
        with database.get_conn() as conn:
            b = conn.execute("SELECT * FROM bookings WHERE id = ?", (d["booking_id"],)).fetchone()
        d["booking"] = crm._booking_out(dict(b)) if b else None  # noqa: SLF001
    return d


# ---------------------------------------------------------------------------
# Kanban with activity
# ---------------------------------------------------------------------------
def kanban(pipeline_id: int, q: str = "") -> dict:
    deals = crm.deals(pipeline_id, q=q)
    ids = [d["id"] for d in deals]
    last: dict = {}
    nxt: dict = {}
    unread: dict = {}
    chan: dict = {}
    if ids:
        qs = ",".join("?" * len(ids))
        with database.get_conn() as conn:
            for r in conn.execute(f"SELECT l.deal_id, c.last_at, c.last_text, c.last_dir, c.unread, c.channel FROM crm_deal_chats l "  # noqa: S608
                                  f"JOIN inbox_chats c ON c.id = l.chat_id WHERE l.deal_id IN ({qs})", ids).fetchall():
                did = r["deal_id"]
                unread[did] = unread.get(did, 0) + (r["unread"] or 0)
                chan.setdefault(did, set()).add(r["channel"] or "wa")
                if (r["last_at"] or "") > (last.get(did, {}).get("at") or ""):
                    last[did] = {"at": r["last_at"], "text": r["last_text"], "dir": r["last_dir"]}
            for r in conn.execute(f"SELECT deal_id, MIN(due) AS due FROM crm_tasks WHERE status = 'open' AND deal_id IN ({qs}) GROUP BY deal_id",  # noqa: S608
                                  ids).fetchall():
                nxt[r["deal_id"]] = r["due"]
    now = datetime.now().isoformat(timespec="minutes")
    today = date.today().isoformat()
    for d in deals:
        d["last_message"] = last.get(d["id"])
        d["unread"] = unread.get(d["id"], 0)
        d["channels"] = sorted(chan.get(d["id"], set()))
        due = nxt.get(d["id"])
        d["task_due"] = due
        d["task_state"] = None if due is None else "overdue" if due < now else "today" if due[:10] == today else "later"
        if due is None and d["id"] in nxt:
            d["task_state"] = "later"
        d["activity_at"] = max(d["updated_at"] or "", (d["last_message"] or {}).get("at") or "")
    deals.sort(key=lambda d: d["activity_at"], reverse=True)
    return {"deals": deals, "unsorted": unsorted() if not q else []}


def home(uid: int) -> dict:
    """Рабочий стол: what needs attention today."""
    base = crm.home(uid)
    base["unsorted"] = len(unsorted())
    now = datetime.now().isoformat(timespec="minutes")
    with database.get_conn() as conn:
        base["no_task_deals"] = conn.execute(
            "SELECT COUNT(*) FROM crm_deals d JOIN crm_stages s ON s.id = d.stage_id WHERE s.kind = 'open' "
            "AND NOT EXISTS (SELECT 1 FROM crm_tasks t WHERE t.deal_id = d.id AND t.status = 'open')").fetchone()[0]
        base["my_overdue"] = conn.execute("SELECT COUNT(*) FROM crm_tasks WHERE status = 'open' AND assignee_uid = ? AND due < ?",
                                          (uid, now)).fetchone()[0]
        base["unanswered"] = [inbox._chat_out(r) for r in conn.execute(  # noqa: SLF001
            "SELECT * FROM inbox_chats WHERE last_dir = 'in' AND unread > 0 ORDER BY last_at LIMIT 10").fetchall()]
    base["my_tasks"] = crm.tasks(uid, "open")[:15]
    return base
