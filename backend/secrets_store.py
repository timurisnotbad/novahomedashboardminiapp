"""Independent secrets for this installation.

Until v73 every key in the system (CRM session cookies, inbox links, media
signatures, the Booking.com extension token, the Wazzup webhook path) was
derived from BOT_TOKEN — and from the constant «dev-inbox» when the bot was
not configured. Now a random root is generated once and kept in
data/secrets.json (0600); env variables still win when set explicitly."""
import json
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger("nova.secrets")

KEYS = ("app_secret", "inbox_secret")


def load(data_dir: Path) -> dict:
    path = data_dir / "secrets.json"
    data: dict = {}
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:  # noqa: BLE001
        logger.exception("secrets.json unreadable — regenerating")
        data = {}
    changed = False
    for k in KEYS:
        if not isinstance(data.get(k), str) or len(data[k]) < 32:
            data[k] = secrets.token_hex(32)
            changed = True
    if changed:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
            logger.info("secrets.json written (%s)", path)
        except Exception:  # noqa: BLE001
            logger.exception("cannot write %s — secrets will change on every start", path)
    return data
