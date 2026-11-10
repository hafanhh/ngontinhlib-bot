"""Everything about logging in one package (the log FILES are in the logs/ folder):

  paths.py     where the log files live
  context.py   req_id (one short code per question, or per /add or /edit conversation) and masking of user ids
  redact.py    hides tokens and keys in every log line
  setup.py     setup_logging(): terminal + rotating file + LOG_LEVEL
  summary.py   python -m logtools.summary  ->  today's totals from logs/bot.log

Use it like this:
    import logtools
    logtools.setup_logging()
    logtools.new_req_id()
"""

from logtools.context import clear_req_id, get_req_id, mask_user_id, new_req_id, set_req_id
from logtools.setup import setup_logging

__all__ = ["setup_logging", "new_req_id", "set_req_id", "clear_req_id", "get_req_id", "mask_user_id"]
