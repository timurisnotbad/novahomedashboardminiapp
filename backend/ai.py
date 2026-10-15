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
DEFAULTS = {"ai_mode": "off", "ai_model": "claude-opus-5-5", "ai_model_triage": "claude-haiku-5-5", "ai_budget": "30",
            "ai_channels": "wa,tg,wztg,wz", "ai_pause_hours": "2", "ai_max_turns": "8", "ai_debounce": "20",
            "ai_rules": "", "ai_knowledge": "", "usd_rate": "11850", "ai_style": "", "ai_transcribe": "1",
            "ai_playbook": "", "ai_followup_hours": "3", "ai_facts": ""}
# Rules saved by an admin before v84 equal one of these old defaults: treat them as «use the default».
_OLD_RULES = (  # noqa: E501
    """Ты — ассистент апартаментов Nova Home (Nest One, Ташкент). Отвечаешь гостям в мессенджере от имени команды.
Цель: быстро и точно закрыть простые вопросы, довести запрос на бронь до готовой заявки и передать человеку, когда нужно решение.
Правила:
- Отвечай коротко, тепло, без канцелярита, на языке гостя (русский, английский, узбекский).
- Свободные квартиры и цены — только через check_availability: это шахматка RealtyCalendar, там актуальные цены за ночь в долларах. Называй итог за весь период и цену за ночь; в сумах — по курсу из данных, если гость просит. Если на даты цены нет — скажи, что уточнишь, и вызови handover.
- Адрес, Wi‑Fi, условия, предоплата — только из базы знаний и данных CRM. Если данных нет — честно скажи, что уточнишь, и вызови handover.
- Никаких скидок, обещаний и подтверждений брони от своего имени. Подтверждает бронь только человек.
- Передавай человеку (handover): жалоба, спор о деньгах, просьба позвонить, поломка в квартире, нестандартная просьба, гость раздражён, вопрос вне твоих данных.
- Если гость хочет забронировать: уточни даты, число гостей, пожелания, собери контакт — и сохрани заявку (save_lead), после чего сообщи, что менеджер подтвердит бронь.
- Если гость назвал время приезда — сохрани его (set_arrival_time).
- Не выдумывай. Не повторяй одно и то же. Не пиши, что ты ИИ, если не спросят прямо; если спросят — скажи честно.""",)
DEFAULT_RULES = """Ты — ассистент команды Nova Home (апартаменты в Nest One, Ташкент). Отвечаешь гостям в мессенджере от имени команды.
Цель: быстро закрыть простой вопрос, довести запрос на жильё до заявки и передать человеку, когда нужно решение.

Как вести разговор о жилье:
1. Пойми запрос: даты, сколько гостей, пожелания. Чего не хватает — спроси одним коротким вопросом (сначала даты и число гостей).
2. Как только есть даты — вызови check_availability и предложи 2–3 подходящих варианта с ценой за ночь и итогом за период. Не спрашивай «на какие даты?», если даты уже назвали.
3. Гость выбрал — узнай имя и телефон, сохрани заявку (save_lead) и скажи, что менеджер подтвердит бронь и пришлёт реквизиты.
4. Цены и свободные квартиры — только из check_availability (шахматка RealtyCalendar, цены за ночь в долларах). В сумах — по курсу из данных, если гость просит. Нет цены на даты — скажи, что уточнишь, и вызови handover.

Язык и стиль:
- Отвечай на языке последнего сообщения гостя: русский, узбекский (латиница или кириллица — как пишет гость), английский. Гости часто пишут по-узбекски разговорно и с ошибками: «nechpul / necha pul» = сколько стоит, «bo'sh / bosh» = свободно, «kvartira / uy / xonadon» = квартира, «bormi» = есть ли, «kecha / tun» = ночь, «sutkalik / kunlik» = посуточно, «odam / kishi» = человек, «qachon» = когда, «narx / narxi» = цена, «joy» = место. Отвечай по-узбекски так же просто и естественно.
- Пиши как живой менеджер в мессенджере: коротко, тепло, по делу, 1–3 предложения, без списков и шаблонных оборотов. Никаких «Минуту, проверяю», «Я здесь, чтобы помочь» — сразу ответ по сути. Не здоровайся повторно в одном разговоре.
- Даты называй словами и без года: «18 июня», «18-19 iyun», «June 18». Никогда не пиши 2026-06-18 и не упоминай год.
- Опирайся на памятку о стиле команды и примеры ответов: те же обороты, та же манера.
- Гость часто пишет одну мысль несколькими сообщениями подряд («хочу забронировать» → «2 человека» → «21-24 декабря»): читай их вместе как один запрос. «21-24 декабря» — это даты заезда и выезда, «2 человека» — число гостей; не переспрашивай то, что уже сказано.
- Разговорные сокращения и обращения понимай молча: «брат», «братан», «бро», «ака», «aka», «дорогой», «уважаемый» — обращение к тебе, а не имя; «скок / скока» = сколько; «чел» = человек; «сут / сутки» = ночь посуточно; «кв» = квартира; «пред / предоплата»; «нал» = наличные; «перевод» = на карту; «сег» = сегодня; «завтр» = завтра; «ок / окей / xo'p / xop» = согласие; «мб» = может быть; «хз» = не знает; «4 вариант» = четвёртый из перечисленных тобой.
- Фото квартиры отправляй инструментом send_photos: когда предложил 1–2 подходящих варианта (по одному вызову на квартиру) или когда гость просит фото. В тексте не пиши слово «Фото» как ссылку и не обещай того, чего нет; если у квартиры нет фото, инструмент скажет об этом — тогда напиши, что менеджер пришлёт. Не перечисляй все свободные квартиры списком — 2–3 подходящих.

Честность:
- Ты ассистент команды, а не конкретный человек. Никогда не называйся именем сотрудника или владельца. Если гость спросит, бот ли ты, — скажи честно: ты ассистент Nova Home, при необходимости подключится менеджер.
- Не выдумывай: адрес, Wi‑Fi, условия, предоплата — только из базы знаний и данных CRM. Нет данных — скажи, что уточнишь, и вызови handover.
- Никаких скидок, обещаний и подтверждений брони от своего имени. Подтверждает бронь только человек.
- Название чата или контакта — это имя в телефонной книге, а не запрос гостя: даты и квартиру бери только из сообщений.

Передавай человеку (handover): жалоба, спор о деньгах, просьба позвонить, поломка в квартире, нестандартная просьба, гость раздражён, вопрос вне твоих данных. Если гость назвал время приезда — сохрани его (set_arrival_time)."""

_WD = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_state: dict = {"pending": {}}  # chat_id → timestamp of the last scheduled run (debounce)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def init_db() -> None:
    with database.get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(inbox_chats)").fetchall()]
        for col, decl in (("ai_mode", "TEXT"), ("ai_paused_until", "TEXT"), ("ai_draft", "TEXT"), ("ai_draft_at", "TEXT"),
                          ("ai_turns", "INTEGER DEFAULT 0"), ("ai_note", "TEXT"), ("last_author", "TEXT"), ("ai_followup_at", "TEXT"),
                          ("ai_last_in_id", "INTEGER"), ("ai_draft_photos", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE inbox_chats ADD COLUMN {col} {decl}")  # noqa: S608
        mcols = [r[1] for r in conn.execute("PRAGMA table_info(inbox_messages)").fetchall()]
        if mcols and "transcript" not in mcols:
            conn.execute("ALTER TABLE inbox_messages ADD COLUMN transcript TEXT")  # voice note → text (NULL = not tried yet)
        ccols = [r[1] for r in conn.execute("PRAGMA table_info(crm_clients)").fetchall()]
        if ccols and "ai_memory" not in ccols:
            conn.execute("ALTER TABLE crm_clients ADD COLUMN ai_memory TEXT")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL, source TEXT, chat_id INTEGER, at TEXT, active INTEGER DEFAULT 1
        );
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


def openai_key() -> str:
    return (config.OPENAI_API_KEY or "").strip()


def set_openai_key(key: str) -> None:
    from . import secrets_store
    key = (key or "").strip()
    secrets_store.save_extra(config.BASE_DIR / "data", openai_api_key=key)
    config.OPENAI_API_KEY = key


TRANSCRIBE_MODELS = ("gpt-4o-mini-transcribe", "whisper-1")
TRANSCRIBE_PER_MIN = 0.003  # $ per minute of audio (gpt-4o-mini-transcribe)


def transcribe(media: str, mime: str = "") -> str:
    """Speech → text through the OpenAI transcription API (Uzbek, Russian and
    English voice notes). Returns "" when no key, no file or the API failed."""
    import requests as rq
    key = openai_key()
    if not key or not media:
        return ""
    p = Path(config.INBOX_MEDIA_DIR) / media
    if not p.is_file() or p.stat().st_size > 24_000_000:
        return ""
    ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "m4a", "audio/webm": "webm", "audio/wav": "wav", "audio/x-wav": "wav"}.get((mime or "").split(";")[0], p.suffix.lstrip(".") or "ogg")
    data = p.read_bytes()
    last = ""
    for model in TRANSCRIBE_MODELS:
        try:
            r = rq.post("https://api.openai.com/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"}, timeout=90,
                        files={"file": (f"voice.{ext}", data, mime or "application/octet-stream")},
                        data={"model": model, "prompt": "Переписка гостя с апартаментами Nova Home, Ташкент. Речь может быть на узбекском (O'zbek), русском или английском."})
            if r.status_code == 200:
                text = (r.json().get("text") or "").strip()
                if text:
                    _log(0, "transcribe", model, "ok", text[:200], None, 0, None, cost=round(max(len(data), 1) / 32000 / 60 * TRANSCRIBE_PER_MIN, 5))
                return text
            last = f"{r.status_code}: {r.text[:160]}"
            if r.status_code in (401, 402, 429):
                break
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:160]
    _log(0, "transcribe", TRANSCRIBE_MODELS[0], "error", "", None, 0, config.redact(last))
    return ""


def transcribe_message(message_id: int) -> str:
    """Transcribe one stored voice message once; the text is kept on the row so
    staff see it in the chat and the assistant reads it as text."""
    from . import inbox
    with database.get_conn() as conn:
        r = conn.execute("SELECT id, chat_id, media, mime, transcript FROM inbox_messages WHERE id = ? AND kind = 'audio'", (message_id,)).fetchone()
    if not r or r["transcript"] is not None:
        return (r["transcript"] if r else "") or ""
    text = transcribe(r["media"] or "", r["mime"] or "")
    with database.get_conn() as conn:
        conn.execute("UPDATE inbox_messages SET transcript = ?, rev = ? WHERE id = ?", (text, inbox._bump(conn), r["id"]))  # noqa: SLF001
    return text


def transcribe_pending(chat_id: int) -> int:
    """Voice notes of this chat that still have no text (the last 10)."""
    if not openai_key() or setting("ai_transcribe") != "1":
        return 0
    with database.get_conn() as conn:
        ids = [x["id"] for x in conn.execute("SELECT id FROM inbox_messages WHERE chat_id = ? AND kind = 'audio' AND transcript IS NULL AND media IS NOT NULL "
                                             "ORDER BY id DESC LIMIT 10", (chat_id,)).fetchall()]
    n = 0
    for mid in ids:
        if transcribe_message(mid):
            n += 1
    return n


def _client():
    if "client" not in _state:
        import anthropic
        _state["client"] = anthropic.Anthropic(api_key=api_key(), max_retries=2, timeout=90.0)
    return _state["client"]


def setting(key: str) -> str:
    from . import crm
    return crm.get_setting(key, DEFAULTS.get(key, ""))


_OLD_RULES = _OLD_RULES + (_OLD_RULES[0].replace(
    "- Свободные квартиры и цены — только через check_availability: это шахматка RealtyCalendar, там актуальные цены за ночь в долларах. Называй итог за весь период и цену за ночь; в сумах — по курсу из данных, если гость просит. Если на даты цены нет — скажи, что уточнишь, и вызови handover.\n"
    "- Адрес, Wi‑Fi, условия, предоплата — только из базы знаний и данных CRM. Если данных нет — честно скажи, что уточнишь, и вызови handover.",
    "- Цены, свободные даты, адрес, Wi‑Fi, условия — только из базы знаний и из данных CRM. Если данных нет — честно скажи, что уточнишь, и вызови handover."),)


_OLD_RULES = _OLD_RULES + ("Ты — ассистент команды Nova Home (апартаменты в Nest One, Ташкент). Отвечаешь гостям в мессенджере от имени команды.\nЦель: быстро закрыть простой вопрос, довести запрос на жильё до заявки и передать человеку, когда нужно решение.\n\nКак вести разговор о жилье:\n1. Пойми запрос: даты, сколько гостей, пожелания. Чего не хватает — спроси одним коротким вопросом (сначала даты и число гостей).\n2. Как только есть даты — вызови check_availability и предложи 2–3 подходящих варианта с ценой за ночь и итогом за период. Не спрашивай «на какие даты?», если даты уже назвали.\n3. Гость выбрал — узнай имя и телефон, сохрани заявку (save_lead) и скажи, что менеджер подтвердит бронь и пришлёт реквизиты.\n4. Цены и свободные квартиры — только из check_availability (шахматка RealtyCalendar, цены за ночь в долларах). В сумах — по курсу из данных, если гость просит. Нет цены на даты — скажи, что уточнишь, и вызови handover.\n\nЯзык и стиль:\n- Отвечай на языке последнего сообщения гостя: русский, узбекский (латиница или кириллица — как пишет гость), английский. Гости часто пишут по-узбекски разговорно и с ошибками: «nechpul / necha pul» = сколько стоит, «bo'sh / bosh» = свободно, «kvartira / uy / xonadon» = квартира, «bormi» = есть ли, «kecha / tun» = ночь, «sutkalik / kunlik» = посуточно, «odam / kishi» = человек, «qachon» = когда, «narx / narxi» = цена, «joy» = место. Отвечай по-узбекски так же просто и естественно.\n- Пиши как живой менеджер в мессенджере: коротко, тепло, по делу, 1–3 предложения, без списков и шаблонных оборотов. Никаких «Минуту, проверяю», «Я здесь, чтобы помочь» — сразу ответ по сути. Не здоровайся повторно в одном разговоре.\n- Даты называй словами и без года: «18 июня», «18-19 iyun», «June 18». Никогда не пиши 2026-06-18 и не упоминай год.\n- Опирайся на памятку о стиле команды и примеры ответов: те же обороты, та же манера.\n\nЧестность:\n- Ты ассистент команды, а не конкретный человек. Никогда не называйся именем сотрудника или владельца. Если гость спросит, бот ли ты, — скажи честно: ты ассистент Nova Home, при необходимости подключится менеджер.\n- Не выдумывай: адрес, Wi‑Fi, условия, предоплата — только из базы знаний и данных CRM. Нет данных — скажи, что уточнишь, и вызови handover.\n- Никаких скидок, обещаний и подтверждений брони от своего имени. Подтверждает бронь только человек.\n- Название чата или контакта — это имя в телефонной книге, а не запрос гостя: даты и квартиру бери только из сообщений.\n\nПередавай человеку (handover): жалоба, спор о деньгах, просьба позвонить, поломка в квартире, нестандартная просьба, гость раздражён, вопрос вне твоих данных. Если гость назвал время приезда — сохрани его (set_arrival_time).",)


_OLD_RULES = _OLD_RULES + ("Ты — ассистент команды Nova Home (апартаменты в Nest One, Ташкент). Отвечаешь гостям в мессенджере от имени команды.\nЦель: быстро закрыть простой вопрос, довести запрос на жильё до заявки и передать человеку, когда нужно решение.\n\nКак вести разговор о жилье:\n1. Пойми запрос: даты, сколько гостей, пожелания. Чего не хватает — спроси одним коротким вопросом (сначала даты и число гостей).\n2. Как только есть даты — вызови check_availability и предложи 2–3 подходящих варианта с ценой за ночь и итогом за период. Не спрашивай «на какие даты?», если даты уже назвали.\n3. Гость выбрал — узнай имя и телефон, сохрани заявку (save_lead) и скажи, что менеджер подтвердит бронь и пришлёт реквизиты.\n4. Цены и свободные квартиры — только из check_availability (шахматка RealtyCalendar, цены за ночь в долларах). В сумах — по курсу из данных, если гость просит. Нет цены на даты — скажи, что уточнишь, и вызови handover.\n\nЯзык и стиль:\n- Отвечай на языке последнего сообщения гостя: русский, узбекский (латиница или кириллица — как пишет гость), английский. Гости часто пишут по-узбекски разговорно и с ошибками: «nechpul / necha pul» = сколько стоит, «bo'sh / bosh» = свободно, «kvartira / uy / xonadon» = квартира, «bormi» = есть ли, «kecha / tun» = ночь, «sutkalik / kunlik» = посуточно, «odam / kishi» = человек, «qachon» = когда, «narx / narxi» = цена, «joy» = место. Отвечай по-узбекски так же просто и естественно.\n- Пиши как живой менеджер в мессенджере: коротко, тепло, по делу, 1–3 предложения, без списков и шаблонных оборотов. Никаких «Минуту, проверяю», «Я здесь, чтобы помочь» — сразу ответ по сути. Не здоровайся повторно в одном разговоре.\n- Даты называй словами и без года: «18 июня», «18-19 iyun», «June 18». Никогда не пиши 2026-06-18 и не упоминай год.\n- Опирайся на памятку о стиле команды и примеры ответов: те же обороты, та же манера.\n- Гость часто пишет одну мысль несколькими сообщениями подряд («хочу забронировать» → «2 человека» → «21-24 декабря»): читай их вместе как один запрос. «21-24 декабря» — это даты заезда и выезда, «2 человека» — число гостей; не переспрашивай то, что уже сказано.\n- Разговорные сокращения и обращения понимай молча: «брат», «братан», «бро», «ака», «aka», «дорогой», «уважаемый» — обращение к тебе, а не имя; «скок / скока» = сколько; «чел» = человек; «сут / сутки» = ночь посуточно; «кв» = квартира; «пред / предоплата»; «нал» = наличные; «перевод» = на карту; «сег» = сегодня; «завтр» = завтра; «ок / окей / xo'p / xop» = согласие; «мб» = может быть; «хз» = не знает; «4 вариант» = четвёртый из перечисленных тобой.\n- Не пиши слово «Фото» и не обещай ссылок, которых у тебя нет. Если просят фото или видео — скажи, что менеджер пришлёт, и продолжай разговор. Не перечисляй все свободные квартиры списком — 2–3 подходящих.\n\nЧестность:\n- Ты ассистент команды, а не конкретный человек. Никогда не называйся именем сотрудника или владельца. Если гость спросит, бот ли ты, — скажи честно: ты ассистент Nova Home, при необходимости подключится менеджер.\n- Не выдумывай: адрес, Wi‑Fi, условия, предоплата — только из базы знаний и данных CRM. Нет данных — скажи, что уточнишь, и вызови handover.\n- Никаких скидок, обещаний и подтверждений брони от своего имени. Подтверждает бронь только человек.\n- Название чата или контакта — это имя в телефонной книге, а не запрос гостя: даты и квартиру бери только из сообщений.\n\nПередавай человеку (handover): жалоба, спор о деньгах, просьба позвонить, поломка в квартире, нестандартная просьба, гость раздражён, вопрос вне твоих данных. Если гость назвал время приезда — сохрани его (set_arrival_time).",)


def rules_text() -> str:
    r = setting("ai_rules")
    if not r or _norm(r) in {_norm(x) for x in _OLD_RULES} or _norm(r) == _norm(DEFAULT_RULES):
        return DEFAULT_RULES
    return r


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())


def playbook_text() -> str:
    from . import ai_playbook
    return setting("ai_playbook").strip() or ai_playbook.DEFAULT_PLAYBOOK


def settings() -> dict:
    out = {k: setting(k) for k in DEFAULTS}
    out["ai_rules"] = rules_text()
    out["ai_playbook"] = playbook_text()
    out["openai_key_set"] = bool(openai_key())
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
            if k == "usd_rate":
                try:
                    v = f"{float(v.replace(' ', '').replace(',', '.')):g}"
                except ValueError:
                    continue
            crm.set_setting(k, v[:20000])
    if data.get("api_key"):
        set_api_key(str(data["api_key"]))
    if data.get("openai_key"):
        set_openai_key(str(data["openai_key"]))
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


def _log(chat_id, kind, model, decision, text, usage=None, ms=0, error=None, cost=None) -> float:
    if cost is None:
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


def _rates_status() -> dict:
    from . import rc_rates
    return rc_rates.status()


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
            "recent": recent, "examples": examples, "chats_by_mode": chats, "enabled": enabled(), "mode": setting("ai_mode"),
            "rates": _rates_status(), "lessons": lessons_list(100, all_rows=True)}


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
        last_in = conn.execute("SELECT id, text, transcript FROM inbox_messages WHERE chat_id = ? AND direction = 'in' ORDER BY id DESC LIMIT 1",
                               (chat_id,)).fetchone()
        guest = ((last_in["text"] or last_in["transcript"]) if last_in else "") or ""
        if draft and sent_text and sent_text.strip() != draft.strip() and guest:
            conn.execute("INSERT INTO ai_examples (q, a, source, chat_id, at) VALUES (?, ?, 'edited', ?, ?)",
                         (guest[:1000], sent_text.strip()[:2000], chat_id, util.now()))
        # the assistant already answered the guest and a person still wrote: a correction worth a lesson
        last_ai = conn.execute("SELECT id, text, at FROM inbox_messages WHERE chat_id = ? AND direction = 'out' AND author = ? ORDER BY id DESC LIMIT 1",
                               (chat_id, AUTHOR)).fetchone()
    _state["pending"].pop(chat_id, None)
    if not sent_text or not enabled():
        return
    ai_text = draft or ""
    if not ai_text and last_ai and last_in and last_ai["id"] > last_in["id"] and (last_ai["at"] or "") >= (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds"):
        ai_text = last_ai["text"] or ""
    if ai_text and ai_text.strip() != sent_text.strip():
        bg.run(learn_lesson, chat_id, guest, ai_text, sent_text, "correction", key=f"ai-lesson-{chat_id}")


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


def learn_style(max_msgs: int = 240) -> dict:
    """Read the team's own past replies and write a short style memo (tone,
    greetings, typical phrases per language, how prices and options are
    offered). One model call; the memo goes into the cached system block."""
    with database.get_conn() as conn:
        rows = conn.execute("SELECT author, text FROM inbox_messages WHERE direction = 'out' AND kind = 'text' AND text != '' AND COALESCE(author, '') NOT IN (?, 'Автоответ') "
                            "AND LENGTH(text) BETWEEN 12 AND 600 ORDER BY id DESC LIMIT ?", (AUTHOR, max_msgs * 2)).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        if re.search(r"\{[а-яa-z ]+\}", r["text"]):
            continue
        by.setdefault(r["author"] or "Телефон", []).append(r["text"])
    texts = []
    per = max(40, max_msgs // max(1, min(6, len(by))))
    for au, lst in sorted(by.items(), key=lambda kv: -len(kv[1]))[:6]:
        texts += [f"[{au}] {t}" for t in lst[:per]]
    texts = texts[:max_msgs]
    if len(texts) < 10:
        return {"ok": False, "error": f"мало ответов команды в истории ({len(texts)}), нужно хотя бы 10", "messages": len(texts)}
    model = setting("ai_model") or "claude-opus-5-5"
    t0 = time.time()
    resp = _client().messages.create(
        model=model, max_tokens=1800, output_config={"effort": "medium"},
        system="Ты анализируешь, как менеджеры апартаментов Nova Home (Ташкент) пишут гостям в мессенджере, чтобы ассистент писал так же. "
               "Сообщения помечены автором в квадратных скобках. Составь памятку до 2500 символов: 1) общий тон и длина сообщений; как здороваются и обращаются (ты/вы, имя); эмодзи; "
               "типичные фразы и обороты отдельно для русского, узбекского и английского (3–5 реальных коротких фраз на каждом языке, если есть); "
               "как называют цены и предлагают варианты; как просят предоплату и контакты; чего менеджеры никогда не пишут. "
               "2) «Как отвечает каждый»: по 2–4 строки на каждого автора — манера, сильные приёмы, характерные фразы. "
               "Только памятка, без вступления. Не включай персональные данные гостей (имена, телефоны, коды).",
        messages=[{"role": "user", "content": "Ответы команды (новые сверху):\n\n" + "\n---\n".join(texts)}])
    memo = "\n".join(b.text for b in resp.content if b.type == "text").strip()[:4000]
    cost = _log(0, "style", model, "ok" if memo else "empty", memo[:400], resp.usage, (time.time() - t0) * 1000)
    if memo:
        from . import crm
        crm.set_setting("ai_style", memo)
    return {"ok": bool(memo), "messages": len(texts), "style": memo, "cost": cost}


# ---------------------------------------------------------------------------
# Lessons: the assistant's own accumulated experience
# ---------------------------------------------------------------------------
LESSON_SCHEMA = {"type": "object", "properties": {"lesson": {"type": "string"}, "skip": {"type": "boolean"}, "why": {"type": "string"}},
                 "required": ["lesson", "skip", "why"], "additionalProperties": False}


def lessons_list(limit: int = 100, all_rows: bool = False) -> list[dict]:
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM ai_lessons" + ("" if all_rows else " WHERE active = 1") + " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def add_lesson(text: str, source: str = "manual", chat_id: int | None = None) -> int:
    text = re.sub(r"\s+", " ", (text or "")).strip()[:400]
    if not text:
        return 0
    with database.get_conn() as conn:
        dup = conn.execute("SELECT id FROM ai_lessons WHERE lower(text) = lower(?)", (text,)).fetchone()
        if dup:
            return dup["id"]
        return conn.execute("INSERT INTO ai_lessons (text, source, chat_id, at, active) VALUES (?, ?, ?, ?, 1)", (text, source, chat_id, util.now())).lastrowid


def set_lesson(lesson_id: int, active: bool | None = None, text: str | None = None, delete: bool = False) -> None:
    with database.get_conn() as conn:
        if delete:
            conn.execute("DELETE FROM ai_lessons WHERE id = ?", (lesson_id,))
            return
        if active is not None:
            conn.execute("UPDATE ai_lessons SET active = ? WHERE id = ?", (1 if active else 0, lesson_id))
        if text is not None and text.strip():
            conn.execute("UPDATE ai_lessons SET text = ? WHERE id = ?", (text.strip()[:400], lesson_id))


def learn_lesson(chat_id: int, guest: str, ai_text: str, human_text: str, source: str) -> str:
    """One cheap call: what did the person do differently from the assistant (or
    what was wrong in a disliked reply) → one general, reusable lesson, or skip
    when it is already known or purely situational."""
    model = setting("ai_model_triage") or "claude-haiku-5-5"
    known = "\n".join(f"- {x['text']}" for x in lessons_list(40)) or "(пока нет)"
    t0 = time.time()
    try:
        resp = _client().messages.create(
            model=model, max_tokens=300, output_config={"effort": "low", "format": {"type": "json_schema", "schema": LESSON_SCHEMA}},
            system="Ты ведёшь базу опыта ассистента апартаментов Nova Home. Сравни ответ ассистента с тем, что сделал менеджер (или с замечанием), "
                   "и сформулируй ОДИН общий, переиспользуемый урок для будущих разговоров: как понимать такие сообщения гостей, что отвечать, чего не делать. "
                   "Урок — одно предложение до 200 символов, без имён и дат. skip=true, если разница ситуативная (конкретные даты, цены этого раза), "
                   "урок уже есть в списке известных, или менеджер просто дописал приветствие. Отвечай только JSON.",
            messages=[{"role": "user", "content": f"Известные уроки:\n{known}\n\nСообщение гостя: {guest[:600]}\n\nОтвет/черновик ассистента: {ai_text[:800]}\n\n"
                                                  + (f"Что отправил менеджер вместо этого: {human_text[:800]}" if human_text else "Менеджер отметил этот ответ как плохой (👎).")}])
        data = json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))
        lesson = "" if data.get("skip") else (data.get("lesson") or "").strip()
        _log(chat_id, "lesson", model, "add" if lesson else "skip", lesson or data.get("why") or "", resp.usage, (time.time() - t0) * 1000)
        if lesson:
            add_lesson(lesson, source, chat_id)
        return lesson
    except Exception as exc:  # noqa: BLE001
        _log(chat_id, "lesson", model, "error", "", None, 0, str(exc))
        return ""


def learn_facts(max_msgs: int = 400) -> dict:
    """What the team actually told guests about conditions, prices, payment,
    registration, parking… → a draft fact list (setting ai_facts) for the admin
    to check and move into the knowledge base."""
    with database.get_conn() as conn:
        rows = conn.execute("SELECT m.chat_id, m.direction, m.text, m.at FROM inbox_messages m WHERE m.kind = 'text' AND m.text != '' "
                            "AND (m.direction = 'in' OR COALESCE(m.author, '') NOT IN (?, 'Автоответ')) AND LENGTH(m.text) BETWEEN 15 AND 900 "
                            "ORDER BY m.id DESC LIMIT ?", (AUTHOR, max_msgs * 2)).fetchall()
    pairs, prev = [], None
    for r in reversed(rows):
        if prev and prev["chat_id"] == r["chat_id"] and prev["direction"] == "in" and r["direction"] == "out":
            pairs.append(f"Гость: {prev['text'][:250]}\nКоманда ({(r['at'] or '')[:7]}): {r['text'][:500]}")
        prev = r
    pairs = pairs[-max_msgs:]
    if len(pairs) < 10:
        return {"ok": False, "error": f"мало переписок для разбора ({len(pairs)} пар)", "pairs": len(pairs)}
    model = setting("ai_model") or "claude-opus-5-5"
    t0 = time.time()
    resp = _client().messages.create(
        model=model, max_tokens=1500, output_config={"effort": "medium"},
        system="Ты разбираешь прошлые переписки команды апартаментов Nova Home (Ташкент) с гостями. Извлеки ФАКТЫ, которые команда реально сообщала гостям: "
               "условия заселения и выезда, предоплата и возврат, способы оплаты, регистрация, парковка, ЗАГС, дети/животные, скидки, что входит, адрес и как добраться, "
               "ответы на частые вопросы. Каждый факт — одна строка «- …» с пометкой месяца, если факт мог измениться (цены). Не придумывай, не обобщай спорное; "
               "если команда отвечала по-разному — укажи оба варианта с пометкой «расходится». До 2500 символов, без вступления.",
        messages=[{"role": "user", "content": "Пары «гость → команда» (старые сверху):\n\n" + "\n---\n".join(pairs)}])
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()[:4000]
    cost = _log(0, "facts", model, "ok" if text else "empty", text[:400], resp.usage, (time.time() - t0) * 1000)
    if text:
        from . import crm
        crm.set_setting("ai_facts", text)
    return {"ok": bool(text), "pairs": len(pairs), "facts": text, "cost": cost}


def find_examples(text: str, limit: int = 6) -> list[dict]:
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
    rows = conn.execute("SELECT id, direction, author, kind, text, media, mime, at, transcript FROM inbox_messages WHERE chat_id = ? "
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
            tr = (m.get("transcript") or "").strip()
            body = f"[голосовое]: {tr}" if tr else "[голосовое сообщение — расшифровки нет, попроси написать текстом, если важно]"
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
        au = m.get("author") or "менеджер"
        who = "" if role == "user" else ("[ты] " if au == AUTHOR else "[менеджер с телефона] " if au == "Телефон" else f"[менеджер {au}] ")
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
    last_in = next((m.get("text") or m.get("transcript") for m in reversed(history) if m["direction"] == "in" and (m.get("text") or m.get("transcript"))), "")
    return {"chat": chat, "client": client, "history": history, "stays": stays, "last_in": last_in,
            "examples": find_examples(last_in) if last_in else []}


def _system(ctx: dict) -> list[dict]:
    """Stable part first (cached across chats), then the per-chat facts."""
    from . import crm, docs
    rules = rules_text()
    knowledge = setting("ai_knowledge") or "(база знаний пока не заполнена)"
    style = setting("ai_style")
    facts = setting("ai_facts")
    lessons = "\n".join(f"- {x['text']}" for x in lessons_list(40))
    co = docs.company()
    apt_rows = crm.apartment_info()
    apt_lines = [f"- {r['apartment'] if r['kind'] == 'apt' else ('блок ' + r['apartment'] if r['kind'] == 'block' else 'все квартиры')}: "
                 + "; ".join(f"{k}: {r[f]}" for k, f in (("описание", "descr"), ("домофон", "door_code"), ("ключи", "keys_in"), ("ключи при выезде", "keys_out"), ("Wi‑Fi", "wifi"), ("заметка", "note")) if r.get(f))
                 for r in apt_rows if any(r.get(f) for f in crm.APT_FIELDS)]
    ci, co_t = crm.default_times()
    stable = (f"{rules}\n\n## Методика продаж\n{playbook_text()}\n\n## Компания\n{co.get('name') or 'Nova Home'} · адрес: {co.get('address') or '—'} · телефон: {co.get('phone') or '—'} · "
              f"заезд с {ci}, выезд до {co_t}.\n\n## База знаний (правда только то, что здесь и в данных CRM)\n{knowledge}\n\n"
              f"## Квартиры\n" + ("\n".join(apt_lines) or "(нет данных)")
              + (f"\n\n## Стиль команды (памятка из прошлых переписок)\n{style}" if style else "")
              + (f"\n\n## Из прошлых переписок команды (что команда реально отвечала; если противоречит базе знаний — база знаний главнее)\n{facts}" if facts else "")
              + (f"\n\n## Опыт: уроки из прошлых переписок (соблюдай)\n{lessons}" if lessons else ""))
    chat, client, stays = ctx["chat"], ctx["client"], ctx["stays"]
    b = chat.get("booking")
    td = date.today()
    facts = [f"Сегодня {_WD[td.weekday()]}, {td.day} {util.RU_MONTHS_GEN[td.month - 1]} {td.year} г., {datetime.now().strftime('%H:%M')} (в ответах гостю год не пиши). "
             f"Цены в календаре в долларах; курс 1 $ = {setting('usd_rate') or '—'} сум.",
             f"Канал: {chat.get('channel_name')}. Контакт в телефонной книге: «{chat.get('title')}» (это не запрос)" + (f", телефон +{chat['phone']}" if chat.get("phone") else "") + "."]
    if ctx.get("lang"):
        facts.append(f"Язык последнего сообщения гостя: {ctx['lang']} — отвечай на нём.")
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
    {"name": "check_availability", "description": "Свободные квартиры на даты и их стоимость из шахматки RealtyCalendar (цены за ночь в долларах, итог за период, минимальный срок). Вызывай перед любым ответом о свободных датах или ценах.",
     "strict": True, "input_schema": {"type": "object", "properties": {"checkin": {"type": "string", "description": "YYYY-MM-DD"}, "checkout": {"type": "string", "description": "YYYY-MM-DD"},
                                                                       "apartment": {"type": "string", "description": "код квартиры (A-066) или блока (A, B); пусто — все"}},
                                      "required": ["checkin", "checkout", "apartment"], "additionalProperties": False}},
    {"name": "send_photos", "description": "Отправить гостю все фото квартиры из CRM (альбомом) с короткой подписью. Вызывай после того, как предложил квартиру, или когда просят фото. Одна квартира за вызов.",
     "strict": True, "input_schema": {"type": "object", "properties": {"apartment": {"type": "string", "description": "код квартиры, например B-005"},
                                                                       "caption": {"type": "string", "description": "подпись к первому фото на языке гостя, до 120 символов"}},
                                      "required": ["apartment", "caption"], "additionalProperties": False}},
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


def availability(checkin: str, checkout: str, apartment: str = "") -> dict:
    """Free apartments for [checkin, checkout) with the calendar's prices:
    per-night range, total for the stay, min nights; in USD and in sums."""
    from . import rc_rates
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
    want = (apartment or "").strip().upper()
    names = sorted(set(config.APARTMENTS.values()))
    if want:
        names = [n for n in names if n == want or (len(want) == 1 and n.startswith(want))]
    nights = (d1 - d0).days
    try:
        rate = float(setting("usd_rate") or 0)
    except ValueError:
        rate = 0.0
    free = []
    from . import crm
    about = {r["apartment"]: r.get("descr") or "" for r in crm.apartment_info()}
    with database.get_conn() as conn:
        nphotos = {r["apartment"]: r["n"] for r in conn.execute("SELECT apartment, COUNT(*) AS n FROM crm_apartment_photos GROUP BY apartment").fetchall()}
    for n in names:
        if n in busy:
            continue
        p = rc_rates.prices(n, d0.isoformat(), d1.isoformat())
        row = {"apartment": n, "about": about.get(n) or about.get(n[:1]) or about.get("*") or "", "photos": nphotos.get(n) or nphotos.get(n[:1]) or 0,
               "total_usd": p["total"], "per_night_usd": None if p["per_night_min"] is None else
               (p["per_night_min"] if p["per_night_min"] == p["per_night_max"] else f"{p['per_night_min']:g}–{p['per_night_max']:g}")}
        if p["total"] is not None and rate:
            row["total_uzs"] = round(p["total"] * rate)
        if p["min_nights"] and nights < p["min_nights"]:
            row["min_nights"] = p["min_nights"]
            row["note"] = f"минимум {p['min_nights']} ноч."
        if p["missing"]:
            row["note"] = "цена на часть дат не задана — уточнить у менеджера" if p["priced"] else "цены на эти даты нет — уточнить у менеджера"
        free.append(row)
    free.sort(key=lambda r: (r["total_usd"] is None, r["total_usd"] or 0))
    out = {"checkin": d0.isoformat(), "checkout": d1.isoformat(), "nights": nights, "free": free, "free_count": len(free),
           "total": len(names), "currency": "USD", "usd_rate": rate or None, "hint": "даты гостю называй словами без года"}
    if free and all(r["total_usd"] is None for r in free):
        out["note"] = "тарифы из календаря не загружены — цены назвать нельзя, передай менеджеру"
    return out


def _run_tool(name: str, inp: dict, ctx: dict, state: dict) -> str:
    from . import crm, crm_amo, inbox, notify
    chat = ctx["chat"]
    try:
        if name == "check_availability":
            return json.dumps(availability(inp.get("checkin", ""), inp.get("checkout", ""), inp.get("apartment", "")), ensure_ascii=False)
        if name == "send_photos":
            apt = (inp.get("apartment") or "").strip().upper()
            photos = crm.apartment_photos(apt)[:MAX_PHOTOS]
            if not photos:
                return json.dumps({"ok": False, "error": f"у {apt} нет фото в CRM — скажи гостю, что менеджер пришлёт фото"}, ensure_ascii=False)
            if state.get("mode") == "auto":
                try:
                    sent = len(inbox.send_album(chat["id"], AUTHOR, (inp.get("caption") or "")[:200], [(ph["media"], ph["mime"]) for ph in photos]))
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({"ok": False, "error": str(exc)[:120]}, ensure_ascii=False)
                return json.dumps({"ok": True, "sent": sent, "note": "фото уже ушли гостю, в тексте не повторяй их и не пиши «Фото»"}, ensure_ascii=False)
            state.setdefault("photos", [])
            if apt not in state["photos"]:
                state["photos"].append(apt)
            return json.dumps({"ok": True, "queued": len(photos), "note": "фото уйдут вместе с твоим ответом, когда менеджер его отправит; в тексте их не описывай"}, ensure_ascii=False)
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
    lines = [f"{'Гость' if m['direction'] == 'in' else 'Менеджер'}: {(m.get('text') or m.get('transcript') or '[' + (m.get('kind') or '') + ']')[:300]}" for m in hist]
    t0 = time.time()
    resp = _client().messages.create(
        model=model, max_tokens=300, output_config={"effort": "low", "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
        system="Ты сортируешь входящие сообщения гостей апартаментов. Реши: нужен ли ответ ассистента, и нужен ли человек. "
               "Человек нужен при жалобе, споре о деньгах/возврате, поломке, просьбе позвонить, раздражении, нестандартной просьбе. "
               "Простые вопросы (адрес, Wi‑Fi, время заезда, цены, свободные даты, как добраться) и заявки на бронь — ассистент. "
               "Гости пишут по-русски, по-узбекски (латиницей, часто разговорно: nechpul, bosh, kvartira bormi) и по-английски; language — код языка последнего сообщения (ru/uz/en/…). Отвечай только JSON.",
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
def _reply(ctx: dict, state: dict, extra: str = "") -> str:
    model = setting("ai_model") or "claude-opus-5-5"
    messages = _messages_for_model(ctx["history"])
    if extra:
        messages[-1]["content"].append({"type": "text", "text": extra})
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
def _prepare(chat_id: int) -> None:
    try:
        transcribe_pending(chat_id)
    except Exception:  # noqa: BLE001
        logger.exception("transcription failed")


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


def handle(chat_id: int, force: bool = False, want_draft: bool = False, nudge: bool = False, _retry: bool = False) -> dict:
    """One pass over the chat: triage → reply → send or draft. force=True is the
    «Ответить с ИИ» button (always a draft, ignores mode/pause/budget).
    nudge=True: the guest went silent after our offer — one gentle follow-up."""
    from . import inbox
    if force:
        _prepare(chat_id)  # the «Ответить с ИИ» button: voice notes first
    ctx = _context(chat_id)
    if not ctx:
        return {"status": "no chat"}
    chat = ctx["chat"]
    mode = "draft" if force else chat_mode(chat)
    if mode == "off":
        return {"status": "off"}
    extra = ""
    if nudge:
        if mode != "auto":
            return {"status": "off"}
        hrs = setting("ai_followup_hours") or "3"
        extra = (f"(Служебное: гость не ответил уже {hrs} ч. после твоего последнего сообщения. Напиши одно короткое тёплое напоминание без давления — "
                 f"уточни, актуально ли, или предложи помощь с выбором; 1–2 фразы, вопрос в конце, на языке гостя, не повторяй прошлое сообщение. "
                 f"Если напоминание неуместно — гость попрощался, отказался, вопрос закрыт, бронь уже оформлена — ответь ровно {NO_FOLLOWUP}.)")
    last_in_id = max((m["id"] for m in ctx["history"] if m["direction"] == "in"), default=0)
    if not force and not nudge:
        if not last_in_id or last_in_id <= int(chat.get("ai_last_in_id") or 0):
            return {"status": "nothing new"}  # every guest message is already answered
        last = ctx["history"][-1] if ctx["history"] else None
        if last and last["direction"] == "out" and last.get("author") != AUTHOR and last["id"] > last_in_id:
            return {"status": "nothing new"}  # a person answered after the guest
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
        ctx["lang"] = tri.get("language") or ""
        try:
            if int(chat.get("ai_turns") or 0) >= int(setting("ai_max_turns") or 8) and mode == "auto":
                _do_handover(chat_id, {"reason": "лимит сообщений ИИ без результата", "summary": ctx["last_in"][:400]}, mode)
                return {"status": "handover"}
        except ValueError:
            pass
    state: dict = {"mode": mode}
    try:
        text = _reply(ctx, state, extra)
    except Exception as exc:  # noqa: BLE001
        _log(chat_id, "reply", setting("ai_model"), "error", "", None, 0, str(exc))
        return {"status": "error", "error": str(exc)[:200]}
    if nudge and (not text or NO_FOLLOWUP in text):
        _log(chat_id, "followup", state.get("model") or "", "skip", text, state.get("usage"), state.get("ms") or 0)
        return {"status": "skip"}
    if not nudge and not state.get("handover") and not state.get("lead"):
        with database.get_conn() as conn:  # the guest wrote more while we were thinking: answer everything at once instead
            newer = conn.execute("SELECT 1 FROM inbox_messages WHERE chat_id = ? AND direction = 'in' AND id > ? LIMIT 1", (chat_id, last_in_id)).fetchone()
        if newer and not _retry:
            _log(chat_id, "reply", state.get("model") or "", "stale", text, state.get("usage"), state.get("ms") or 0)
            return handle(chat_id, force=force, want_draft=want_draft, _retry=True)
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
            conn.execute("UPDATE inbox_chats SET ai_draft = ?, ai_draft_at = ?, ai_draft_photos = ?, rev = ? WHERE id = ?",
                         (text, util.now(), json.dumps(state.get("photos") or []), inbox._bump(conn), chat_id))  # noqa: SLF001
    if text and not nudge:
        with database.get_conn() as conn:
            conn.execute("UPDATE inbox_chats SET ai_last_in_id = ? WHERE id = ?", (last_in_id, chat_id))
    cost = _log(chat_id, "reply", state.get("model") or "", "handover" if state.get("handover") and not text else decision, text, state.get("usage"), state.get("ms") or 0)
    if state.get("handover") or state.get("lead"):
        bg.run(_update_memory, ctx, key=f"ai-mem-{chat_id}")
    return {"status": decision, "text": text, "cost": cost, "handover": bool(state.get("handover")), "lead": state.get("lead")}


NO_FOLLOWUP = "NO_FOLLOWUP"
MAX_PHOTOS = 30  # per apartment per send (Telegram albums of 10, WhatsApp one by one)


def followups() -> int:
    """A guest went silent after the assistant's offer: one short, warm nudge
    after ai_followup_hours (auto chats only, daytime only, once per silence)."""
    from . import inbox
    try:
        hours = float(setting("ai_followup_hours") or 0)
    except ValueError:
        hours = 0
    if hours <= 0 or not enabled() or not (9 <= datetime.now().hour < 21):
        return 0
    now = datetime.now()
    lo, hi = (now - timedelta(hours=36)).isoformat(timespec="seconds"), (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    with database.get_conn() as conn:
        rows = conn.execute("SELECT c.id FROM inbox_chats c WHERE c.last_dir = 'out' AND c.last_author = ? AND c.last_at BETWEEN ? AND ? "
                            "AND COALESCE(c.ai_mode, '') != 'off' AND (c.ai_followup_at IS NULL OR c.ai_followup_at < "
                            "(SELECT MAX(m.at) FROM inbox_messages m WHERE m.chat_id = c.id AND m.direction = 'in'))", (AUTHOR, lo, hi)).fetchall()
    n = 0
    for r in rows:
        chat = inbox.get_chat(r["id"])
        if not chat or chat_mode(chat) != "auto":
            continue
        with database.get_conn() as conn:  # the guest had to write at least once in the last 2 days
            had = conn.execute("SELECT 1 FROM inbox_messages WHERE chat_id = ? AND direction = 'in' AND at >= ? LIMIT 1",
                               (r["id"], (now - timedelta(days=2)).isoformat(timespec="seconds"))).fetchone()
            conn.execute("UPDATE inbox_chats SET ai_followup_at = ? WHERE id = ?", (now.isoformat(timespec="seconds"), r["id"]))
        if not had:
            continue
        try:
            res = handle(r["id"], nudge=True)
            if res.get("status") == "sent":
                n += 1
        except Exception:  # noqa: BLE001
            logger.exception("ai follow-up failed for chat %s", r["id"])
    return n


def send_draft(chat_id: int, author: str, text: str | None = None) -> dict:
    """«Отправить» on the draft panel: the (possibly edited) text, then the photos
    the assistant queued for it, as the assistant's messages."""
    from . import crm, inbox
    with database.get_conn() as conn:
        r = conn.execute("SELECT ai_draft, ai_draft_photos FROM inbox_chats WHERE id = ?", (chat_id,)).fetchone()
    if not r:
        return {"ok": False}
    body = (text if text is not None else r["ai_draft"]) or ""
    try:
        apts = json.loads(r["ai_draft_photos"] or "[]")
    except ValueError:
        apts = []
    out = {"ok": True, "photos": 0}
    pause_for_human(chat_id, body.strip())  # a person sends: pause, and an edited draft becomes an example/lesson
    if body.strip():
        inbox.send(chat_id, author, body.strip())
    for apt in apts[:3]:
        photos = crm.apartment_photos(apt)[:MAX_PHOTOS]
        out["photos"] += len(inbox.send_album(chat_id, author, apt, [(ph["media"], ph["mime"]) for ph in photos]))
    with database.get_conn() as conn:
        conn.execute("UPDATE inbox_chats SET ai_draft = NULL, ai_draft_photos = NULL, rev = ? WHERE id = ?", (inbox._bump(conn), chat_id))  # noqa: SLF001
    return out


def clear_draft(chat_id: int) -> None:
    from . import inbox
    with database.get_conn() as conn:
        conn.execute("UPDATE inbox_chats SET ai_draft = NULL, ai_draft_photos = NULL, rev = ? WHERE id = ?", (inbox._bump(conn), chat_id))  # noqa: SLF001


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
    if not good and last_out and last_in and enabled():
        bg.run(learn_lesson, chat_id, last_in["text"] or "", last_out["text"] or "", "", "dislike", key=f"ai-lesson-{chat_id}")


def test_call() -> dict:
    """Settings page «Проверить ключ»: one tiny request."""
    t0 = time.time()
    resp = _client().messages.create(model=setting("ai_model_triage") or "claude-haiku-5-5", max_tokens=20, output_config={"effort": "low"},
                                     messages=[{"role": "user", "content": "Ответь одним словом: готов?"}])
    text = next((b.text for b in resp.content if b.type == "text"), "")
    cost = _log(None, "test", resp.model, "ok", text, resp.usage, (time.time() - t0) * 1000)
    return {"ok": True, "model": resp.model, "text": text, "cost": cost}
