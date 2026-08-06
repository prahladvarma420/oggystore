"""Single global lane for all Playwright/Chromium work.

Only one browser-automation job (M365 invite, M365 keepalive, Canva invite)
may run at a time. A headless Chromium needs hundreds of MB of memory and
dozens of OS tasks; launching two at once on a small container is exactly
what exhausted the process/thread budget and crashed the bot after 4 days
of uptime (RuntimeError: can't start new thread).

This is a non-blocking lock, not a queue, on purpose: when the lane is busy
the caller must skip or park its job. It must never stack another worker
thread behind one that a hard timeout already abandoned -- abandoned
``asyncio.to_thread`` workers keep running in the background, and stacking
new ones behind them is how the leak compounded.
"""
import threading
import time

_lock = threading.Lock()
_busy_since = [0.0]
_busy_label = [""]


def acquire(label: str, wait_seconds: float = 0) -> bool:
    """Try to take the browser lane. Returns False if it stays busy.

    wait_seconds > 0 waits that long for the lane to free up (bounded), so a
    customer invite arriving during a short keepalive is not bounced.
    """
    if wait_seconds > 0:
        ok = _lock.acquire(timeout=wait_seconds)
    else:
        ok = _lock.acquire(blocking=False)
    if ok:
        _busy_since[0] = time.time()
        _busy_label[0] = str(label)
    return ok


def release() -> None:
    _busy_since[0] = 0.0
    _busy_label[0] = ""
    try:
        _lock.release()
    except RuntimeError:
        pass


def busy_info() -> str:
    """Human-readable state for log lines."""
    if _busy_since[0]:
        return f"{_busy_label[0]} (running {int(time.time() - _busy_since[0])}s)"
    return "idle"
