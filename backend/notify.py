"""Send Telegram notifications from the backend (fire-and-forget).

Delivery runs in a background thread so it never slows an API request. Messages
go to every configured target (owner private chats + a team group).
"""
import logging
import threading

import requests

from . import config, database

logger = logging.getLogger("nova.notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, targets=None, topic: str | None = None) -> None:
    """Send `text` to targets. `topic` names the message kind (cleaning /
    attendance / general); in forum groups it selects the thread bound via
    /topic, private chats ignore it."""
    token = config.BOT_TOKEN
    ids = targets if targets is not None else config.notify_targets()
    if not token or not ids:
        return

    def _run():
        url = _API.format(token=token)
        for chat_id in ids:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
                # night messages arrive silently (see QUIET_FROM/QUIET_TO)
                "disable_notification": config.quiet_now(),
            }
            if chat_id < 0:  # groups only — private chats have no topics
                try:
                    thread = database.get_topic(chat_id, topic or "general")
                except Exception:  # noqa: BLE001
                    thread = None
                if thread:
                    payload["message_thread_id"] = thread
            try:
                requests.post(url, json=payload, timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning("notify to %s failed: %s", chat_id, exc)

    threading.Thread(target=_run, daemon=True).start()
