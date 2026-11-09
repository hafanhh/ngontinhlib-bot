"""The request id (req_id): a short code that belongs to ONE user question.

A "context variable" is like a sticky note that follows the question around (also into the
worker thread that talks to Gemini and Notion), so we never pass req_id by hand.
"-" is shown for lines that are not part of a question (startup, /add, /edit...)."""

import contextvars
import logging
import secrets
import string

_req_id = contextvars.ContextVar("req_id", default="-")
_ID_CHARS = string.ascii_lowercase + string.digits


def new_req_id() -> str:
    """Make a new 5-character id (like 'k3x9a'), make it the current one, and return it."""
    value = "".join(secrets.choice(_ID_CHARS) for _ in range(5))
    _req_id.set(value)
    return value


def clear_req_id() -> None:
    """Forget the current id (lines go back to showing '-')."""
    _req_id.set("-")


def get_req_id() -> str:
    return _req_id.get()


def mask_user_id(user_id) -> str:
    """Hide most of a Telegram user id: 123456789 -> '1234***'."""
    return f"{str(user_id)[:4]}***"


class ReqIdFilter(logging.Filter):
    """Copies the current req_id onto every log record, so the log format can print it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.req_id = _req_id.get()
        return True
