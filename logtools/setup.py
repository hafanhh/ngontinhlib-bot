"""Switches logging on: where logs go, how they look and how detailed they are.

  1. Terminal AND the file logs/bot.log (about 1 MB per file, 3 old copies kept).
  2. LOG_LEVEL from .env (DEBUG, INFO, WARNING, ERROR; default INFO).
  3. Every line: time, level, [req_id], message (see context.py).
  4. Secrets are hidden in every line (see redact.py)."""

import logging
import os
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from logtools import paths
from logtools.context import ReqIdFilter
from logtools.redact import RedactingFormatter

# Format of every line: time, level, req_id, message
LOG_FORMAT = "%(asctime)s %(levelname)s [%(req_id)s] %(message)s"


def read_level() -> int:
    """LOG_LEVEL from .env as a number. A missing or invalid value means INFO."""
    name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = logging.getLevelName(name)  # gives a number for a valid name, text for an invalid one
    return level if isinstance(level, int) else logging.INFO


def setup_logging(log_dir: str = None) -> str:
    """Switch logging on and return the path of the log file.
    log_dir is only for tests (a temporary folder); the bot uses the default logs/ folder.
    Safe to call more than once (old handlers are replaced)."""
    load_dotenv()  # so LOG_LEVEL and the secrets can be read from .env
    log_dir = log_dir or paths.LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, os.path.basename(paths.BOT_LOG))

    formatter = RedactingFormatter(LOG_FORMAT)
    req_filter = ReqIdFilter()

    # Handler 1: the terminal. Handler 2: the file, capped at ~1 MB with 3 older copies.
    terminal = logging.StreamHandler()
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    for handler in (terminal, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(req_filter)  # on the handlers, so lines from every library get a req_id

    root = logging.getLogger()
    for old in list(root.handlers):  # replace handlers from an earlier call
        root.removeHandler(old)
        old.close()
    root.addHandler(terminal)
    root.addHandler(file_handler)
    root.setLevel(read_level())

    # These libraries log every request URL, and Telegram URLs contain the bot token.
    # Keep them quiet whatever LOG_LEVEL is. (httpx2/httpcore2 are the ones used for Gemini.)
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return log_file
