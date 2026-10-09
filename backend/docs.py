"""Documents and email for the CRM.

Documents (счёт / квитанция / подтверждение) are made from a booking, stored as
a snapshot in crm_documents and rendered to PDF on demand (reportlab, DejaVu
fonts bundled in backend/fonts so Cyrillic works on any machine). Email goes
through plain SMTP (SMTP_* in .env, Gmail app password works) and is logged in
crm_emails. Company details for the header live in crm_settings «company».
"""
import json
import logging
import smtplib
from datetime import date, datetime
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path

from . import config, database

logger = logging.getLogger("nova.docs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER,
    client_id INTEGER,
    kind TEXT NOT NULL,              -- invoice | receipt | confirmation
    number TEXT,
    lang TEXT DEFAULT 'ru',
    amount REAL,
    currency TEXT DEFAULT 'USD',
    data TEXT,                       -- JSON snapshot of the booking + company at creation
    created_by INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS crm_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_addr TEXT,
    subject TEXT,
    body TEXT,
    booking_id INTEGER,
    client_id INTEGER,
    doc_id INTEGER,
    status TEXT,                     -- sent | failed
    error TEXT,
    by_user TEXT,
    at TEXT
);
"""

KINDS = {"invoice": "INV", "receipt": "RCP", "confirmation": "CNF"}
T = {
    "en": {"invoice": "INVOICE", "receipt": "RECEIPT", "confirmation": "Reservation Confirmation",
           "conf_sub": "OFFICIAL BOOKING CONFIRMATION ISSUED BY {brand}", "confirmed": "✓ CONFIRMED", "no": "No.", "issued": "Issued",
           "bill_from": "BILL FROM", "bill_to": "BILL TO", "source": "Booking source", "ref": "Ref", "stay": "STAY DETAILS",
           "desc": "DESCRIPTION", "checkin": "CHECK-IN", "checkout": "CHECK-OUT", "nights": "NIGHTS", "amount": "AMOUNT",
           "accom": "Accommodation", "per_night": "/night", "from": "from", "until": "until", "subtotal": "Subtotal", "tax": "Tax / VAT",
           "included": "Included", "total": "TOTAL", "paid_full": "✓ PAID IN FULL", "paid_part": "PAID {paid} · DUE {due}", "unpaid": "DUE {due}",
           "via": "via", "thanks": "Thank you for choosing {brand}.", "official": "This invoice serves as an official payment confirmation.",
           "receipt_official": "This receipt confirms the payment received.", "questions": "For questions", "guest": "GUEST",
           "conf_no": "CONFIRMATION NO.", "src": "SOURCE", "property": "Property", "unit": "Unit(s)", "address": "Address",
           "ci": "Check-in", "co": "Check-out", "len": "Length of stay", "night1": "night", "nightn": "nights", "payment": "PAYMENT",
           "pay_status": "Payment status", "st_conf": "Reservation Confirmed", "st_paid": "Paid in full", "st_part": "Partially paid · due {due}",
           "st_unpaid": "Payment due at check-in", "conf_text": "This document confirms that the reservation above has been successfully registered in the {brand} reservation system.",
           "director": "DIRECTOR"},
    "ru": {"invoice": "СЧЁТ", "receipt": "КВИТАНЦИЯ", "confirmation": "Подтверждение бронирования",
           "conf_sub": "ОФИЦИАЛЬНОЕ ПОДТВЕРЖДЕНИЕ БРОНИРОВАНИЯ · {brand}", "confirmed": "✓ ПОДТВЕРЖДЕНО", "no": "№", "issued": "Дата",
           "bill_from": "ИСПОЛНИТЕЛЬ", "bill_to": "ГОСТЬ", "source": "Источник брони", "ref": "Реф.", "stay": "ПРОЖИВАНИЕ",
           "desc": "ОПИСАНИЕ", "checkin": "ЗАЕЗД", "checkout": "ВЫЕЗД", "nights": "НОЧЕЙ", "amount": "СУММА",
           "accom": "Проживание", "per_night": "/ночь", "from": "с", "until": "до", "subtotal": "Подытог", "tax": "Налоги",
           "included": "Включены", "total": "ИТОГО", "paid_full": "✓ ОПЛАЧЕНО ПОЛНОСТЬЮ", "paid_part": "ОПЛАЧЕНО {paid} · К ОПЛАТЕ {due}", "unpaid": "К ОПЛАТЕ {due}",
           "via": "через", "thanks": "Спасибо, что выбрали {brand}.", "official": "Этот счёт является официальным подтверждением оплаты.",
           "receipt_official": "Квитанция подтверждает получение оплаты.", "questions": "Вопросы", "guest": "ГОСТЬ",
           "conf_no": "НОМЕР БРОНИ", "src": "ИСТОЧНИК", "property": "Объект", "unit": "Апартаменты", "address": "Адрес",
           "ci": "Заезд", "co": "Выезд", "len": "Продолжительность", "night1": "ночь", "nightn": "ночей", "payment": "ОПЛАТА",
           "pay_status": "Статус оплаты", "st_conf": "Бронирование подтверждено", "st_paid": "Оплачено полностью", "st_part": "Частичная оплата · к оплате {due}",
           "st_unpaid": "Оплата при заезде", "conf_text": "Документ подтверждает, что бронирование выше зарегистрировано в системе бронирования {brand}.",
           "director": "ДИРЕКТОР"},
}
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
DEFAULT_COMPANY = {"brand": "NOVA HOME", "name": "NOVA HOME Apartments", "legal": "YATT Zakirov Timur Sadriddinovich",
                   "tagline": "NEST ONE · TASHKENT, UZBEKISTAN", "property": "NOVA HOME Apartments — Nest One",
                   "address": "Nest One, Tashkent City 100100, Uzbekistan", "phone": "+998 33 320 05 28",
                   "email": "timurhotel2005@gmail.com", "website": "novahome.uz", "signer": "Timur Zakirov", "signer_title": "DIRECTOR, NOVA HOME",
                   "inn": "", "bank": "", "currency": "USD", "note": "", "accent": "#8B7D5A"}


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)


# ---- company --------------------------------------------------------------------
def company() -> dict:
    from . import crm
    try:
        c = json.loads(crm.get_setting("company", "") or "{}")
    except ValueError:
        c = {}
    return {**DEFAULT_COMPANY, **{k: v for k, v in c.items() if isinstance(v, str)}}


def set_company(data: dict) -> dict:
    from . import crm
    cur = company()
    for k in DEFAULT_COMPANY:
        if k in data and isinstance(data[k], str):
            cur[k] = data[k].strip()
    crm.set_setting("company", json.dumps(cur, ensure_ascii=False))
    return cur


# ---- documents -------------------------------------------------------------------
def _fmt_money(v, cur="USD") -> str:
    v = float(v or 0)
    s = f"{v:,.2f}".replace(",", " ")
    return f"{s} {'$' if cur == 'USD' else cur}"


def _dmy(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if iso and len(iso) >= 10 else (iso or "")


def _unit_code(apartment: str) -> str:
    import re
    m = re.search(r"([A-Za-zА-Яа-я])\s*-?\s*(\d{2,4})", apartment or "")
    return (m.group(1).upper() + m.group(2)) if m else re.sub(r"[^A-Za-z0-9]", "", apartment or "")[:6].upper()


def _next_number(conn, kind: str, b: dict | None = None) -> str:
    """INV-2026-0912-A067: type, check-in date, unit; a suffix when the same stay gets a second document."""
    base = f"{KINDS.get(kind, 'DOC')}-{(b or {}).get('checkin', date.today().isoformat()).replace('-', '')[:4]}-" \
           f"{(b or {}).get('checkin', date.today().isoformat()).replace('-', '')[4:8]}-{_unit_code((b or {}).get('apartment') or '')}"
    n = conn.execute("SELECT COUNT(*) FROM crm_documents WHERE number = ? OR number LIKE ?", (base, base + "-%")).fetchone()[0]
    return base if n == 0 else f"{base}-{n + 1}"


def create_document(bid: int, kind: str, uid: int, lang: str = "ru", amount=None) -> dict:
    from . import crm
    if kind not in KINDS:
        raise ValueError("Неизвестный тип документа")
    lang = lang if lang in T else "ru"
    with database.get_conn() as conn:
        b = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
        if not b:
            raise ValueError("Бронь не найдена")
        b = crm._booking_out(dict(b))  # noqa: SLF001
        cid = crm.ensure_client(b["phone"] or "", b["guest"] or "", b["source"]) if b["phone"] else None
        client = crm.get_client(cid) if cid else None
        comp = company()
        total = float(b.get("amount") or 0)
        paid = float(b.get("paid") or 0)
        debt = max(0.0, total - paid)
        prepay = paid
        if amount is None:
            amount = debt if kind == "invoice" and debt > 0 else (paid if kind == "receipt" else total)
        snap = {"guest": b["guest"], "phone": b["phone"], "email": (client or {}).get("email") or "", "apartment": b["apartment"],
                "checkin": b["checkin"], "checkout": b["checkout"], "nights": b.get("nights") or 0, "source": b["source"],
                "total": total, "paid": paid, "debt": debt, "prepayment": prepay, "arrival_time": b.get("arrival_time"),
                "departure_time": b.get("departure_time"), "notes": b.get("notes") or "", "company": comp}
        number = str(bid) if kind == "confirmation" else _next_number(conn, kind, b)  # confirmation no. = the calendar booking id
        did = conn.execute("INSERT INTO crm_documents (booking_id, client_id, kind, number, lang, amount, currency, data, created_by, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (bid, cid, kind, number, lang, float(amount or 0), comp.get("currency") or "USD",
                            json.dumps(snap, ensure_ascii=False), uid, datetime.now().isoformat(timespec="seconds"))).lastrowid
    return get_document(did)


def _doc_out(r) -> dict:
    d = dict(r)
    try:
        d["data"] = json.loads(d.get("data") or "{}")
    except ValueError:
        d["data"] = {}
    d["title"] = {"invoice": "Invoice", "receipt": "Receipt", "confirmation": "Reservation Confirmation"}[d["kind"]] if (d.get("lang") or "ru") == "en" else {"invoice": "Счёт", "receipt": "Квитанция", "confirmation": "Подтверждение брони"}[d["kind"]]
    d["url"] = f"{config.API_PREFIX}/crm/documents/{d['id']}.pdf"
    return d


def get_document(did: int) -> dict | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT * FROM crm_documents WHERE id = ?", (did,)).fetchone()
    return _doc_out(r) if r else None


def documents(booking_id: int | None = None, client_id: int | None = None) -> list[dict]:
    where, args = [], []
    if booking_id:
        where.append("booking_id = ?"); args.append(booking_id)
    if client_id:
        where.append("client_id = ?"); args.append(client_id)
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM crm_documents" + (" WHERE " + " AND ".join(where) if where else "")
                            + " ORDER BY id DESC LIMIT 100", args).fetchall()
    return [_doc_out(r) for r in rows]


def delete_document(did: int) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_documents WHERE id = ?", (did,))


# ---- PDF ---------------------------------------------------------------------------
_fonts_ready = False


def _fonts() -> dict:
    global _fonts_ready
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if not _fonts_ready:
        base = Path(__file__).parent / "fonts"
        try:
            pdfmetrics.registerFont(TTFont("DejaVu", str(base / "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(base / "DejaVuSans-Bold.ttf")))
            pdfmetrics.registerFont(TTFont("DejaVuSerif", str(base / "DejaVuSerif.ttf")))
            pdfmetrics.registerFont(TTFont("DejaVuSerif-Bold", str(base / "DejaVuSerif-Bold.ttf")))
            _fonts_ready = True
        except Exception:  # noqa: BLE001
            logger.exception("DejaVu fonts not registered; Cyrillic will not render")
            return {"r": "Helvetica", "b": "Helvetica-Bold", "s": "Times-Roman", "sb": "Times-Bold"}
    return {"r": "DejaVu", "b": "DejaVu-Bold", "s": "DejaVuSerif", "sb": "DejaVuSerif-Bold"}


def _sp(s: str) -> str:
    """Letter-spaced small caps look (N E S T  O N E), like the brand documents."""
    out = "\u2009".join((s or "").upper())
    return out.replace("\u2009 \u2009", "\u00a0\u00a0\u00a0")


def _long_date(iso: str, lang: str) -> str:
    if not iso or len(iso) < 10:
        return iso or ""
    y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    return f"{d} {MONTHS_EN[m - 1]} {y}" if lang == "en" else f"{d} {MONTHS_RU[m - 1]} {y}"


def _money(v, cur="USD") -> str:
    v = float(v or 0)
    s = f"{v:,.2f}"
    return f"US$ {s}" if cur == "USD" else (f"€ {s}" if cur == "EUR" else f"{s} {cur}")


def render_pdf(doc: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    F = _fonts()
    lang = doc.get("lang") if doc.get("lang") in T else "en"
    t = T[lang]
    d = doc["data"]
    comp = {**DEFAULT_COMPANY, **(d.get("company") or {})}
    cur = doc.get("currency") or comp.get("currency") or "USD"
    accent = colors.HexColor(comp.get("accent") or "#8B7D5A")
    ink = colors.HexColor("#1F2328")
    grey = colors.HexColor("#6B7280")
    line = colors.HexColor("#D9D4C7")
    P = lambda name, **kw: ParagraphStyle(name, **{"fontName": F["r"], "fontSize": 9.5, "leading": 13, "textColor": ink, **kw})  # noqa: E731
    st = {"brand": P("brand", fontName=F["b"], fontSize=15, leading=18, textColor=accent),
          "tag": P("tag", fontSize=7, leading=10, textColor=grey),
          "label": P("label", fontName=F["b"], fontSize=7, leading=10, textColor=grey),
          "p": P("p"), "pb": P("pb", fontName=F["b"]), "small": P("small", fontSize=8, leading=11, textColor=grey),
          "title": P("title", fontName=F["sb"], fontSize=22, leading=26, textColor=accent, alignment=TA_RIGHT),
          "titleL": P("titleL", fontName=F["s"], fontSize=20, leading=24, textColor=accent),
          "r": P("r", alignment=TA_RIGHT), "rb": P("rb", fontName=F["b"], alignment=TA_RIGHT),
          "rs": P("rs", fontSize=8, leading=11, textColor=grey, alignment=TA_RIGHT),
          "big": P("big", fontName=F["b"], fontSize=13, leading=16),
          "total": P("total", fontName=F["b"], fontSize=12, leading=15, textColor=accent),
          "totalR": P("totalR", fontName=F["b"], fontSize=12, leading=15, textColor=accent, alignment=TA_RIGHT),
          "ok": P("ok", fontName=F["b"], fontSize=8.5, leading=11, textColor=colors.HexColor("#2E7D32")),
          "sig": P("sig", fontName=F["s"], fontSize=11, leading=14, alignment=TA_RIGHT),
          "sigs": P("sigs", fontSize=6.5, leading=9, textColor=grey, alignment=TA_RIGHT)}
    buf = BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=16 * mm,
                            title=f"{doc['title']} {doc['number']}", author=comp.get("name") or "")
    W = A4[0] - 40 * mm
    el = []
    nights = int(d.get("nights") or 0)
    total, paid = float(d.get("total") or 0), float(d.get("paid") or 0)
    due = max(0.0, total - paid)
    per = total / nights if nights else 0
    brand = comp.get("brand") or "NOVA HOME"
    contacts = " · ".join(x for x in (comp.get("website"), comp.get("phone")) if x)
    def grid(rows, widths, style):
        tb = Table(rows, colWidths=widths)
        tb.setStyle(TableStyle(style))
        return tb
    hr = lambda c=line, w=0.6: [("LINEBELOW", (0, 0), (-1, -1), w, c)]  # noqa: E731

    if doc["kind"] in ("invoice", "receipt"):
        head = grid([[Paragraph(f"{_sp(brand)}<br/><font size=7 color='#6B7280'>{_sp(comp.get('tagline') or '')}</font><br/><font size=8 color='#6B7280'>{contacts}</font>", st["brand"]),
                      Paragraph(f"{t[doc['kind']]}<br/><font name='{F['r']}' size=8.5 color='#1F2328'>{t['no']} {doc['number']}<br/>{t['issued']}: {_long_date(doc['created_at'][:10], lang)}</font>", st["title"])]],
                     [W * 0.55, W * 0.45], [("VALIGN", (0, 0), (-1, -1), "TOP")])
        el += [head, Spacer(1, 9 * mm)]
        guest_lines = [f"<b>{d.get('guest') or ''}</b>"]
        if d.get("phone"):
            guest_lines.append("+" + d["phone"])
        if d.get("email"):
            guest_lines.append(d["email"])
        guest_lines.append(f"{t['source']}: {d.get('source') or ''}")
        guest_lines.append(f"{t['ref']}: #RC-{doc.get('booking_id') or ''}")
        from_lines = [f"<b>{comp.get('name') or ''}</b>"] + [x for x in (comp.get("legal"), comp.get("address"), comp.get("email")) if x]
        parties = grid([[Paragraph(_sp(t["bill_from"]), st["label"]), Paragraph(_sp(t["bill_to"]), st["label"])],
                        [Paragraph("<br/>".join(from_lines), st["p"]), Paragraph("<br/>".join(guest_lines), st["p"])]],
                       [W / 2, W / 2], [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBEFORE", (1, 0), (1, -1), 0.6, line), ("LEFTPADDING", (1, 0), (1, -1), 10),
                                        ("BOTTOMPADDING", (0, 0), (-1, 0), 4)])
        el += [parties, Spacer(1, 9 * mm), Paragraph(_sp(t["stay"]), st["label"]), Spacer(1, 2 * mm)]
        desc = f"<b>{d.get('apartment') or ''}</b><br/><font size=8 color='#6B7280'>{t['accom']} · {_money(per, cur)}{t['per_night']}</font>"
        ci = f"{_long_date(d.get('checkin'), lang)}<br/><font size=7.5 color='#6B7280'>{t['from']} {d.get('arrival_time') or '14:00'}</font>"
        co = f"{_long_date(d.get('checkout'), lang)}<br/><font size=7.5 color='#6B7280'>{t['until']} {d.get('departure_time') or '11:00'}</font>"
        rows = [[Paragraph(_sp(t["desc"]), st["label"]), Paragraph(_sp(t["checkin"]), st["label"]), Paragraph(_sp(t["checkout"]), st["label"]),
                 Paragraph(_sp(t["nights"]), P("lc", fontName=F["b"], fontSize=7, textColor=grey, alignment=1)), Paragraph(_sp(t["amount"]), P("lr", fontName=F["b"], fontSize=7, textColor=grey, alignment=TA_RIGHT))],
                [Paragraph(desc, st["p"]), Paragraph(ci, st["p"]), Paragraph(co, st["p"]), Paragraph(f"<b>{nights}</b>", P("c", alignment=1)), Paragraph(f"<b>{_money(total, cur)}</b>", st["rb"])]]
        el.append(grid(rows, [W * 0.38, W * 0.17, W * 0.17, W * 0.12, W * 0.16],
                       [("LINEBELOW", (0, 0), (-1, 0), 0.8, ink), ("LINEBELOW", (0, 1), (-1, 1), 0.6, line), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 1), (-1, 1), 7), ("BOTTOMPADDING", (0, 1), (-1, 1), 7)]))
        el.append(Spacer(1, 7 * mm))
        totals = [[Paragraph(t["subtotal"], st["p"]), Paragraph(_money(total, cur), st["r"])],
                  [Paragraph(t["tax"], st["p"]), Paragraph(t["included"], st["r"])],
                  [Paragraph(t["total"], st["total"]), Paragraph(_money(total, cur), st["totalR"])]]
        box = grid(totals, [W * 0.22, W * 0.22], [("BOX", (0, 0), (-1, -1), 0.6, line), ("LINEBELOW", (0, 0), (-1, -2), 0.6, line),
                                                  ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#FAF8F3")), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                                  ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8)])
        if paid >= total - 0.005 and total:
            pay_txt = f"{t['paid_full']} <font name='{F['r']}' color='#6B7280'>{t['via']} {d.get('source') or ''}</font>"
        elif paid > 0:
            pay_txt = t["paid_part"].format(paid=_money(paid, cur), due=_money(due, cur))
        else:
            pay_txt = t["unpaid"].format(due=_money(due or total, cur))
        pay = grid([[Paragraph(pay_txt, st["ok"] if paid >= total - 0.005 and total else P("due", fontName=F["b"], fontSize=8.5, textColor=accent))]],
                   [W * 0.44], [("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5C0") if paid >= total - 0.005 and total else line),
                                ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8)])
        right = grid([[box], [Spacer(1, 3 * mm)], [pay]], [W * 0.44], [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)])
        el.append(grid([["", right]], [W * 0.56, W * 0.44], [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        if comp.get("bank") and doc["kind"] == "invoice" and due > 0:
            el += [Spacer(1, 6 * mm), Paragraph(_sp("BANK DETAILS" if lang == "en" else "РЕКВИЗИТЫ"), st["label"]), Paragraph((comp["bank"] or "").replace("\n", "<br/>"), st["small"])]
        el.append(Spacer(1, 16 * mm))
        foot_l = [t["thanks"].format(brand=comp.get("name") or brand), t["official"] if doc["kind"] == "invoice" else t["receipt_official"],
                  f"{t['questions']}: {' · '.join(x for x in (comp.get('email'), comp.get('phone')) if x)}"]
        if comp.get("note"):
            foot_l.append(comp["note"])
        foot = grid([[Paragraph("<br/>".join(foot_l), st["small"]),
                      Paragraph(f"{comp.get('signer') or ''}<br/><font name='{F['r']}' size=6.5 color='#6B7280'>{_sp(comp.get('signer_title') or '')}</font>", st["sig"])]],
                    [W * 0.62, W * 0.38], [("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LINEABOVE", (1, 0), (1, 0), 0.6, line), ("TOPPADDING", (1, 0), (1, 0), 6)])
        el.append(foot)
    else:  # confirmation
        head = grid([[Paragraph(f"{_sp(brand)}<br/><font size=7 color='#6B7280'>{_sp(comp.get('tagline') or '')}</font>", st["brand"]),
                      grid([[Paragraph(t["confirmed"], P("cf", fontName=F["b"], fontSize=7.5, textColor=colors.HexColor("#2E7D32"), alignment=1))]], [38 * mm],
                           [("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5C0")), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)])]],
                    [W * 0.7, W * 0.3], [("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")])
        el += [head, Spacer(1, 8 * mm), Paragraph(t["confirmation"], st["titleL"]), Paragraph(_sp(t["conf_sub"].format(brand=brand)), st["tag"]), Spacer(1, 8 * mm)]
        top = grid([[Paragraph(_sp(t["guest"]), st["label"]), Paragraph(_sp(t["conf_no"]), st["label"]), Paragraph(_sp(t["src"]), st["label"])],
                    [Paragraph(d.get("guest") or "", st["big"]), Paragraph(doc["number"], st["big"]), Paragraph(d.get("source") or "", st["big"])]],
                   [W * 0.45, W * 0.3, W * 0.25], [("BOX", (0, 0), (-1, -1), 0.6, line), ("LINEBEFORE", (1, 0), (2, -1), 0.6, line),
                                                     ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, 1), (-1, 1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 9)])
        el += [top, Spacer(1, 8 * mm), Paragraph(_sp(t["stay"]), st["label"]), Spacer(1, 2 * mm)]
        rows = [[t["property"], comp.get("property") or comp.get("name") or ""], [t["unit"], d.get("apartment") or ""], [t["address"], comp.get("address") or ""],
                [t["ci"], f"{_long_date(d.get('checkin'), lang)}, {d.get('arrival_time') or '14:00'}"],
                [t["co"], f"{_long_date(d.get('checkout'), lang)}, {d.get('departure_time') or '11:00'}"],
                [t["len"], f"{nights} {t['night1'] if nights == 1 else t['nightn']}"]]
        el.append(grid([[Paragraph(a, st["p"]), Paragraph(f"<b>{b}</b>", st["rb"])] for a, b in rows], [W * 0.3, W * 0.7],
                       [("BOX", (0, 0), (-1, -1), 0.6, line), ("LINEBELOW", (0, 0), (-1, -2), 0.6, line), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9)]))
        el += [Spacer(1, 8 * mm), Paragraph(_sp(t["payment"]), st["label"]), Spacer(1, 2 * mm)]
        status = t["st_paid"] if (total and paid >= total - 0.005) else (t["st_part"].format(due=_money(due, cur)) if paid > 0 else (t["st_conf"] if d.get("status") in ("confirmed", "booked") and not total else t["st_unpaid"]))
        if not total:
            status = t["st_conf"]
        el.append(grid([[Paragraph(t["accom"], st["p"]), Paragraph(_money(total, cur), P("acc", textColor=accent, alignment=TA_RIGHT))],
                        [Paragraph(t["pay_status"], st["p"]), Paragraph(f"<b>{status}</b>", P("ps", fontName=F["b"], textColor=colors.HexColor("#2E7D32"), alignment=TA_RIGHT))]],
                       [W * 0.5, W * 0.5], [("LINEBELOW", (0, 0), (-1, 0), 0.6, line), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                                            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12)]))
        if d.get("notes"):
            el += [Spacer(1, 4 * mm), Paragraph(d["notes"], st["small"])]
        el.append(Spacer(1, 16 * mm))
        foot_l = [t["conf_text"].format(brand=brand), "", f"<b>{brand}</b> · {comp.get('legal') or ''}",
                  " · ".join(x for x in (comp.get("website"), comp.get("email"), comp.get("phone")) if x)]
        foot = grid([[Paragraph("<br/>".join(foot_l), st["small"]),
                      Paragraph(f"{comp.get('signer') or ''}<br/><font name='{F['r']}' size=6.5 color='#6B7280'>{_sp(t['director'])}</font>", st["sig"])]],
                    [W * 0.65, W * 0.35], [("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LINEABOVE", (1, 0), (1, 0), 0.6, line), ("TOPPADDING", (1, 0), (1, 0), 6)])
        el.append(foot)
    pdf.build(el)
    return buf.getvalue()


# ---- email -------------------------------------------------------------------------
def email_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def email_status() -> dict:
    return {"configured": email_configured(), "host": config.SMTP_HOST, "user": config.SMTP_USER,
            "from": config.SMTP_FROM or config.SMTP_USER, "port": config.SMTP_PORT, "log": email_log(10)}


def email_log(limit: int = 20) -> list[dict]:
    with database.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM crm_emails ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def send_email(to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] | None = None,
               who: str = "", booking_id=None, client_id=None, doc_id=None) -> dict:
    to = (to or "").strip()
    if not to or "@" not in to:
        raise ValueError("Укажите email получателя")
    if not email_configured():
        raise ValueError("Email не настроен: заполните SMTP_HOST, SMTP_USER, SMTP_PASSWORD в .env")
    msg = EmailMessage()
    sender = config.SMTP_FROM or config.SMTP_USER
    msg["From"] = f"{config.SMTP_FROM_NAME} <{sender}>" if config.SMTP_FROM_NAME else sender
    msg["To"] = to
    msg["Subject"] = subject or "Nova Home"
    msg.set_content(body or "")
    for name, data, mime in attachments or []:
        main, _, sub = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub or "octet-stream", filename=name)
    status, error = "sent", ""
    try:
        if config.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        status, error = "failed", str(exc)[:500]
    with database.get_conn() as conn:
        conn.execute("INSERT INTO crm_emails (to_addr, subject, body, booking_id, client_id, doc_id, status, error, by_user, at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (to, subject, (body or "")[:4000], booking_id, client_id, doc_id, status, error, who,
                      datetime.now().isoformat(timespec="seconds")))
        conn.execute("DELETE FROM crm_emails WHERE id NOT IN (SELECT id FROM crm_emails ORDER BY id DESC LIMIT 500)")
    if status == "failed":
        raise ValueError(f"Письмо не отправлено: {error}")
    return {"ok": True}
