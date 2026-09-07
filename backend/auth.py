"""Telegram Mini App authentication.

Validates the ``initData`` string that Telegram passes to a Mini App by checking
its HMAC signature against the bot token, then derives the user's role.

Rules:
- If the bot token or owner list is not configured, everything is treated as
  owner (local dev / plain browser use — the app is fully usable out of the box).
- When configured (real Telegram use) we apply least privilege: ONLY a valid
  signature whose user id is in the owner list unlocks owner access. Everyone
  else — a valid signature for a non-owner, an invalid signature, or missing
  initData (opened via a shared link, an old cached build, etc.) — is staff.
  This guarantees the owner-only "Касса"/"Гости" screens never leak to staff.
"""
import hashlib
import hmac
import json
import logging
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from . import config

logger = logging.getLogger("nova.auth")


def parse_init_data(raw: str):
    """Return the user dict if the signature is valid, else None."""
    if not raw or not config.BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(raw, keep_blank_values=True))
    except Exception:  # noqa: BLE001
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    raw_user = pairs.get("user")
    if not raw_user:
        return {}
    try:
        return json.loads(raw_user)
    except (ValueError, TypeError):
        return {}


def key_ok(key: str) -> bool:
    """Owner access key fallback for Telegram clients that pass no initData
    (native macOS app). The bot hands the key only to owner ids, so presenting
    it proves ownership."""
    return bool(config.OWNER_KEY) and bool(key) and hmac.compare_digest(key, config.OWNER_KEY)


def role_from_init(raw: str, owner_key: str = "") -> str:
    # Not fully configured → dev mode, full access.
    if not config.BOT_TOKEN or not config.OWNER_TELEGRAM_IDS:
        return "owner"
    if key_ok(owner_key):
        logger.info("auth: owner key OK -> owner")
        return "owner"
    # Configured for real Telegram use → least privilege: only a valid signature
    # from a known owner unlocks owner. Missing/invalid initData → staff.
    if not raw:
        logger.info("auth: initData EMPTY -> staff")
        return "staff"
    user = parse_init_data(raw)
    if user is None:
        logger.info("auth: initData INVALID signature (len=%d) -> staff", len(raw))
        return "staff"
    uid = user.get("id")
    role = "owner" if uid in config.OWNER_TELEGRAM_IDS else "staff"
    logger.info("auth: uid=%s -> %s", uid, role)
    return role


def is_owner(raw: str, owner_key: str = "") -> bool:
    return role_from_init(raw, owner_key) == "owner"


# FastAPI dependency for owner-only endpoints (finance, guests, prices).
async def owner_guard(
    x_telegram_init_data: str = Header(default=""),  # noqa: B008
    x_owner_key: str = Header(default=""),  # noqa: B008
):
    if not is_owner(x_telegram_init_data, x_owner_key):
        raise HTTPException(status_code=403, detail="Только для владельца")


def pay_viewer_ids() -> set[int]:
    """PAY_VIEWERS resolved to Telegram ids: numeric entries directly,
    @usernames through the bot's staff registry."""
    from . import database
    ids: set[int] = set()
    unames: set[str] = set()
    for tok in config.PAY_VIEWERS:
        if tok.isdigit():
            ids.add(int(tok))
        else:
            unames.add(tok.lstrip("@").lower())
    if unames:
        try:
            for r in database.all_staff(active_only=False):
                if (r.get("username") or "").lower() in unames:
                    ids.add(r["staff_id"])
        except Exception:  # noqa: BLE001
            logger.exception("pay viewer resolve failed")
    return ids


def can_payments(raw: str, owner_key: str = "") -> bool:
    """The 'Оплаты' block: owners always; PAY_VIEWERS by Telegram signature or
    by the viewer key the bot hands them (macOS passes no initData)."""
    if is_owner(raw, owner_key):
        return True
    if config.PAY_KEY and owner_key and hmac.compare_digest(owner_key, config.PAY_KEY):
        logger.info("auth: pay key OK -> pay viewer")
        return True
    user = parse_init_data(raw)
    if not user:
        return False
    uid = user.get("id")
    ok = uid in pay_viewer_ids()
    if ok:
        logger.info("auth: uid=%s -> pay viewer", uid)
    return ok


# FastAPI dependency for the payments reconciliation endpoints.
async def pay_guard(
    x_telegram_init_data: str = Header(default=""),  # noqa: B008
    x_owner_key: str = Header(default=""),  # noqa: B008
):
    if not can_payments(x_telegram_init_data, x_owner_key):
        raise HTTPException(status_code=403, detail="Нет доступа к оплатам")
