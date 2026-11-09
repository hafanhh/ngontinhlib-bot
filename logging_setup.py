"""Central logging setup: the ONE place that decides where logs go and how they look.

What it does:
  1. Writes logs to the terminal AND to logs/bot.log (about 1 MB per file, 3 old copies kept).
  2. Reads LOG_LEVEL from .env (DEBUG, INFO, WARNING, ERROR; default INFO).
  3. Puts a short request id (req_id) in every log line of one user question.
  4. Hides secrets (tokens, API keys) from every line, even inside error tracebacks.

Use it like this (bot.py does it once at startup):
    import logging_setup
    logging_setup.setup_logging()
"""

import contextvars
import logging
import os
import re
import secrets
import string
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

# The log files live in the logs/ folder next to this file (git-ignored)
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

# Format of every line: time, level, req_id, message
LOG_FORMAT = "%(asctime)s %(levelname)s [%(req_id)s] %(message)s"

# ---------------------------------------------------------------------------------
# req_id: a short code that belongs to ONE question.
# A "context variable" is like a sticky note that follows the question around (also into
# the worker thread that talks to Gemini and Notion), so we never pass req_id by hand.
# "-" is shown for lines that are not part of a question (startup, /add, /edit...).
# ---------------------------------------------------------------------------------
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


class _ReqIdFilter(logging.Filter):
    """Copies the current req_id onto every log record, so the format can print it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.req_id = _req_id.get()
        return True


# ---------------------------------------------------------------------------------
# Secret hiding
# ---------------------------------------------------------------------------------
# Secrets whose real value we hide wherever it appears (read from .env at startup)
_SECRET_ENV_KEYS = ("BOT_TOKEN", "NOTION_TOKEN", "GEMINI_API_KEY")

# Shapes of secrets we hide even when we do not know the value
_SECRET_PATTERNS = [
    (re.compile(r"bot\d+:[\w-]+"), "bot***"),                       # Telegram URL: .../bot123:ABC/getUpdates
    (re.compile(r"\b(?:secret|ntn)_[A-Za-z0-9]{10,}"), "***"),      # Notion tokens
    (re.compile(r"AIza[\w-]{20,}"), "***"),                         # Google / Gemini keys
    (re.compile(r"(?i)([?&](?:key|api_key|token)=)[^&\s'\"]+"), r"\1***"),  # ?key=... in a URL
    (re.compile(r"(?i)(X-Amz-[A-Za-z-]+=)[^&\s'\"]+"), r"\1***"),   # signed Notion file links
]


class _RedactingFormatter(logging.Formatter):
    """Builds the normal log line (including any traceback), then hides secrets in it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only values that are long enough to be real secrets (avoids hiding common words)
        self._secrets = [v for v in (os.getenv(k) for k in _SECRET_ENV_KEYS) if v and len(v) >= 8]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for value in self._secrets:
            text = text.replace(value, "***")
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def _read_level() -> int:
    """LOG_LEVEL from .env as a number. A missing or invalid value means INFO."""
    name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = logging.getLevelName(name)  # gives a number for a valid name, text for an invalid one
    return level if isinstance(level, int) else logging.INFO


def setup_logging() -> None:
    """Switch logging on. Safe to call more than once (old handlers are replaced)."""
    load_dotenv()  # so LOG_LEVEL and the secrets can be read from .env
    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = _RedactingFormatter(LOG_FORMAT)
    req_filter = _ReqIdFilter()

    # Handler 1: the terminal. Handler 2: the file, capped at ~1 MB with 3 older copies.
    terminal = logging.StreamHandler()
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    for handler in (terminal, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(req_filter)  # on the handlers, so lines from every library get a req_id

    root = logging.getLogger()
    for old in list(root.handlers):  # replace handlers from an earlier call
        root.removeHandler(old)
        old.close()
    root.addHandler(terminal)
    root.addHandler(file_handler)
    root.setLevel(_read_level())

    # These libraries log every request URL, and Telegram URLs contain the bot token.
    # Keep them quiet whatever LOG_LEVEL is. (httpx2/httpcore2 are the ones used for Gemini.)
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
