"""Shopping list ("расходники"): parse the cleaners' requests and format the
list for the owner.

A request is a message starting with "нужно" / "надо" / "купить" / "kerak",
optionally an apartment code, then items separated by commas:
    нужно 103 полотенца 2, шампунь, туалетная бумага
    kerak B-051 sochiq 4
A quantity is a number next to the item ("полотенца 2", "2 полотенца", "x2").
"""
import re
from collections import OrderedDict

from . import database

# Deliberately narrow: "надо"/"купить" start too many ordinary sentences in
# the work chat ("надо позвонить гостю"). "нужно" is kept because staff were
# taught this exact form; in groups the bot additionally requires an apartment
# code, a comma-separated list or a quantity (see looks_like_list).
TRIGGER_RE = re.compile(
    r"(?iu)^\s*(?:нужно|нужны|закупить|докупить|закупка|kerak|sotib\s+olish\s+kerak)\b[\s:—-]*"
)
# quantities are 1–2 digits: a 3-digit number is an apartment, never a count
_QTY_TAIL = re.compile(r"(?iu)^(.*?)[\s×x*]+(\d{1,2})\s*(?:шт\.?|штук|dona|ta|pcs)?\s*$")
_QTY_HEAD = re.compile(r"(?iu)^(\d{1,2})\s*(?:шт\.?|штук|dona|ta|pcs)?\s+(.+)$")
# the apartment mention, wherever it sits: "103", "б-051", "в 103", "для B-103:"
_APT_TOKEN = re.compile(
    r"(?iu)(?:\b(?:в|для|на|кв\.?|квартира|uchun|ga)\s+)?(?<![\w-])[A-Za-zА-Яа-я]?\s?-?\s?\d{3}(?![\w-])\s*[:—-]?"
)


def is_request(text: str) -> bool:
    return bool(text) and TRIGGER_RE.match(text) is not None


def parse_items(text: str, match_apartment) -> tuple[str | None, list[tuple[str, int]]]:
    """Return (apartment or None, [(item, qty), ...]). `match_apartment` is the
    bot's apartment matcher (accepts bare 3-digit numbers)."""
    body = TRIGGER_RE.sub("", text or "", count=1).strip()
    apt = match_apartment(body) if body else None
    if apt:
        # drop the token that named the apartment, wherever it is:
        # "103 полотенца", "полотенца в 103", "для B-103: шампунь"
        body = _APT_TOKEN.sub(" ", body, count=1)
    items: list[tuple[str, int]] = []
    for raw in re.split(r"[,;\n]|\s+и\s+|\s+va\s+", body):
        it = re.sub(r"\s+", " ", raw).strip(" .;:-—")
        if not it or re.fullmatch(r"(?iu)(в|для|на|uchun|ga)", it):
            continue
        qty = 1
        m = _QTY_TAIL.match(it)
        if m and m.group(1).strip():
            it, qty = m.group(1).strip(), int(m.group(2))
        else:
            m = _QTY_HEAD.match(it)
            if m:
                qty, it = int(m.group(1)), m.group(2).strip()
        items.append((it[:80], max(1, qty)))
    return apt, items


def looks_like_list(apt, items: list[tuple[str, int]]) -> bool:
    """In a group chat only act on messages that are clearly a shopping list:
    an apartment is named, or several items, or an explicit quantity."""
    if apt or len(items) > 1:
        return True
    return bool(items) and items[0][1] > 1


def aggregate(rows: list[dict]) -> list[dict]:
    """Group open rows by item name (case-insensitive): total qty + apartments."""
    agg: "OrderedDict[str, dict]" = OrderedDict()
    for r in rows:
        key = (r.get("item") or "").strip().lower()
        if not key:
            continue
        a = agg.setdefault(key, {"item": (r.get("item") or "").strip(), "qty": 0, "apartments": [], "ids": []})
        a["qty"] += int(r.get("qty") or 1)
        apt = r.get("apartment")
        if apt and apt not in a["apartments"]:
            a["apartments"].append(apt)
        a["ids"].append(r.get("id"))
    return list(agg.values())


def format_list(rows: list[dict] | None = None) -> str:
    rows = database.open_supplies() if rows is None else rows
    if not rows:
        return "🛒 Список закупок пуст."
    lines = [f"🛒 Закупить ({len(rows)} позиц.):"]
    for a in aggregate(rows):
        where = f" ({', '.join(a['apartments'])})" if a["apartments"] else ""
        qty = f" ×{a['qty']}" if a["qty"] > 1 else ""
        lines.append(f"  • {a['item']}{qty}{where}")
    lines.append("")
    lines.append("Отметить купленное — в дашборде: Контроль → Закупки.")
    return "\n".join(lines)
