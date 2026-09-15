"""Read the «Поломки» topic: every line the team writes there becomes either a
shopping-list entry (Контроль → Закупки) or a repair task (Контроль → Задачи).

    В-071 надо купить крышку унитаза      → закупка: крышка унитаза (B-071)
    А -278 починить биде                  → задача:  починить биде (A-278)
    Смазать общую дверь около 066         → задача                 (A-066)
    B- 005 нет утюга                      → закупка: утюг (B-005)

Nobody has to learn a format: lines are split, the apartment is found
wherever it sits, "buy" words route the line to the shopping list.
"""
import re

# a line goes to the shopping list when it says so
BUY_RE = re.compile(
    r"(?iu)\b(?:купить|закупить|докупить|заказать|нужно|нужны|нужен|нужна|надо|"
    r"kerak|sotib|olish|zakaz)\b"
)
# "нет утюга", "utyug yo'q" — something is missing → buy it
MISSING_RE = re.compile(r"(?iu)(?:^|\s)(?:нет|нету|отсутствует|yo['ʻ’]?q|yoq)(?=\s|$)")
# words that only introduce the item ("надо купить крышку" → "крышку")
_BUY_LEAD = re.compile(
    r"(?iu)^(?:(?:надо|нужно|нужны|нужен|нужна|срочно|пожалуйста|please)\s+)*"
    r"(?:(?:купить|закупить|докупить|заказать|kerak|sotib\s+olish\s+kerak)\s*[:—-]*\s*)?"
)
_MISSING_LEAD = re.compile(r"(?iu)^(?:нет|нету|отсутствует)\s+")
_MISSING_TAIL = re.compile(r"(?iu)\s+(?:нет|нету|отсутствует|yo['ʻ’]?q|yoq|kerak)\s*[.!]*$")
# the apartment mention, wherever it sits: "В-071", "А -278", "около 066", "в 103"
_APT_TOKEN = re.compile(
    r"(?iu)(?:\b(?:в|для|на|около|у|кв\.?|квартира|квартире|uchun|ga|da)\s+)?"
    r"(?<![\w-])[A-Za-zА-Яа-я]?\s?-?\s?\d{3}(?![\w-])\s*[:—-]?"
)
# chatter that is not an issue: acknowledgements, questions
_ACK_RE = re.compile(
    r"(?iu)^\s*(?:ок|окей|ok|okay|хорошо|ладно|понял|поняла|принято|сделано|сделала|сделал|готово|"
    r"спасибо|да|нет|xop|хоп|rahmat|bo['ʻ’]?ldi|tayyor|\+|👍|✅)\s*[.!]*\s*$"
)
_QTY_TAIL = re.compile(r"(?iu)^(.*?)[\s×x*]+(\d{1,2})\s*(?:шт\.?|штук|dona|ta|pcs)?\s*$")


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" .;:-—•·*")
    return s


def parse_lines(text: str, match_apartment) -> list[dict]:
    """[{kind: 'buy'|'fix', apartment, text, qty}] — one per meaningful line."""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = _clean(raw)
        if not line or _ACK_RE.match(line) or line.endswith("?"):
            continue
        apt = match_apartment(line)
        body = line
        if apt:
            body = _clean(_APT_TOKEN.sub(" ", body, count=1))
            body = re.sub(r"(?iu)^(?:в|для|на|около|у|uchun|ga|da)\s+", "", body).strip()
        if not body:
            continue
        words = [w for w in re.findall(r"\w+", body) if not w.isdigit()]
        if not apt and len(words) < 2:
            continue  # "ок", a bare number, a name — not an issue
        if BUY_RE.search(line) or MISSING_RE.search(line):
            item = _BUY_LEAD.sub("", body, count=1)
            item = _MISSING_LEAD.sub("", item, count=1)
            item = _MISSING_TAIL.sub("", item, count=1)
            item = _clean(item) or body
            qty = 1
            m = _QTY_TAIL.match(item)
            if m and m.group(1).strip():
                item, qty = m.group(1).strip(), int(m.group(2))
            out.append({"kind": "buy", "apartment": apt, "text": item[:80], "qty": max(1, qty)})
        else:
            out.append({"kind": "fix", "apartment": apt, "text": body[:200], "qty": 1})
    return out


def src_key(chat_id: int, message_id: int, kind: str, apartment, text: str) -> str:
    """Stable id of one line of one chat message — an edited message adds only
    the lines that are new."""
    norm = re.sub(r"\W+", "", (text or "").lower())
    return f"{chat_id}:{message_id}:{kind}:{apartment or ''}:{norm[:60]}"
