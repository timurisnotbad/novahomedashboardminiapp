"""File logging for both processes.

A console window that closes takes its traceback with it, so everything the
bot and the server print is also written to logs/<name>.log. Send those files
when something breaks.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def setup(name: str) -> Path:
    """Attach a rotating UTF-8 file handler to the root logger. Returns the path."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
    except Exception:  # noqa: BLE001 — read-only folder: keep the console only
        return LOG_DIR / f"{name}.log"
    path = LOG_DIR / f"{name}.log"
    root = logging.getLogger()
    if any(getattr(h, "_nova_tag", None) == name for h in root.handlers):
        return path
    try:
        h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return path
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h._nova_tag = name  # noqa: SLF001
    root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return path


def log_crash(name: str, exc: BaseException) -> Path:
    """Write a crash with its traceback to logs/<name>-error.log and return it."""
    import traceback
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"{name}-error.log"
        with path.open("a", encoding="utf-8") as fh:
            from datetime import datetime
            fh.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
        return path
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return LOG_DIR / f"{name}-error.log"
