"""Send Telegram notifications from the backend (fire-and-forget).

Delivery runs in a background thread so it never slows an API request. Messages
go to every configured target (owner private chats + a team group). Long texts
are split at Telegram's 4096-character limit; rate limits (429) and transient
network errors are retried.

`send_replacing` keeps ONE message per kind and chat (the evening cleaning
plan): a newer version edits the earlier message in place, or — when editing is
impossible — deletes it and posts a fresh one. The chat stays tidy instead of
collecting a "🔄 График изменился" message every 15 minutes.
"""
import hashlib
import logging
import threading
import time
from datetime import datetime, timedelta

import requests

from . import config, database

logger = logging.getLogger("nova.notify")

_API = "https://api.telegram.org/bot{token}/{method}"
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


def _call(method: str, payload: dict):
    """One Bot API call with up to 3 attempts; honours Telegram's retry_after.
    Returns the `result` object on success, None on failure (the description is
    logged; `_last_error` carries it for callers that need to branch)."""
    global _last_error
    _last_error = ""
    url = _API.format(token=config.BOT_TOKEN, method=method)
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
        except requests.RequestException as exc:
            logger.warning("%s to %s failed (%s), attempt %d", method, payload.get("chat_id"), exc, attempt + 1)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 200:
            try:
                return resp.json().get("result", True)
            except ValueError:
                return True
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 3))
            except Exception:  # noqa: BLE001
                wait = 3
            logger.warning("%s rate-limited for %s: waiting %ss", method, payload.get("chat_id"), wait)
            time.sleep(min(wait, 30))
            continue
        try:
            _last_error = str(resp.json().get("description", ""))
        except ValueError:
            _last_error = resp.text[:200]
        logger.warning("%s to %s rejected: %s %s", method, payload.get("chat_id"),
                       resp.status_code, _last_error[:200])
        return None  # 400/403 (blocked bot, bad chat) — retrying won't help
    return None


_last_error = ""


def _thread_for(chat_id: int, topic: str | None):
    if chat_id >= 0:  # private chats have no topics
        return None
    try:
        return database.get_topic(chat_id, topic or "general")
    except Exception:  # noqa: BLE001
        return None


def _deliver(chat_id: int, chunks: list[str], thread) -> int | None:
    """Send the chunks to one chat; returns the first message id (or None)."""
    first = None
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
        res = _call("sendMessage", payload)
        if res is None:
            break  # don't spray the remaining chunks at a dead chat
        if first is None and isinstance(res, dict):
            first = res.get("message_id")
    return first


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
        for chat_id in ids:
            _deliver(chat_id, chunks, _thread_for(chat_id, topic))

    threading.Thread(target=_run, daemon=True).start()


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def send_replacing(key: str, text: str, targets=None, topic: str | None = None,
                   dedupe: str | None = None) -> None:
    """Deliver `text` as THE message for `key` in each chat.

    * the same content (by `dedupe`, default the text itself) as last time →
      nothing is sent at all;
    * an earlier message exists → it is edited in place;
    * editing fails (deleted by an admin, too old, too long) → the old message
      is removed and a new one posted.
    """
    token = config.BOT_TOKEN
    ids = targets if targets is not None else config.notify_targets()
    if not token or not ids or not text:
        return
    chunks = split_text(text)
    digest = _hash(dedupe if dedupe is not None else text)

    def _one(chat_id: int) -> None:
        try:
            prev = database.get_bot_message(key, chat_id)
        except Exception:  # noqa: BLE001
            prev = None
        if prev and prev.get("text_hash") == digest:
            logger.info("replacing %s for %s: unchanged, skipped", key, chat_id)
            return
        thread = _thread_for(chat_id, topic)
        if prev and prev.get("message_id") and len(chunks) == 1:
            res = _call("editMessageText", {
                "chat_id": chat_id, "message_id": prev["message_id"],
                "text": chunks[0], "disable_web_page_preview": True,
            })
            if res is not None or "not modified" in _last_error.lower():
                try:
                    database.set_bot_message(key, chat_id, prev["message_id"], digest)
                except Exception:  # noqa: BLE001
                    logger.exception("set_bot_message failed")
                logger.info("replacing %s for %s: edited", key, chat_id)
                return
        if prev and prev.get("message_id"):
            _call("deleteMessage", {"chat_id": chat_id, "message_id": prev["message_id"]})
        mid = _deliver(chat_id, chunks, thread)
        if mid:
            try:
                database.set_bot_message(key, chat_id, mid, digest)
            except Exception:  # noqa: BLE001
                logger.exception("set_bot_message failed")

    def _run():
        for chat_id in ids:
            try:
                _one(chat_id)
            except Exception:  # noqa: BLE001
                logger.exception("send_replacing %s to %s failed", key, chat_id)
        try:  # keys are per day; rows older than a week are dead weight
            database.prune_bot_messages((datetime.now() - timedelta(days=7)).isoformat(timespec="seconds"))
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, daemon=True).start()
