"""Bridge to the Google Sheet via its Apps Script web app.

Phase 1 is read-only: it fetches the cash balance per apartment and the grand
total from the "Счета" sheet. Results are cached briefly in memory so the UI
stays snappy and we stay well within Apps Script's daily quota.
"""
import time

import requests

from . import config

_cache = {"at": 0.0, "data": None}


def _empty(reason: str) -> dict:
    return {"ok": False, "configured": config.FINANCE_ENABLED,
            "error": reason, "apartments": [], "total_usd": None}


def fetch_balances(force: bool = False) -> dict:
    """Return {ok, sheet, total_usd, apartments:[{code,number,usd}], ...}."""
    if not config.FINANCE_ENABLED:
        return _empty("not_configured")

    now = time.time()
    cached = _cache["data"]
    if not force and cached is not None and now - _cache["at"] < config.FINANCE_CACHE_TTL:
        return cached

    resp = requests.get(
        config.SHEET_API_URL,
        params={"action": "balances", "token": config.SHEET_API_TOKEN},
        timeout=30,
        allow_redirects=True,  # Apps Script /exec redirects to googleusercontent.com
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:  # got HTML (auth page / error) instead of JSON
        raise RuntimeError("sheet did not return JSON — check web app access is 'Anyone'") from exc

    data["configured"] = True
    _cache["data"] = data
    _cache["at"] = now
    return data


def invalidate() -> None:
    _cache["data"] = None
    _cache["at"] = 0.0
