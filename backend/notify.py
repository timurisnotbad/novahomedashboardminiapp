"""Send Telegram notifications from the backend (fire-and-forget).

Delivery runs in a background thread so it never slows an API request. Messages
go to every configured target (owner private chats + a team group). Long texts
are split at Telegram's 4096-character limit; rate limits (429) and transient
network errors are retried.
"""
import logging
import threading
import time

import requests

from . import config, database

logger = logging.getLogger("nova.notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_LIMIT = 4000


def split_text(text: str, limit: int = _LIMIT) -> list[str]:
    """Split on line breaks so a chunk never cuts a line in half."""
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts or [""]


def _post(url: str, payload: dict) -> bool:
    """One message with up to 3 attempts; honours Telegram's retry_after."""
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
        except requests.RequestException as exc:
            logger.warning("notify to %s failed (%s), attempt %d", payload.get("chat_id"), exc, attempt + 1)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 3))
            except Exception:  # noqa: BLE001
                wait = 3
            logger.warning("notify rate-limited for %s: waiting %ss", payload.get("chat_id"), wait)
            time.sleep(min(wait, 30))
            continue
        logger.warning("notify to %s rejected: %s %s", payload.get("chat_id"),
                       resp.status_code, resp.text[:200])
        return False  # 400/403 (blocked bot, bad chat) — retrying won't help
    return False


def send(text: str, targets=None, topic: str | None = None) -> None:
    """Send `text` to targets. `topic` names the message kind (cleaning /
    attendance / general); in forum groups it selects the thread bound via
    /topic, private chats ignore it."""
    token = config.BOT_TOKEN
    ids = targets if targets is not None else config.notify_targets()
    if not token or not ids or not text:
        return
    chunks = split_text(text)

    def _run():
        url = _API.format(token=token)
        for chat_id in ids:
            thread = None
            if chat_id < 0:  # groups only — private chats have no topics
                try:
                    thread = database.get_topic(chat_id, topic or "general")
                except Exception:  # noqa: BLE001
                    thread = None
            for chunk in chunks:
                payload = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                    # night messages arrive silently (see QUIET_FROM/QUIET_TO)
                    "disable_notification": config.quiet_now(),
                }
                if thread:
                    payload["message_thread_id"] = thread
                if not _post(url, payload):
                    break  # don't spray the remaining chunks at a dead chat

    threading.Thread(target=_run, daemon=True).start()
