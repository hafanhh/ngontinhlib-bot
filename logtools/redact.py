"""Hides secrets in log lines. Whatever is logged (a message, an error, a traceback), the
real BOT_TOKEN, NOTION_TOKEN and GEMINI_API_KEY and anything shaped like a token never
reaches the terminal or the file."""

import logging
import os
import re

# Secrets whose real value we hide wherever it appears (read from .env at startup)
SECRET_ENV_KEYS = ("BOT_TOKEN", "NOTION_TOKEN", "GEMINI_API_KEY")

# Shapes of secrets we hide even when we do not know the value
SECRET_PATTERNS = [
    (re.compile(r"bot\d+:[\w-]+"), "bot***"),                       # Telegram URL: .../bot123:ABC/getUpdates
    (re.compile(r"\b(?:secret|ntn)_[A-Za-z0-9]{10,}"), "***"),      # Notion tokens
    (re.compile(r"AIza[\w-]{20,}"), "***"),                         # Google / Gemini keys
    (re.compile(r"(?i)([?&](?:key|api_key|token)=)[^&\s'\"]+"), r"\1***"),  # ?key=... in a URL
    (re.compile(r"(?i)(X-Amz-[A-Za-z-]+=)[^&\s'\"]+"), r"\1***"),   # signed Notion file links
]


class RedactingFormatter(logging.Formatter):
    """Builds the normal log line (including any traceback), then hides secrets in it."""

    def __init__(self, *args, **kwargs):
        """Collect the real secret values from the environment once, when the logger is set up."""
        super().__init__(*args, **kwargs)
        # Only values that are long enough to be real secrets (avoids hiding common words)
        self._secrets = [v for v in (os.getenv(k) for k in SECRET_ENV_KEYS) if v and len(v) >= 8]

    def format(self, record: logging.LogRecord) -> str:
        """Return the formatted log line with every secret replaced by '***'."""
        text = super().format(record)
        for value in self._secrets:
            text = text.replace(value, "***")
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
