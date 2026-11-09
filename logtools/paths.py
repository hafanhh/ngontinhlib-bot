"""Where the log files live. This is the ONLY place that knows the paths:
bot.py's logging, the log summary and the Notion write log all take them from here."""

import os

# The project folder (one level above this logtools/ folder)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# All logs go in the logs/ folder (git-ignored, created automatically)
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
BOT_LOG = os.path.join(LOG_DIR, "bot.log")        # what the bot does and its errors
WRITES_LOG = os.path.join(LOG_DIR, "writes.log")  # every change made to Notion (before/after)
