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
    "ru": {"invoice": "Счёт на оплату", "receipt": "Квитанция об оплате", "confirmation": "Подтверждение бронирования",
           "no": "№", "date": "Дата", "guest": "Гость", "phone": "Телефон", "apartment": "Апартаменты", "checkin": "Заезд",
           "checkout": "Выезд", "nights": "Ночей", "source": "Источник", "desc": "Наименование", "sum": "Сумма",
           "stay": "Проживание", "total": "Итого", "paid": "Оплачено", "due": "К оплате", "deposit": "Предоплата",
           "thanks": "Спасибо, что выбрали нас!", "contacts": "Контакты", "bank": "Реквизиты", "night": "ночь", "nights_w": "ночей",
           "paid_full": "Оплачено полностью", "pay_until": "Просим оплатить до заезда", "conf_text": "Ваше бронирование подтверждено. Ждём вас!"},
    "en": {"invoice": "Invoice", "receipt": "Payment receipt", "confirmation": "Booking confirmation",
           "no": "No.", "date": "Date", "guest": "Guest", "phone": "Phone", "apartment": "Apartment", "checkin": "Check-in",
           "checkout": "Check-out", "nights": "Nights", "source": "Source", "desc": "Description", "sum": "Amount",
           "stay": "Accommodation", "total": "Total", "paid": "Paid", "due": "Amount due", "deposit": "Prepayment",
           "thanks": "Thank you for choosing us!", "contacts": "Contacts", "bank": "Bank details", "night": "night", "nights_w": "nights",
           "paid_full": "Paid in full", "pay_until": "Please pay before check-in", "conf_text": "Your booking is confirmed. We look forward to welcoming you!"},
}
DEFAULT_COMPANY = {"name": "Nova Home", "address": "Ташкент", "phone": "", "email": "", "website": "", "inn": "", "bank": "",
                   "currency": "USD", "note": ""}


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


def _next_number(conn, kind: str) -> str:
    year = date.today().year
    prefix = f"{KINDS.get(kind, 'DOC')}-{year}-"
    n = conn.execute("SELECT COUNT(*) FROM crm_documents WHERE number LIKE ?", (prefix + "%",)).fetchone()[0]
    return f"{prefix}{n + 1:04d}"


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
        debt = float(b.get("debt") or 0)
        prepay = float(b.get("prepayment") or 0)
        paid = max(0.0, total - debt) if total else prepay
        if amount is None:
            amount = debt if kind == "invoice" and debt > 0 else (paid if kind == "receipt" else total)
        snap = {"guest": b["guest"], "phone": b["phone"], "email": (client or {}).get("email") or "", "apartment": b["apartment"],
                "checkin": b["checkin"], "checkout": b["checkout"], "nights": b.get("nights") or 0, "source": b["source"],
                "total": total, "paid": paid, "debt": debt, "prepayment": prepay, "arrival_time": b.get("arrival_time"),
                "departure_time": b.get("departure_time"), "notes": b.get("notes") or "", "company": comp}
        number = _next_number(conn, kind)
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
    d["title"] = T.get(d.get("lang") or "ru", T["ru"]).get(d["kind"], d["kind"])
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


def _fonts() -> tuple[str, str]:
    global _fonts_ready
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if not _fonts_ready:
        base = Path(__file__).parent / "fonts"
        try:
            pdfmetrics.registerFont(TTFont("DejaVu", str(base / "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(base / "DejaVuSans-Bold.ttf")))
            _fonts_ready = True
        except Exception:  # noqa: BLE001
            logger.exception("DejaVu fonts not registered; Cyrillic will not render")
            return "Helvetica", "Helvetica-Bold"
    return "DejaVu", "DejaVu-Bold"


def render_pdf(doc: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    f, fb = _fonts()
    t = T.get(doc.get("lang") or "ru", T["ru"])
    d = doc["data"]
    comp = d.get("company") or company()
    cur = doc.get("currency") or "USD"
    st = {
        "h": ParagraphStyle("h", fontName=fb, fontSize=18, leading=22, textColor=colors.HexColor("#1C1C1E")),
        "co": ParagraphStyle("co", fontName=fb, fontSize=13, leading=16, textColor=colors.HexColor("#2563EB")),
        "p": ParagraphStyle("p", fontName=f, fontSize=10, leading=14, textColor=colors.HexColor("#1C1C1E")),
        "m": ParagraphStyle("m", fontName=f, fontSize=9, leading=12, textColor=colors.HexColor("#6B7280")),
        "b": ParagraphStyle("b", fontName=fb, fontSize=10, leading=14),
    }
    buf = BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"{doc['title']} {doc['number']}", author=comp.get("name") or "")
    el = []
    # header: company left, document right
    comp_lines = [x for x in (comp.get("address"), comp.get("phone"), comp.get("email"), comp.get("website")) if x] or [" "]
    head = Table([[Paragraph("<br/>".join(comp_lines), st["p"]),
                   Paragraph(f"<b>{doc['title']}</b><br/>{t['no']} {doc['number']}<br/>{t['date']}: {_dmy(doc['created_at'][:10])}", st["p"])]],
                 colWidths=[100 * mm, 74 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    el += [Paragraph(comp.get("name") or "", st["co"]), Spacer(1, 2 * mm), head, Spacer(1, 6 * mm)]
    # booking details
    nights = d.get("nights") or 0
    rows = [[t["guest"], d.get("guest") or ""], [t["phone"], ("+" + d["phone"]) if d.get("phone") else ""],
            [t["apartment"], d.get("apartment") or ""],
            [t["checkin"], _dmy(d.get("checkin")) + (f" {d['arrival_time']}" if d.get("arrival_time") else "")],
            [t["checkout"], _dmy(d.get("checkout")) + (f" {d['departure_time']}" if d.get("departure_time") else "")],
            [t["nights"], str(nights)], [t["source"], d.get("source") or ""]]
    info = Table([[Paragraph(f"<b>{k}</b>", st["p"]), Paragraph(v, st["p"])] for k, v in rows], colWidths=[45 * mm, 129 * mm])
    info.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#E5E7EB")), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    el += [info, Spacer(1, 7 * mm)]
    # amounts
    total, paid, debt = float(d.get("total") or 0), float(d.get("paid") or 0), float(d.get("debt") or 0)
    per = (total / nights) if nights else 0
    lines = [[t["desc"], t["sum"]],
             [f"{t['stay']}: {d.get('apartment') or ''}, {_dmy(d.get('checkin'))} – {_dmy(d.get('checkout'))}, {nights} {t['nights_w']}" + (f" × {_fmt_money(per, cur)}" if per else ""), _fmt_money(total, cur)]]
    if doc["kind"] == "invoice":
        if paid:
            lines.append([t["paid"], "− " + _fmt_money(paid, cur)])
        lines.append([t["due"], _fmt_money(doc["amount"], cur)])
    elif doc["kind"] == "receipt":
        lines.append([t["paid"], _fmt_money(doc["amount"], cur)])
        if debt > 0:
            lines.append([t["due"], _fmt_money(debt, cur)])
    else:
        if paid:
            lines.append([t["paid"], _fmt_money(paid, cur)])
        if debt > 0:
            lines.append([t["due"], _fmt_money(debt, cur)])
        else:
            lines.append([t["total"], _fmt_money(total, cur)])
    amt = Table([[Paragraph(a, st["b"] if i in (0, len(lines) - 1) else st["p"]), Paragraph(b, st["b"] if i in (0, len(lines) - 1) else st["p"])] for i, (a, b) in enumerate(lines)],
                colWidths=[134 * mm, 40 * mm])
    amt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                             ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#E5E7EB")), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#1C1C1E")),
                             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    el += [amt, Spacer(1, 6 * mm)]
    note = {"invoice": t["pay_until"] if debt > 0 else t["paid_full"], "receipt": t["thanks"], "confirmation": t["conf_text"]}[doc["kind"]]
    el.append(Paragraph(note, st["p"]))
    if comp.get("bank") and doc["kind"] != "confirmation":
        el += [Spacer(1, 4 * mm), Paragraph(f"<b>{t['bank']}</b>", st["p"]), Paragraph((comp["bank"] or "").replace("\n", "<br/>"), st["m"])]
    if comp.get("inn"):
        el.append(Paragraph(comp["inn"], st["m"]))
    if comp.get("note"):
        el += [Spacer(1, 3 * mm), Paragraph((comp["note"] or "").replace("\n", "<br/>"), st["m"])]
    if d.get("notes") and doc["kind"] == "confirmation":
        el += [Spacer(1, 3 * mm), Paragraph(d["notes"], st["m"])]
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
