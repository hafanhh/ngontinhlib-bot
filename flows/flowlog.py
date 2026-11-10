"""Logging helpers shared by /add and /edit, so their log lines look the same.

Every /add or /edit conversation gets ONE short id (like 'k3x9a'). It is printed in square
brackets on every log line of that conversation, so you can find the whole story of one
/add or /edit in logs/bot.log with a single search, for example:   grep k3x9a logs/bot.log

What is logged: the steps taken, lengths and counts, and the outcome. What is NOT logged:
the text you typed (only its length, or the website of a link). The one exception is the
story name in the final "done" line, because writes.log already holds it in full.
"""

import functools
import logging
import time
from urllib.parse import urlparse

import logtools


def begin(update, context, key: str, logger: logging.Logger, **info) -> None:
    """Call at the start of /add or /edit (after the old data was cleared).
    Makes a new log id, remembers it with the conversation, and logs the start."""
    d = context.user_data.setdefault(key, {})
    d["log_id"] = logtools.new_req_id()
    d["started"] = time.monotonic()   # to report how long the whole conversation took
    d["last_step"] = "start"
    user = update.effective_user
    extra = "".join(f" {k}={v}" for k, v in info.items())
    logger.info("/%s started: user=%s%s", key, logtools.mask_user_id(user.id) if user else "unknown", extra)


def tracked(key: str):
    """Decorator for the handlers of a flow. Every button press or message is handled
    separately by the bot, so this puts the conversation's log id back before the handler
    runs. Without it, the lines of the later steps would show '[-]'."""
    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(update, context, *args, **kwargs):
            logtools.set_req_id((context.user_data.get(key) or {}).get("log_id", "-"))
            return await handler(update, context, *args, **kwargs)
        return wrapper
    return decorator


def step(logger: logging.Logger, context, key: str, name: str, text: str = "", *args,
         level: int = logging.INFO) -> None:
    """Log one step, like '/add link: ok host=example.com', and remember it as the last
    step (so a later /cancel can say where the conversation stopped)."""
    d = context.user_data.get(key)
    if d is not None:
        d["last_step"] = name
    logger.log(level, "/%s %s: " + text, key, name, *args)


def last_step(d: dict | None) -> str:
    """The last step logged for this conversation ('?' if there is no conversation)."""
    return (d or {}).get("last_step", "?")


def elapsed(d: dict | None) -> float:
    """Seconds since the conversation started."""
    started = (d or {}).get("started")
    return time.monotonic() - started if started else 0.0


def host_of(url: str) -> str:
    """Only the website of a link (example.com), never the whole link."""
    return urlparse(url).hostname or "?"


def short(text: str, limit: int = 50) -> str:
    """A story name cut to `limit` characters, for the 'done' line."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
