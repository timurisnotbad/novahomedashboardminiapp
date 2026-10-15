"""AI assistant for guest chats (Claude).

Per chat: off / draft / auto. A cheap triage model decides whether the message
needs an answer at all and whether a person is needed; the main model answers
with the house rules, the knowledge base, the guest's memory and history, and
a few tools (availability, arrival time, lead, handover). Every call is logged
with its cost; a monthly budget turns auto mode into drafts.

Learning: real past question→answer pairs of the team (ai_examples) are found
by full-text search and shown to the model as examples; a draft the manager
edited before sending is stored as an example too.
"""
import base64
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import bg, config, database, util

logger = logging.getLogger("nova.ai")

AUTHOR = "ИИ-ассистент"
MODELS = {  # $ per 1M tokens: input, output, cache read, cache write
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
MODEL_NAMES = {"claude-opus-5-5": "Claude Opus 5.5 — лучшее качество", "claude-sonnet-5-5": "Claude Sonnet 5.5 — дешевле вдвое",
               "claude-haiku-5-5": "Claude Haiku 5.5 — самый дешёвый"}
DEFAULTS = {"ai_mode": "draft", "ai_model": "claude-opus-5-5", "ai_model_triage": "claude-haiku-5-5", "ai_budget": "30",
            "ai_channels": "wa,tg,wztg,wz", "ai_pause_hours": "2", "ai_max_turns": "8", "ai_debounce": "20",
            "ai_rules": "", "ai_knowledge": ""}
DEFAULT_RULES = """Ты — ассистент апартаментов Nova Home (Nest One, Ташкент). Отвечаешь гостям в мессенджере от имени команды.
Цель: быстро и точно закрыть простые вопросы, довести запрос на бронь до готовой заявки и передать человеку, когда нужно решение.
Правила:
- Отвечай коротко, тепло, без канцелярита, на языке гостя (русский, английский, узбекский).
- Цены, свободные даты, адрес, Wi‑Fi, условия — только из базы знаний и из данных CRM. Если данных нет — честно скажи, что уточнишь, и вызови handover.
- Никаких скидок, обещаний и подтверждений брони от своего имени. Подтверждает бронь только человек.
- Передавай человеку (handover): жалоба, спор о деньгах, просьба позвонить, поломка в квартире, нестандартная просьба, гость раздражён, вопрос вне твоих данных.
- Если гость хочет забронировать: уточни даты, число гостей, пожелания, собери контакт — и сохрани заявку (save_lead), после чего сообщи, что менеджер подтвердит бронь.
- Если гость назвал время приезда — сохрани его (set_arrival_time).
- Не выдумывай. Не повторяй одно и то же. Не пиши, что ты ИИ, если не спросят прямо; если спросят — скажи честно."""

_state: dict = {"pending": {}}  # chat_id → timestamp of the last scheduled run (debounce)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def init_db() -> None:
    with database.get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(inbox_chats)").fetchall()]
        for col, decl in (("ai_mode", "TEXT"), ("ai_paused_until", "TEXT"), ("ai_draft", "TEXT"), ("ai_draft_at", "TEXT"),
                          ("ai_turns", "INTEGER DEFAULT 0"), ("ai_note", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE inbox_chats ADD COLUMN {col} {decl}")  # noqa: S608
        ccols = [r[1] for r in conn.execute("PRAGMA table_info(crm_clients)").fetchall()]
        if ccols and "ai_memory" not in ccols:
            conn.execute("ALTER TABLE crm_clients ADD COLUMN ai_memory TEXT")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER, kind TEXT, model TEXT, decision TEXT, text TEXT,
            input_tokens INTEGER DEFAULT 0, cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0, ms INTEGER DEFAULT 0, error TEXT, at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ai_log_at ON ai_log(at);
        CREATE INDEX IF NOT EXISTS idx_ai_log_chat ON ai_log(chat_id);
        CREATE TABLE IF NOT EXISTS ai_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            q TEXT NOT NULL, a TEXT NOT NULL, source TEXT, chat_id INTEGER, at TEXT, rating INTEGER DEFAULT 0
        );
        """)
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS ai_examples_fts USING fts5(q, a, content='ai_examples', content_rowid='id')")
            conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS ai_examples_ai AFTER INSERT ON ai_examples BEGIN
              INSERT INTO ai_examples_fts(rowid, q, a) VALUES (new.id, new.q, new.a); END;
            CREATE TRIGGER IF NOT EXISTS ai_examples_ad AFTER DELETE ON ai_examples BEGIN
              INSERT INTO ai_examples_fts(ai_examples_fts, rowid, q, a) VALUES ('delete', old.id, old.q, old.a); END;
            """)
            _state["fts"] = True
        except Exception as exc:  # noqa: BLE001 — SQLite without FTS5: LIKE search instead
            logger.warning("FTS5 unavailable (%s) — example search falls back to LIKE", exc)
            _state["fts"] = False


def api_key() -> str:
    return (config.ANTHROPIC_API_KEY or "").strip()


def set_api_key(key: str) -> None:
    from . import secrets_store
    key = (key or "").strip()
    secrets_store.save_extra(config.BASE_DIR / "data", ai_api_key=key)
    config.ANTHROPIC_API_KEY = key
    _state.pop("client", None)


def enabled() -> bool:
    return bool(api_key())


def _client():
    if "client" not in _state:
        import anthropic
        _state["client"] = anthropic.Anthropic(api_key=api_key(), max_retries=2, timeout=90.0)
    return _state["client"]


def setting(key: str) -> str:
    from . import crm
    return crm.get_setting(key, DEFAULTS.get(key, ""))


def settings() -> dict:
    out = {k: setting(k) for k in DEFAULTS}
    out["ai_rules"] = out["ai_rules"] or DEFAULT_RULES
    out["key_set"] = enabled()
    out["models"] = MODEL_NAMES
    return out


def save_settings(data: dict) -> dict:
    from . import crm
    for k in DEFAULTS:
        if k in data and data[k] is not None:
            v = str(data[k]).strip()
            if k in ("ai_model", "ai_model_triage") and v not in MODELS:
                continue
            if k == "ai_mode" and v not in ("off", "draft", "auto"):
                continue
            crm.set_setting(k, v[:20000])
    if data.get("api_key"):
        set_api_key(str(data["api_key"]))
    return settings()


# ---------------------------------------------------------------------------
# Cost / log
# ---------------------------------------------------------------------------
def _cost(model: str, usage) -> float:
    p = MODELS.get(model) or MODELS["claude-opus-5-5"]
    i = getattr(usage, "input_tokens", 0) or 0
    o = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return round((i * p[0] + o * p[1] + cr * p[2] + cw * p[3]) / 1_000_000, 6)


def _log(chat_id, kind, model, decision, text, usage=None, ms=0, error=None) -> float:
    cost = _cost(model, usage) if usage is not None else 0.0
    with database.get_conn() as conn:
        conn.execute("INSERT INTO ai_log (chat_id, kind, model, decision, text, input_tokens, cache_read, cache_write, output_tokens, cost, ms, error, at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (chat_id, kind, model, decision, (text or "")[:4000],
                      getattr(usage, "input_tokens", 0) or 0, getattr(usage, "cache_read_input_tokens", 0) or 0,
                      getattr(usage, "cache_creation_input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
                      cost, int(ms), (error or "")[:500] or None, util.now()))
    return cost


def month_cost() -> float:
    with database.get_conn() as conn:
        r = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM ai_log WHERE at >= ?", (date.today().replace(day=1).isoformat(),)).fetchone()
    return round(r[0] or 0, 4)


def budget_left() -> float:
    try:
        return float(setting("ai_budget") or 0) - month_cost()
    except ValueError:
        return 0.0


def stats() -> dict:
    today = date.today().isoformat()
    month = date.today().replace(day=1).isoformat()
    with database.get_conn() as conn:
        def agg(since):
            r = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(cost), 0) AS cost, "
                             "SUM(kind = 'reply' AND decision = 'sent') AS sent, SUM(kind = 'reply' AND decision = 'draft') AS drafts, "
                             "SUM(decision = 'handover') AS handover, SUM(kind = 'triage' AND decision = 'skip') AS skipped, "
                             "SUM(error IS NOT NULL) AS errors FROM ai_log WHERE at >= ?", (since,)).fetchone()
            return {k: (r[k] or 0) for k in r.keys()}
        recent = [dict(r) for r in conn.execute("SELECT l.*, c.name AS chat_name, c.push_name FROM ai_log l LEFT JOIN inbox_chats c ON c.id = l.chat_id "
                                                "ORDER BY l.id DESC LIMIT 30").fetchall()]
        examples = conn.execute("SELECT COUNT(*) FROM ai_examples").fetchone()[0]
        chats = {r["ai_mode"] or "default": r["n"] for r in conn.execute("SELECT ai_mode, COUNT(*) AS n FROM inbox_chats GROUP BY ai_mode").fetchall()}
        t_agg, m_agg = agg(today), agg(month)
    return {"today": t_agg, "month": m_agg, "budget": float(setting("ai_budget") or 0), "budget_left": round(budget_left(), 2),
            "recent": recent, "examples": examples, "chats_by_mode": chats, "enabled": enabled(), "mode": setting("ai_mode")}


# ---------------------------------------------------------------------------
# Per-chat mode
# ---------------------------------------------------------------------------
def chat_mode(chat: dict) -> str:
    """off | draft | auto for this chat: its own setting, else the global one;
    channels outside ai_channels are off; a paused chat (a person answered) is draft."""
    if not enabled():
        return "off"
    mode = chat.get("ai_mode") or setting("ai_mode") or "off"
    if mode == "off":
        return "off"
    if (chat.get("channel") or "wa") not in [c.strip() for c in setting("ai_channels").split(",") if c.strip()]:
        return "off"
    if mode == "auto":
        if (chat.get("ai_paused_until") or "") > util.now():
            return "draft"
        if budget_left() <= 0:
            return "draft"
    return mode


def set_chat_mode(chat_id: int, mode: str | None) -> None:
    from . import inbox
    with database.get_conn() as conn:
        conn.execute("UPDATE inbox_chats SET ai_mode = ?, ai_paused_until = NULL, ai_turns = 0, rev = ? WHERE id = ?",
                     (mode if mode in ("off", "draft", "auto") else None, inbox._bump(conn), chat_id))  # noqa: SLF001


def pause_for_human(chat_id: int, sent_text: str = "") -> None:
    """A person answered in this chat: the assistant steps back for ai_pause_hours.
    If the person edited the assistant's draft, the pair becomes an example."""
    from . import inbox
    try:
        hours = float(setting("ai_pause_hours") or 2)
    except ValueError:
        hours = 2.0
    until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    with database.get_conn() as conn:
        r = conn.execute("SELECT ai_draft FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
        draft = (r["ai_draft"] if r else "") or ""
        conn.execute("UPDATE inbox_chats SET ai_paused_until = ?, ai_draft = NULL, ai_turns = 0, rev = ? WHERE id = ?",
                     (until, inbox._bump(conn), chat_id))  # noqa: SLF001
        if draft and sent_text and sent_text.strip() != draft.strip():
            last_in = conn.execute("SELECT text FROM inbox_messages WHERE chat_id = ? AND direction = 'in' AND kind = 'text' ORDER BY id DESC LIMIT 1",
                                   (chat_id,)).fetchone()
            if last_in and last_in["text"]:
                conn.execute("INSERT INTO ai_examples (q, a, source, chat_id, at) VALUES (?, ?, 'edited', ?, ?)",
                             (last_in["text"][:1000], sent_text.strip()[:2000], chat_id, util.now()))
    _state["pending"].pop(chat_id, None)


# ---------------------------------------------------------------------------
# Examples («learning» from the team's own answers)
# ---------------------------------------------------------------------------
def import_examples(max_pairs: int = 3000) -> int:
    """Past chats → (guest question, team answer) pairs: a guest text message
    followed within 24 h by a human text reply of reasonable length. No model
    calls, nothing leaves the server."""
    n = 0
    with database.get_conn() as conn:
        have = {(r["chat_id"], r["q"][:200]) for r in conn.execute("SELECT chat_id, q FROM ai_examples").fetchall()}
        rows = conn.execute("SELECT chat_id, direction, author, kind, text, at FROM inbox_messages WHERE kind = 'text' AND text != '' "
                            "ORDER BY chat_id, at, id").fetchall()
        prev = None
        for r in rows:
            if prev and prev["chat_id"] == r["chat_id"] and prev["direction"] == "in" and r["direction"] == "out" \
                    and (r["author"] or "") not in (AUTHOR, "Автоответ", "Телефон") \
                    and 8 <= len(prev["text"]) <= 1000 and 15 <= len(r["text"]) <= 2000 \
                    and (r["at"] or "") <= (datetime.fromisoformat(prev["at"]) + timedelta(hours=24)).isoformat(timespec="seconds") \
                    and not re.search(r"\{[а-яa-z ]+\}", r["text"]) and (r["chat_id"], prev["text"][:200]) not in have:
                conn.execute("INSERT INTO ai_examples (q, a, source, chat_id, at) VALUES (?, ?, 'history', ?, ?)",
                             (prev["text"], r["text"], r["chat_id"], r["at"]))
                have.add((r["chat_id"], prev["text"][:200]))
                n += 1
                if n >= max_pairs:
                    break
            prev = r
    return n


def find_examples(text: str, limit: int = 5) -> list[dict]:
    words = [w for w in re.findall(r"[\w']{3,}", (text or "").lower()) if not w.isdigit()][:12]
    if not words:
        return []
    with database.get_conn() as conn:
        if _state.get("fts"):
            q = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
            try:
                rows = conn.execute("SELECT e.q, e.a FROM ai_examples_fts f JOIN ai_examples e ON e.id = f.rowid WHERE ai_examples_fts MATCH ? "
                                    "AND e.rating >= 0 ORDER BY bm25(ai_examples_fts), e.source = 'edited' DESC LIMIT ?", (q, limit)).fetchall()
                return [dict(r) for r in rows]
            except Exception:  # noqa: BLE001
                pass
        like = " OR ".join("q LIKE ?" for _ in words[:5])
        rows = conn.execute(f"SELECT q, a FROM ai_examples WHERE rating >= 0 AND ({like}) ORDER BY id DESC LIMIT ?",  # noqa: S608
                            [f"%{w}%" for w in words[:5]] + [limit]).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Context for the model
# ---------------------------------------------------------------------------
def _client_for_chat(conn, chat: dict):
    r = conn.execute("SELECT * FROM crm_clients WHERE chat_id = ?", (chat["id"],)).fetchone()
    if not r and chat.get("phone"):
        r = conn.execute("SELECT * FROM crm_clients WHERE phone LIKE ?", ("%" + chat["phone"][-9:],)).fetchone()
    return dict(r) if r else None


def _history(conn, chat_id: int, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT id, direction, author, kind, text, media, mime, at FROM inbox_messages WHERE chat_id = ? "
                        "ORDER BY at DESC, id DESC LIMIT ?", (chat_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def _image_block(media: str, mime: str) -> dict | None:
    try:
        p = Path(config.INBOX_MEDIA_DIR) / media
        if not p.is_file() or p.stat().st_size > 4_500_000:
            return None
        if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(p.read_bytes()).decode()}}
    except Exception:  # noqa: BLE001
        return None


def _messages_for_model(history: list[dict]) -> list[dict]:
    """The chat as alternating turns; images of the last incoming messages are
    attached so the model can read a passport, a receipt or a photo of a problem."""
    out = []
    images_left = 3
    for i, m in enumerate(history):
        role = "user" if m["direction"] == "in" else "assistant"
        when = (m.get("at") or "")[:16].replace("T", " ")
        kind = m.get("kind") or "text"
        text = (m.get("text") or "").strip()
        if kind == "text":
            body = text
        elif kind == "image":
            body = (f"[фото{': ' + text if text else ''}]")
        elif kind == "audio":
            body = "[голосовое сообщение — расшифровка пока недоступна, попроси написать текстом, если важно]"
        elif kind == "document":
            body = f"[файл {text or ''}]"
        elif kind == "location":
            body = "[геолокация]"
        else:
            body = f"[{kind}{': ' + text if text else ''}]"
        content: list = []
        if role == "user" and kind == "image" and m.get("media") and images_left > 0 and i >= len(history) - 10:
            blk = _image_block(m["media"], m.get("mime") or "")
            if blk:
                content.append(blk)
                images_left -= 1
        who = "" if role == "user" else f"[{m.get('author') or 'менеджер'}] "
        content.append({"type": "text", "text": f"({when}) {who}{body}"})
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": role, "content": content})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(начало переписки)"}]})
    if out[-1]["role"] != "user":
        out.append({"role": "user", "content": [{"type": "text", "text": "(гость ждёт ответа)"}]})
    return out


def _context(chat_id: int) -> dict | None:
    from . import crm, inbox
    chat = inbox.get_chat(chat_id)
    if not chat:
        return None
    with database.get_conn() as conn:
        client = _client_for_chat(conn, chat)
        history = _history(conn, chat_id)
    stays = []
    if client and client.get("phone"):
        try:
            stays = crm.client_bookings(client["phone"])[:8]
        except Exception:  # noqa: BLE001
            stays = []
    last_in = next((m["text"] for m in reversed(history) if m["direction"] == "in" and m.get("text")), "")
    return {"chat": chat, "client": client, "history": history, "stays": stays, "last_in": last_in,
            "examples": find_examples(last_in) if last_in else []}


def _system(ctx: dict) -> list[dict]:
    """Stable part first (cached across chats), then the per-chat facts."""
    from . import crm, docs
    rules = setting("ai_rules") or DEFAULT_RULES
    knowledge = setting("ai_knowledge") or "(база знаний пока не заполнена)"
    co = docs.company()
    apt_rows = crm.apartment_info()
    apt_lines = [f"- {r['apartment'] if r['kind'] == 'apt' else ('блок ' + r['apartment'] if r['kind'] == 'block' else 'все квартиры')}: "
                 + "; ".join(f"{k}: {r[f]}" for k, f in (("домофон", "door_code"), ("ключи", "keys_in"), ("ключи при выезде", "keys_out"), ("Wi‑Fi", "wifi"), ("заметка", "note")) if r.get(f))
                 for r in apt_rows if any(r.get(f) for f in crm.APT_FIELDS)]
    ci, co_t = crm.default_times()
    stable = (f"{rules}\n\n## Компания\n{co.get('name') or 'Nova Home'} · адрес: {co.get('address') or '—'} · телефон: {co.get('phone') or '—'} · "
              f"заезд с {ci}, выезд до {co_t}.\n\n## База знаний (правда только то, что здесь и в данных CRM)\n{knowledge}\n\n"
              f"## Квартиры\n" + ("\n".join(apt_lines) or "(нет данных)"))
    chat, client, stays = ctx["chat"], ctx["client"], ctx["stays"]
    b = chat.get("booking")
    facts = [f"Сегодня {date.today().isoformat()}, время {datetime.now().strftime('%H:%M')}.",
             f"Канал: {chat.get('channel_name')}. Гость: {chat.get('title')}" + (f", телефон +{chat['phone']}" if chat.get("phone") else "") + "."]
    if client:
        facts.append(f"Карточка гостя: {client.get('name') or ''}; статус: {client.get('status') or '—'}; язык: {client.get('lang') or '—'}; "
                     f"особенности: {client.get('notes') or '—'}.")
        if client.get("ai_memory"):
            facts.append(f"Память о госте (из прошлых разговоров): {client['ai_memory']}")
    if b:
        facts.append(f"Текущая бронь: {b.get('apartment')}, {b.get('begin')} → {b.get('end')}, {b.get('when')}; "
                     f"время заезда {b.get('arrival_time') or 'не указано'}, приедет в: {b.get('eta') or 'не уточнено'}.")
    else:
        facts.append("Брони в календаре у этого гостя сейчас нет.")
    if stays:
        facts.append("Прошлые/будущие заезды: " + "; ".join(f"{s.get('apartment')} {s.get('checkin')}–{s.get('checkout')} ({s.get('pay')})" for s in stays))
    if ctx["examples"]:
        facts.append("## Как команда отвечала на похожие вопросы (стиль и факты — ориентир, не копируй дословно)\n" +
                     "\n".join(f"Гость: {e['q'][:300]}\nМенеджер: {e['a'][:500]}" for e in ctx["examples"]))
    return [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "\n".join(facts)}]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
TOOLS = [
    {"name": "check_availability", "description": "Свободные квартиры на даты (из календаря броней). Вызывай перед тем, как говорить о свободных датах.",
     "strict": True, "input_schema": {"type": "object", "properties": {"checkin": {"type": "string", "description": "YYYY-MM-DD"}, "checkout": {"type": "string", "description": "YYYY-MM-DD"}},
                                      "required": ["checkin", "checkout"], "additionalProperties": False}},
    {"name": "set_arrival_time", "description": "Гость назвал время приезда — сохранить для горничных.", "strict": True,
     "input_schema": {"type": "object", "properties": {"time": {"type": "string", "description": "HH:MM"}, "note": {"type": "string", "description": "откуда / рейс / комментарий"}},
                      "required": ["time", "note"], "additionalProperties": False}},
    {"name": "save_lead", "description": "Заявка на бронь готова (даты, гости, контакт): сохранить сделку и поставить задачу менеджеру подтвердить бронь.", "strict": True,
     "input_schema": {"type": "object", "properties": {"checkin": {"type": "string"}, "checkout": {"type": "string"}, "guests": {"type": "integer"},
                                                       "apartment": {"type": "string", "description": "пожелание по квартире/блоку или пусто"},
                                                       "name": {"type": "string"}, "phone": {"type": "string"}, "notes": {"type": "string"}},
                      "required": ["checkin", "checkout", "guests", "apartment", "name", "phone", "notes"], "additionalProperties": False}},
    {"name": "handover", "description": "Передать разговор человеку. После вызова ты больше не отвечаешь в этом чате; скажи гостю, что менеджер скоро ответит.", "strict": True,
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}, "summary": {"type": "string", "description": "2–4 строки для менеджера: что нужно гостю"}},
                      "required": ["reason", "summary"], "additionalProperties": False}},
]


def _free_apartments(checkin: str, checkout: str) -> dict:
    try:
        d0, d1 = date.fromisoformat(checkin[:10]), date.fromisoformat(checkout[:10])
    except ValueError:
        return {"error": "неверные даты"}
    if d1 <= d0:
        return {"error": "выезд должен быть позже заезда"}
    busy = set()
    for b in database.get_bookings(d0.isoformat(), (d1 - timedelta(days=1)).isoformat()):
        if (b.get("status") or "") in ("cancelled", "canceled"):
            continue
        if b["begin_date"] < d1.isoformat() and b["end_date"] > d0.isoformat():
            busy.add(b["apartment_name"])
    names = sorted(set(config.APARTMENTS.values()))
    free = [n for n in names if n not in busy]
    return {"checkin": d0.isoformat(), "checkout": d1.isoformat(), "nights": (d1 - d0).days, "free": free, "free_count": len(free), "total": len(names)}


def _run_tool(name: str, inp: dict, ctx: dict, state: dict) -> str:
    from . import crm, crm_amo, notify
    chat = ctx["chat"]
    try:
        if name == "check_availability":
            return json.dumps(_free_apartments(inp.get("checkin", ""), inp.get("checkout", "")), ensure_ascii=False)
        if name == "set_arrival_time":
            b = chat.get("booking")
            if not b:
                return json.dumps({"ok": False, "error": "у гостя нет брони в календаре — просто запомни время в ответе"}, ensure_ascii=False)
            crm.set_eta(b["id"], inp.get("time", ""), inp.get("note", ""), AUTHOR)
            return json.dumps({"ok": True}, ensure_ascii=False)
        if name == "save_lead":
            cid = None
            if ctx["client"]:
                cid = ctx["client"]["id"]
            elif inp.get("phone") and len(util.digits(inp["phone"])) >= 7:
                cid = crm.ensure_client(inp["phone"], inp.get("name") or chat.get("title") or "", chat.get("channel_name") or "", chat["id"])
            else:
                cid = crm.ensure_client_for_chat(chat["id"], inp.get("name") or chat.get("title") or "", chat.get("channel_name") or "")
            summary = (f"Заявка от ИИ: {inp.get('checkin')} → {inp.get('checkout')}, гостей {inp.get('guests')}, квартира: {inp.get('apartment') or 'любая'}, "
                       f"{inp.get('name') or ''} {inp.get('phone') or ''}. {inp.get('notes') or ''}").strip()
            admins = [u for u in crm.list_users() if u["role"] == "admin" and u["active"]]
            uid = admins[0]["id"] if admins else 0
            deal = crm.save_deal(None, {"client_id": cid, "checkin": inp.get("checkin"), "checkout": inp.get("checkout"), "guests": inp.get("guests"),
                                        "apartment": inp.get("apartment") or "", "notes": summary,
                                        "title": f"{inp.get('name') or chat.get('title')} · {inp.get('checkin')}–{inp.get('checkout')}"}, uid)
            try:
                crm_amo.link_chat(deal["id"], chat["id"])
            except Exception:  # noqa: BLE001
                pass
            crm.save_task(None, {"title": f"Подтвердить бронь: {inp.get('name') or chat.get('title')} {inp.get('checkin')}–{inp.get('checkout')}, {inp.get('guests')} гост.",
                                 "due": (datetime.now() + timedelta(hours=1)).isoformat(timespec="minutes"), "assignee_uid": uid, "deal_id": deal["id"], "client_id": cid}, uid)
            state["lead"] = deal["id"]
            try:
                notify.send(f"🤖 Новая заявка из чата ({chat.get('channel_name')}): {summary}\n{config.WEBAPP_URL.rstrip('/')}/crm/#messages/{chat['id']}" if config.WEBAPP_URL else f"🤖 Новая заявка из чата: {summary}")
            except Exception:  # noqa: BLE001
                pass
            return json.dumps({"ok": True, "deal_id": deal["id"]}, ensure_ascii=False)
        if name == "handover":
            state["handover"] = {"reason": inp.get("reason") or "", "summary": inp.get("summary") or ""}
            return json.dumps({"ok": True, "note": "менеджер уведомлён; коротко скажи гостю, что менеджер скоро ответит"}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ai tool %s failed", name)
        return json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False)
    return json.dumps({"ok": False, "error": "unknown tool"})


def _do_handover(chat_id: int, info: dict, mode: str) -> None:
    from . import crm, inbox, notify
    text = f"🙋 ИИ передал чат человеку: {info.get('reason') or ''}\n{info.get('summary') or ''}".strip()
    with database.get_conn() as conn:
        conn.execute("UPDATE inbox_chats SET ai_paused_until = ?, ai_note = ?, ai_turns = 0, unread = CASE WHEN unread > 0 THEN unread ELSE 1 END, "
                     "last_dir = 'in', notify_stage = 0, rev = ? WHERE id = ?",
                     ((datetime.now() + timedelta(days=3)).isoformat(timespec="seconds"), text[:1000], inbox._bump(conn), chat_id))  # noqa: SLF001
    admins = [u for u in crm.list_users() if u["role"] == "admin" and u["active"]]
    try:
        crm.save_task(None, {"title": f"Ответить гостю (ИИ передал: {(info.get('reason') or '')[:60]})", "due": (datetime.now() + timedelta(minutes=30)).isoformat(timespec="minutes"),
                             "assignee_uid": admins[0]["id"] if admins else 0}, admins[0]["id"] if admins else 0)
    except Exception:  # noqa: BLE001
        logger.exception("handover task failed")
    try:
        c = inbox.get_chat(chat_id)
        link = f"\n🔗 {config.WEBAPP_URL.rstrip('/')}/crm/#messages/{chat_id}" if config.WEBAPP_URL else ""
        notify.send(f"🙋 Чат {c['title'] if c else chat_id} ({c.get('channel_name') if c else ''}): нужен человек.\n{info.get('reason') or ''}\n{info.get('summary') or ''}{link}",
                    targets=list(dict.fromkeys(list(config.inbox_notify_targets()) + crm.users_tg_ids())))
    except Exception:  # noqa: BLE001
        logger.exception("handover notify failed")


# ---------------------------------------------------------------------------
# Triage (cheap model): answer? who?
# ---------------------------------------------------------------------------
TRIAGE_SCHEMA = {"type": "object", "properties": {
    "needs_reply": {"type": "boolean", "description": "нужен ли ответ вообще (не нужен: «ок», «спасибо», «я приехал» без вопроса, стикер)"},
    "handover": {"type": "boolean", "description": "нужен человек прямо сейчас (жалоба, деньги, поломка, звонок, злость, вне базы знаний)"},
    "category": {"type": "string", "enum": ["smalltalk", "question", "booking", "arrival", "payment", "complaint", "problem", "other"]},
    "language": {"type": "string", "description": "ru | en | uz | other"},
    "reason": {"type": "string", "description": "одна строка почему"}},
    "required": ["needs_reply", "handover", "category", "language", "reason"], "additionalProperties": False}


def _triage(ctx: dict) -> dict:
    model = setting("ai_model_triage") or "claude-haiku-5-5"
    hist = ctx["history"][-8:]
    lines = [f"{'Гость' if m['direction'] == 'in' else 'Менеджер'}: {(m.get('text') or '[' + (m.get('kind') or '') + ']')[:300]}" for m in hist]
    t0 = time.time()
    resp = _client().messages.create(
        model=model, max_tokens=300, output_config={"effort": "low", "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
        system="Ты сортируешь входящие сообщения гостей апартаментов. Реши: нужен ли ответ ассистента, и нужен ли человек. "
               "Человек нужен при жалобе, споре о деньгах/возврате, поломке, просьбе позвонить, раздражении, нестандартной просьбе. "
               "Простые вопросы (адрес, Wi‑Fi, время заезда, цены, свободные даты, как добраться) и заявки на бронь — ассистент. Отвечай только JSON.",
        messages=[{"role": "user", "content": "Последние сообщения:\n" + "\n".join(lines) + "\n\nПоследнее сообщение гостя: " + (ctx["last_in"] or "[не текст]")[:500]}])
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    try:
        data = json.loads(text)
    except ValueError:
        data = {"needs_reply": True, "handover": False, "category": "other", "language": "ru", "reason": "json?"}
    decision = "handover" if data.get("handover") else ("reply" if data.get("needs_reply") else "skip")
    _log(ctx["chat"]["id"], "triage", model, decision, f"{data.get('category')}/{data.get('language')}: {data.get('reason')}", resp.usage, (time.time() - t0) * 1000)
    return data


# ---------------------------------------------------------------------------
# Reply (main model + tools)
# ---------------------------------------------------------------------------
def _reply(ctx: dict, state: dict) -> str:
    model = setting("ai_model") or "claude-opus-5-5"
    messages = _messages_for_model(ctx["history"])
    system = _system(ctx)
    t0 = time.time()
    text_out, usage_sum = "", {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    class U:  # summed usage for the log
        pass
    for _ in range(5):
        resp = _client().messages.create(model=model, max_tokens=1024, system=system, messages=messages, tools=TOOLS,
                                         output_config={"effort": "low"})
        for k in usage_sum:
            usage_sum[k] += getattr(resp.usage, k, 0) or 0
        text_out = "\n".join(b.text for b in resp.content if b.type == "text").strip() or text_out
        if resp.stop_reason == "refusal":
            text_out = ""
            state["handover"] = state.get("handover") or {"reason": "модель отказалась отвечать", "summary": ctx["last_in"][:300]}
            break
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for u in uses:
            inp = u.input if isinstance(u.input, dict) else {}
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": _run_tool(u.name, inp, ctx, state)})
        messages.append({"role": "user", "content": results})
    u = U()
    for k, v in usage_sum.items():
        setattr(u, k, v)
    state["usage"], state["model"], state["ms"] = u, model, (time.time() - t0) * 1000
    return text_out.strip()


def _update_memory(ctx: dict) -> None:
    """Short, persistent notes about the guest (cheap model), kept on the client card."""
    client = ctx["client"]
    if not client:
        return
    model = setting("ai_model_triage") or "claude-haiku-5-5"
    lines = [f"{'Гость' if m['direction'] == 'in' else 'Мы'}: {(m.get('text') or '[' + (m.get('kind') or '') + ']')[:300]}" for m in ctx["history"][-30:]]
    try:
        resp = _client().messages.create(
            model=model, max_tokens=400, output_config={"effort": "low"},
            system="Обнови краткую память о госте апартаментов для менеджеров: предпочтения, особенности, язык, что обещали, прошлые проблемы. "
                   "До 6 коротких строк, только факты из переписки, без воды. Ответ — только текст памяти.",
            messages=[{"role": "user", "content": f"Текущая память:\n{client.get('ai_memory') or '(пусто)'}\n\nПереписка:\n" + "\n".join(lines)}])
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()[:1500]
        if text:
            with database.get_conn() as conn:
                conn.execute("UPDATE crm_clients SET ai_memory = ?, updated_at = ? WHERE id = ?", (text, util.now(), client["id"]))
        _log(ctx["chat"]["id"], "memory", model, "ok", text, resp.usage)
    except Exception as exc:  # noqa: BLE001
        _log(ctx["chat"]["id"], "memory", model, "error", "", None, 0, str(exc))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def on_incoming(chat_id: int) -> None:
    """Called for every incoming message: wait a little (guests send several
    lines in a row), then handle the chat once."""
    if not enabled():
        return
    try:
        delay = max(3, min(int(setting("ai_debounce") or 20), 120))
    except ValueError:
        delay = 20
    stamp = time.time()
    _state["pending"][chat_id] = stamp

    def later():
        time.sleep(delay)
        if _state["pending"].get(chat_id) != stamp:
            return  # a newer message rescheduled it
        _state["pending"].pop(chat_id, None)
        try:
            handle(chat_id)
        except Exception:  # noqa: BLE001
            logger.exception("ai handle failed for chat %s", chat_id)
    threading.Thread(target=later, daemon=True, name=f"ai-{chat_id}").start()


def handle(chat_id: int, force: bool = False, want_draft: bool = False) -> dict:
    """One pass over the chat: triage → reply → send or draft. force=True is the
    «Ответить с ИИ» button (always a draft, ignores mode/pause/budget)."""
    from . import inbox
    ctx = _context(chat_id)
    if not ctx:
        return {"status": "no chat"}
    chat = ctx["chat"]
    mode = "draft" if force else chat_mode(chat)
    if mode == "off":
        return {"status": "off"}
    if not force:
        last = ctx["history"][-1] if ctx["history"] else None
        if not last or last["direction"] != "in":
            return {"status": "nothing new"}  # the last word is ours
        try:
            tri = _triage(ctx)
        except Exception as exc:  # noqa: BLE001
            _log(chat_id, "triage", setting("ai_model_triage"), "error", "", None, 0, str(exc))
            return {"status": "error", "error": str(exc)[:200]}
        if tri.get("handover"):
            _do_handover(chat_id, {"reason": tri.get("reason") or tri.get("category") or "", "summary": ctx["last_in"][:400]}, mode)
            _log(chat_id, "reply", setting("ai_model"), "handover", tri.get("reason") or "")
            return {"status": "handover"}
        if not tri.get("needs_reply"):
            return {"status": "skip"}
        try:
            if int(chat.get("ai_turns") or 0) >= int(setting("ai_max_turns") or 8) and mode == "auto":
                _do_handover(chat_id, {"reason": "лимит сообщений ИИ без результата", "summary": ctx["last_in"][:400]}, mode)
                return {"status": "handover"}
        except ValueError:
            pass
    state: dict = {}
    try:
        text = _reply(ctx, state)
    except Exception as exc:  # noqa: BLE001
        _log(chat_id, "reply", setting("ai_model"), "error", "", None, 0, str(exc))
        return {"status": "error", "error": str(exc)[:200]}
    if state.get("handover"):
        _do_handover(chat_id, state["handover"], mode)
    decision = "draft"
    if mode == "auto" and not force and text:
        try:
            inbox.send(chat_id, AUTHOR, text)
            decision = "sent"
            with database.get_conn() as conn:
                conn.execute("UPDATE inbox_chats SET ai_turns = COALESCE(ai_turns, 0) + 1, ai_draft = NULL WHERE id = ?", (chat_id,))
        except Exception as exc:  # noqa: BLE001
            decision = "draft"
            _log(chat_id, "send", state.get("model") or "", "error", text, None, 0, str(exc))
    if decision == "draft" and text:
        with database.get_conn() as conn:
            conn.execute("UPDATE inbox_chats SET ai_draft = ?, ai_draft_at = ?, rev = ? WHERE id = ?", (text, util.now(), inbox._bump(conn), chat_id))  # noqa: SLF001
    cost = _log(chat_id, "reply", state.get("model") or "", "handover" if state.get("handover") and not text else decision, text, state.get("usage"), state.get("ms") or 0)
    if state.get("handover") or state.get("lead"):
        bg.run(_update_memory, ctx, key=f"ai-mem-{chat_id}")
    return {"status": decision, "text": text, "cost": cost, "handover": bool(state.get("handover")), "lead": state.get("lead")}


def clear_draft(chat_id: int) -> None:
    from . import inbox
    with database.get_conn() as conn:
        conn.execute("UPDATE inbox_chats SET ai_draft = NULL, rev = ? WHERE id = ?", (inbox._bump(conn), chat_id))  # noqa: SLF001


def rate(chat_id: int, good: bool) -> None:
    """👍 / 👎 on the assistant's last reply: a liked reply becomes an example, a
    disliked one is kept out of the examples."""
    with database.get_conn() as conn:
        last_out = conn.execute("SELECT text FROM inbox_messages WHERE chat_id = ? AND direction = 'out' AND author = ? ORDER BY id DESC LIMIT 1",
                                (chat_id, AUTHOR)).fetchone()
        last_in = conn.execute("SELECT text FROM inbox_messages WHERE chat_id = ? AND direction = 'in' AND kind = 'text' ORDER BY id DESC LIMIT 1",
                               (chat_id,)).fetchone()
        if last_out and last_in and last_in["text"]:
            conn.execute("INSERT INTO ai_examples (q, a, source, chat_id, at, rating) VALUES (?, ?, 'rated', ?, ?, ?)",
                         (last_in["text"][:1000], last_out["text"][:2000], chat_id, util.now(), 1 if good else -1))


def test_call() -> dict:
    """Settings page «Проверить ключ»: one tiny request."""
    t0 = time.time()
    resp = _client().messages.create(model=setting("ai_model_triage") or "claude-haiku-5-5", max_tokens=20, output_config={"effort": "low"},
                                     messages=[{"role": "user", "content": "Ответь одним словом: готов?"}])
    text = next((b.text for b in resp.content if b.type == "text"), "")
    cost = _log(None, "test", resp.model, "ok", text, resp.usage, (time.time() - t0) * 1000)
    return {"ok": True, "model": resp.model, "text": text, "cost": cost}
