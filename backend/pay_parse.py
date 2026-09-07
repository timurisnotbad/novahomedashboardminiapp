"""Parse payment posts from the team's Telegram payments channel and match
them to PMS bookings.

Posts are semi-structured; the parser is tolerant and extracts whatever it can:

    Счет/Касса: Kapital Bank **4980
    Сумма в валюте: 1 002 240 UZS
    Номер брони из RC: #RC-A066-2408
    Апартамент: A-066
    Даты: 24 августа — 25 августа 2026

or the short form: "073. 25-26.08. СЕРВЕТ нал фото".
"""
import re
from datetime import date

_CYR2LAT = str.maketrans({"А": "A", "В": "B", "Б": "B", "С": "C", "Е": "E"})

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def _year_for(month: int, ref: date) -> int:
    """Pick the year that puts `month` closest to the post date."""
    if month - ref.month > 6:
        return ref.year - 1
    if ref.month - month > 6:
        return ref.year + 1
    return ref.year


def _mk_date(day: int, month: int, ref: date):
    try:
        return date(_year_for(month, ref), month, day).isoformat()
    except ValueError:
        return None


def parse_payment(text: str, post_date: date, apartments=None) -> dict:
    """Extract amount/currency/method/apartment/dates/rc reference.
    `apartments` (known unit names) lets the short form resolve a leading
    digits-only number: '073. 25-26.08 …' -> B-073."""
    t = text or ""
    up = t.upper().translate(_CYR2LAT)
    out = {
        "amount": None, "currency": None, "method": None,
        "apartment": None, "checkin": None, "checkout": None, "rc_ref": None,
    }

    # --- amount: prefer the "Сумма..." line, else the largest grouped number
    m = re.search(r"Сумма[^\d]*([\d][\d  .,]*\d)", t, re.I)
    cand = None
    if m:
        cand = m.group(1)
    else:
        nums = re.findall(r"\b\d{1,3}(?:[  .,]\d{3})+\b|\b\d{5,10}\b", t)
        if nums:
            cand = max(nums, key=lambda s: int(re.sub(r"\D", "", s)))
    if cand:
        out["amount"] = int(re.sub(r"\D", "", cand))
    if re.search(r"UZS|сум", t, re.I):
        out["currency"] = "UZS"
    elif re.search(r"USD|\$", t):
        out["currency"] = "USD"
    elif out["amount"] and out["amount"] >= 100000:
        out["currency"] = "UZS"

    # --- method
    mm = re.search(r"\*\*\s?(\d{4})", t)
    if re.search(r"\bнал", t, re.I):
        out["method"] = "нал"
    elif mm:
        out["method"] = f"карта **{mm.group(1)}"
    else:
        for kw in ("uzcard", "humo", "visa", "click", "payme", "перевод"):
            if re.search(kw, t, re.I):
                out["method"] = kw
                break

    # --- RC reference: #RC-A066-2408 -> apartment + check-in day/month
    m = re.search(r"RC[-\s]?([A-ZА-Я])\s?-?\s?(\d{3})\s?-\s?(\d{2})(\d{2})", up)
    if m:
        letter = m.group(1).translate(_CYR2LAT)
        out["rc_ref"] = f"RC-{letter}{m.group(2)}-{m.group(3)}{m.group(4)}"
        out["apartment"] = f"{letter}-{m.group(2)}"
        ci = _mk_date(int(m.group(3)), int(m.group(4)), post_date)
        if ci:
            out["checkin"] = ci

    # --- apartment code anywhere in the text (A-066 / б-051 / B051)
    if not out["apartment"]:
        m = re.search(r"\b([A-Z])\s?-?\s?(\d{3})\b", up)
        if m:
            out["apartment"] = f"{m.group(1)}-{m.group(2)}"

    # --- leading digits-only unit number: "073. …" -> B-073 (unique suffix)
    if not out["apartment"] and apartments:
        m = re.match(r"\s*(\d{3})\b", t)
        if m:
            hits = [a for a in apartments if str(a).endswith(m.group(1))]
            if len(hits) == 1:
                out["apartment"] = hits[0]

    # --- dates "25-26.08" / "26-28.08"
    if not out["checkin"]:
        m = re.search(r"\b(\d{1,2})\s*[-–—]\s*(\d{1,2})\.(\d{1,2})\b", t)
        if m:
            d1, d2, mo = int(m.group(1)), int(m.group(2)), int(m.group(3))
            out["checkin"] = _mk_date(d1, mo, post_date)
            out["checkout"] = _mk_date(d2, mo, post_date)

    # --- dates "24 августа — 25 августа 2026"
    if not out["checkin"]:
        m = re.search(
            r"(\d{1,2})\s+([а-яА-Я]+)\s*[—–-]+\s*(\d{1,2})\s+([а-яА-Я]+)", t
        )
        if m:
            mo1 = _MONTHS.get(m.group(2)[:3].lower())
            mo2 = _MONTHS.get(m.group(4)[:3].lower())
            if mo1:
                out["checkin"] = _mk_date(int(m.group(1)), mo1, post_date)
            if mo2:
                out["checkout"] = _mk_date(int(m.group(3)), mo2, post_date)
    return out


def match_booking(parsed: dict, bookings: list[dict]) -> tuple:
    """Score bookings against the parsed post; return (booking_id, score) or
    (None, best_score). Threshold: 5 — apartment alone or dates alone are not
    enough to link money to a booking."""
    text_low = (parsed.get("_raw") or "").lower()
    best_id, best_score = None, 0
    for b in bookings:
        s = 0
        if parsed.get("apartment") and b.get("apartment_name") == parsed["apartment"]:
            s += 4
        if parsed.get("checkin") and b.get("begin_date") == parsed["checkin"]:
            s += 3
        if parsed.get("checkout") and b.get("end_date") == parsed["checkout"]:
            s += 1
        client = (b.get("client_name") or "").lower()
        if text_low and client:
            for word in re.findall(r"[a-zа-яё]{4,}", client):
                if word in text_low:
                    s += 2
                    break
        if s > best_score:
            best_id, best_score = b.get("id"), s
    if best_score >= 5:
        return best_id, best_score
    return None, best_score
