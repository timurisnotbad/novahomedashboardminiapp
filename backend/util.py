"""Small helpers that used to be copied into every module (digits(), the
month names, «now», number parsing, DD.MM.YYYY) — one place, one behaviour."""
import re
from datetime import date, datetime

RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
RU_MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
RU_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_NON_DIGIT = re.compile(r"\D")


def digits(s) -> str:
    """Only the digits of a phone (any spelling of the country code / spaces)."""
    return _NON_DIGIT.sub("", str(s or ""))


def phone9(s) -> str:
    """The last 9 digits — what identifies a number whatever the prefix; ''
    when the value is too short to be a phone."""
    d = digits(s)
    return d[-9:] if len(d) >= 7 else ""


def now() -> str:
    """Local time, ISO to the second — the timestamp format of the whole DB."""
    return datetime.now().isoformat(timespec="seconds")


def num(v, default=None):
    """'1 200,50' → 1200.5; None / '' / garbage → default."""
    if v in (None, ""):
        return default
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return default


def dmy(iso) -> str:
    """'2026-10-12…' → '12.10.2026'; anything shorter is returned as is."""
    s = str(iso or "")
    return f"{s[8:10]}.{s[5:7]}.{s[:4]}" if len(s) >= 10 else s


def human_date(iso, lang: str = "ru", year: bool = False) -> str:
    """'2026-10-12' → '12 октября' / '12 October' (optionally with the year)."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return str(iso or "")[:10]
    name = EN_MONTHS[d.month - 1] if lang == "en" else RU_MONTHS_GEN[d.month - 1]
    return f"{d.day} {name}" + (f" {d.year}" if year else "")
