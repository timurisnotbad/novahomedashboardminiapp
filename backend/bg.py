"""One bounded pool for all background work (notifications, webhooks, pushes
to the calendar). A thread per event used to pile up hundreds of threads on a
burst (bridge reconnect, Wazzup retries) and fight for the SQLite write lock."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("nova.bg")

_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="nova-bg")
_inflight: set = set()
_lock = threading.Lock()


def run(fn, *args, key=None, **kwargs) -> bool:
    """Queue fn(*args, **kwargs). With `key`, a job with the same key that is
    still queued or running is not added again (returns False)."""
    if key is not None:
        with _lock:
            if key in _inflight:
                return False
            _inflight.add(key)

    def wrapped():
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("background job %s failed", getattr(fn, "__name__", fn))
        finally:
            if key is not None:
                with _lock:
                    _inflight.discard(key)

    _pool.submit(wrapped)
    return True


def pending() -> int:
    return _pool._work_queue.qsize()  # noqa: SLF001 — for the health endpoint
