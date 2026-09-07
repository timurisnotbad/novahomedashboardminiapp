"""Booking.com competitor price monitor for Nova Home (Nest One, Tashkent).

Scrapes our listing + competitor listings for a date range with a headless
browser and returns structured prices. Booking.com blocks plain HTTP requests,
so a real browser (Playwright + Chromium) is required.

Playwright is imported lazily, so the rest of the app keeps working even when it
is not installed. On the server, install once:

    pip install playwright
    playwright install chromium
"""
import logging
import re
import time
from datetime import date

logger = logging.getLogger("nova.prices")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

OUR_NAME = "Nova Home (наши)"
OUR_URL = "https://www.booking.com/hotel/uz/nova-home-apartment-nest-one.ru.html"

# data-block-id (first 10 digits) -> apartment code
ROOM_MAP = {
    "1584004901": "A-066", "1584004902": "A-067", "1584004904": "A-210",
    "1584004905": "B-008", "1584004906": "B-110", "1584004907": "A-207",
    "1584004908": "A-120", "1584004909": "B-071", "1584004910": "B-007",
    "1584004912": "B-031", "1584004914": "B-051", "1584004915": "B-078",
    "1584004916": "B-103", "1584004917": "B-005", "1584004918": "B-135",
    "1584004919": "A-278", "1584004920": "B-073",
}

COMPETITORS = [
    {"name": "Арсен (Sky High)",
     "url": "https://www.booking.com/hotel/uz/sky-high-luxury-apt-nest-one-panoramic-city-views.ru.html"},
    {"name": "Неизвестный (терраса)",
     "url": "https://www.booking.com/hotel/uz/nest-one-apartment-with-a-terrace.ru.html"},
    {"name": "Студия Nest One",
     "url": "https://www.booking.com/hotel/uz/nest-one-studio.ru.html"},
]

# in-memory cache: key "checkin|checkout" -> (epoch, data)
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 30 * 60  # 30 minutes


def _url(base: str, checkin: date, checkout: date) -> str:
    sep = "&" if "?" in base else "?"
    return (
        f"{base}{sep}checkin={checkin.isoformat()}&checkout={checkout.isoformat()}"
        f"&group_adults=2&no_rooms=1&lang=ru&selected_currency=USD"
    )


def _clean_price(text: str) -> str | None:
    if not text:
        return None
    t = text.replace("\xa0", " ").strip()
    m = re.search(r"US\$\s?([\d\s.,]+)", t)
    if m:
        num = re.sub(r"[^\d]", "", m.group(1))
        return f"US${num}" if num else None
    m = re.search(r"([\d][\d\s.,]{1,})\s*(USD|UZS|US\$)", t)
    if m:
        num = re.sub(r"[^\d]", "", m.group(1))
        cur = "US$" if "US" in m.group(2) else m.group(2)
        return f"{cur}{num}" if num else None
    return None


def _parse(page, is_ours: bool) -> list[dict]:
    rows: list[dict] = []

    # Strategy 1 — room-type table
    seen = set()
    for tr in page.query_selector_all("tr[data-block-id]"):
        bid = (tr.get_attribute("data-block-id") or "")[:10]
        if not bid or bid in seen:
            continue
        name_el = tr.query_selector(".hprt-roomtype-icon-link")
        raw_name = name_el.inner_text().strip() if name_el else ""
        name = ROOM_MAP.get(bid, raw_name or bid) if is_ours else (raw_name or bid)
        price_el = tr.query_selector(".bui-price-display__value, .prco-valign-middle-helper")
        price = _clean_price(price_el.inner_text()) if price_el else None
        if name and price:
            rows.append({"name": name, "price": price})
            seen.add(bid)
    if rows:
        return rows

    # Strategy 2 — unit cards
    for card in page.query_selector_all('[data-testid="property-unit-item"]'):
        h = card.query_selector("h3, h4")
        name = h.inner_text().strip() if h else ""
        pe = card.query_selector('[data-testid="price-and-discounted-price"], [class*="price"]')
        price = _clean_price(pe.inner_text()) if pe else None
        if name and price:
            rows.append({"name": name, "price": price})
    if rows:
        return rows

    # Strategy 3 — raw text scan (last resort). Adjacent numbers on the page
    # easily glue together ("US$124 14" -> 12414), so match bounded 2-4 digit
    # amounts only, keep plausible nightly rates and report just the minimum.
    try:
        body = page.inner_text("body")
    except Exception:  # noqa: BLE001
        body = ""
    vals = {int(m) for m in re.findall(r"US\$\s?(\d{2,4})(?!\d)", body)}
    vals = {v for v in vals if 20 <= v <= 2000}
    if vals:
        rows.append({"name": "минимальная цена на странице", "price": f"US${min(vals)}"})
    return rows


def _scrape_one(page, base: str, checkin: date, checkout: date, is_ours: bool) -> list[dict]:
    url = _url(base, checkin, checkout)
    last_exc = None
    for _ in range(2):  # 2 attempts
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            time.sleep(3.5)  # let Booking's JS render prices
            return _parse(page, is_ours)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(3)
    raise last_exc if last_exc else RuntimeError("scrape failed")


def scrape_prices(checkin: date, checkout: date, use_cache: bool = True) -> dict:
    """Return {ok, error, checkin, checkout, nights, ours[], competitors[]}."""
    key = f"{checkin.isoformat()}|{checkout.isoformat()}"
    if use_cache and key in _CACHE:
        ts, cached = _CACHE[key]
        if time.time() - ts < _CACHE_TTL:
            return {**cached, "cached": True}

    nights = max(1, (checkout - checkin).days)
    result = {
        "ok": True, "error": None,
        "checkin": checkin.isoformat(), "checkout": checkout.isoformat(),
        "nights": nights, "ours": [], "competitors": [], "cached": False,
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {**result, "ok": False, "error": "playwright_not_installed"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_UA, locale="ru-RU")
            page = ctx.new_page()

            try:
                ours = _scrape_one(page, OUR_URL, checkin, checkout, is_ours=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ours scrape failed: %s", exc)
                ours = []
            # sort our apartments alphabetically by code
            result["ours"] = sorted(ours, key=lambda r: r["name"])

            for comp in COMPETITORS:
                try:
                    rooms = _scrape_one(page, comp["url"], checkin, checkout, is_ours=False)
                    result["competitors"].append(
                        {"name": comp["name"], "rooms": rooms, "error": False}
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("competitor %s failed: %s", comp["name"], exc)
                    result["competitors"].append(
                        {"name": comp["name"], "rooms": [], "error": True}
                    )
            browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("scrape_prices failed")
        return {**result, "ok": False, "error": str(exc)}

    _annotate(result)
    _CACHE[key] = (time.time(), result)
    return result


def _num(price: str):
    """Numeric USD value from a price string, or None (non-USD is not comparable)."""
    if not price or not price.startswith("US$"):
        return None
    m = re.search(r"\d+", price)
    return int(m.group()) if m else None


def _annotate(result: dict) -> dict:
    """Compare each of our apartments against the cheapest competitor room.

    Adds to every "ours" row: num (int price), cmp ("low"/"mid"/"high" vs the
    cheapest competitor, ±2% treated as parity) and delta (US$ difference).
    Adds to the result: comp_min, our_avg, our_below, our_count."""
    comp_prices = []
    for c in result.get("competitors", []):
        if c.get("error"):
            continue
        for r in c.get("rooms", []):
            v = _num(r.get("price"))
            if v:
                comp_prices.append(v)
    comp_min = min(comp_prices) if comp_prices else None
    result["comp_min"] = comp_min

    our_vals, below = [], 0
    for r in result.get("ours", []):
        v = _num(r.get("price"))
        r["num"] = v
        if v and comp_min:
            r["delta"] = v - comp_min
            if v <= comp_min * 0.98:
                r["cmp"] = "low"; below += 1
            elif v >= comp_min * 1.02:
                r["cmp"] = "high"
            else:
                r["cmp"] = "mid"
        else:
            r["delta"] = None
            r["cmp"] = None
        if v:
            our_vals.append(v)
    result["our_avg"] = round(sum(our_vals) / len(our_vals)) if our_vals else None
    result["our_below"] = below
    result["our_count"] = len(our_vals)
    return result


# --------------------------------------------------------------------------
# Telegram (HTML) report
# --------------------------------------------------------------------------
def _nights_word(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} ночь"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} ночи"
    return f"{n} ночей"


def _fmt_dm(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return d.strftime("%d.%m")
    except (ValueError, TypeError):
        return iso


def format_report_html(data: dict) -> str:
    if not data.get("ok"):
        if data.get("error") == "playwright_not_installed":
            return ("⚠️ Модуль цен не установлен на сервере.\n"
                    "Выполните: <code>pip install playwright</code> и "
                    "<code>playwright install chromium</code>")
        return f"❌ Не удалось получить цены Booking.com\n{data.get('error') or ''}"

    head = (f"📊 <b>Цены Booking.com — {_fmt_dm(data['checkin'])} → "
            f"{_fmt_dm(data['checkout'])}.{date.today().year} "
            f"({_nights_word(data['nights'])})</b>")
    lines = [head, "─" * 24]
    if data.get("comp_min"):
        below, total = data.get("our_below", 0), data.get("our_count", 0)
        lines.append(
            f"Самый дешёвый конкурент: <b>US${data['comp_min']}</b> · "
            f"наших дешевле него: <b>{below} из {total}</b>"
        )
        lines.append("🟢 дешевле конкурентов · 🟡 наравне · 🔴 дороже")
        lines.append("")
    lines.append("🏠 <b>Nova Home (наши)</b>")
    _MARK = {"low": "🟢", "mid": "🟡", "high": "🔴"}
    if data["ours"]:
        for r in data["ours"]:
            mark = _MARK.get(r.get("cmp"), "")
            delta = r.get("delta")
            tail = ""
            if delta is not None and r.get("cmp") == "high":
                tail = f"  (+{delta}$)"
            elif delta is not None and r.get("cmp") == "low":
                tail = f"  (−{abs(delta)}$)"
            lines.append(f"  {mark} {r['name']}  →  {r['price']}{tail}".rstrip())
    else:
        lines.append("  Цены не найдены")

    for comp in data["competitors"]:
        lines.append("")
        lines.append(f"🏢 <b>{comp['name']}</b>")
        if comp.get("error"):
            lines.append("  нет данных (ошибка загрузки)")
        elif comp["rooms"]:
            for r in comp["rooms"]:
                lines.append(f"  {r['name']}  →  {r['price']}")
        else:
            lines.append("  нет доступных номеров")
    return "\n".join(lines)
