"""Tests for the logging upgrade, fully offline (fake tokens, fake Telegram, fake Notion).
It writes to a temporary folder, never to your real logs/bot.log, and never reads your real keys.
Run with:  python -m tests.test_logging"""

import asyncio
import logging
import os
import re
import tempfile
import types as pytypes

# Fake secrets, set BEFORE the bot is imported (so your real .env values are never used here)
FAKE_BOT_TOKEN = "123456789:FAKE-bot-token-abcdefghijklmnop"
os.environ.update(
    BOT_TOKEN=FAKE_BOT_TOKEN,
    TELEGRAM_USER_ID="123456789",
    NOTION_TOKEN="secret_FAKEnotiontoken1234567890",
    NOTION_DATA_SOURCE_ID="fake-id",
    GEMINI_API_KEY="AIzaFAKEgeminikey1234567890abcdef",
    LOG_LEVEL="INFO",
)

import httpx
import httpx2
from google.genai import errors as gemini_errors
from notion_client import errors as notion_errors
from telegram import Update
from telegram.ext import Application

import logging_setup

# Send the log file to a temporary folder for this test
TMP = tempfile.mkdtemp()
logging_setup.LOG_DIR = TMP
logging_setup.LOG_FILE = os.path.join(TMP, "bot.log")

import bot
from services import gemini_agent as g

failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def read_log() -> str:
    with open(logging_setup.LOG_FILE, encoding="utf-8") as handle:
        return handle.read()


def new_lines(action) -> list:
    """Run action(), return the log lines it wrote."""
    before = len(read_log())
    action()
    return read_log()[before:].splitlines()


# ---- fake Telegram objects ------------------------------------------------------------
class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


def fake_update(text, user_id=123456789):
    return pytypes.SimpleNamespace(message=FakeMessage(text), effective_user=pytypes.SimpleNamespace(id=user_id),
                                   effective_chat=pytypes.SimpleNamespace(id=1))


async def _noop(**kwargs):
    pass


context = pytypes.SimpleNamespace(bot=pytypes.SimpleNamespace(send_chat_action=_noop))


def ask(text, answer_function):
    """Run handle_question with a fake answer_question. Returns (log lines, replies)."""
    bot.answer_question = answer_function
    update = fake_update(text)
    lines = new_lines(lambda: asyncio.run(bot.handle_question(update, context)))
    return lines, update.message.replies


REQ = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (\w+) \[([a-z0-9-]+)\] (.*)$")

print("--- format and level ---")
check("startup: root has a terminal handler and a rotating file handler",
      {type(h).__name__ for h in logging.getLogger().handlers} == {"StreamHandler", "RotatingFileHandler"})
file_handler = [h for h in logging.getLogger().handlers if type(h).__name__ == "RotatingFileHandler"][0]
check("file rotates at about 1 MB and keeps 3 old copies", file_handler.maxBytes == 1_000_000 and file_handler.backupCount == 3)
for value, expected in (("DEBUG", logging.DEBUG), ("warning", logging.WARNING), ("nonsense", logging.INFO), ("", logging.INFO)):
    os.environ["LOG_LEVEL"] = value
    check(f"LOG_LEVEL={value!r} -> {logging.getLevelName(expected)}", logging_setup._read_level() == expected)
del os.environ["LOG_LEVEL"]
check("LOG_LEVEL missing -> INFO", logging_setup._read_level() == logging.INFO)
os.environ["LOG_LEVEL"] = "INFO"
check("httpx/httpx2 loggers stay quiet", all(logging.getLogger(n).level >= logging.WARNING for n in ("httpx", "httpcore", "httpx2", "httpcore2")))
check("mask_user_id keeps 4 digits", logging_setup.mask_user_id(123456789) == "1234***")

print("--- one question, ok ---")


def good_answer(chat_id, text):
    logging.getLogger("services.test").info("work in the worker thread")  # req_id must follow into the thread
    return "câu trả lời"


long_question = "Gợi ý truyện sủng " + "x" * 100
lines, replies = ask(long_question, good_answer)
parsed = [REQ.match(line) for line in lines]
check("every line has time, level, [req_id], message", all(parsed))
ids = {m.group(2) for m in parsed}
check("all lines of the question share ONE req_id of 5 characters", len(ids) == 1 and len(next(iter(ids))) == 5)
check("order: Incoming, thread line, Reply sent, Done",
      [m.group(3).split(":")[0] for m in parsed] == ["Incoming", "work in the worker thread", "Reply sent", "Done"])
check("Reply sent shows how many characters were sent", parsed[2].group(3) == f"Reply sent: chars={len('câu trả lời')}")
check("Incoming has the masked user id, the length and only 50 characters",
      "user=1234***" in lines[0] and f"length={len(long_question)}" in lines[0] and "123456789" not in lines[0]
      and long_question[:50] in lines[0] and long_question[:51] not in lines[0])
check("Done has status=ok and the total time", re.search(r"Done: status=ok total_time=\d+\.\d\ds$", lines[-1]) is not None)
check("the user still gets the answer", replies == ["câu trả lời"])
check("after the question the req_id is cleared", logging_setup.get_req_id() == "-")
lines2, _ = ask("câu hai", good_answer)
check("the next question gets a different req_id", REQ.match(lines2[0]).group(2) != next(iter(ids)))

print("--- errors: logged with traceback, user gets a friendly message ---")
cases = [
    ("Notion error", notion_errors.APIResponseError("rate_limited", 429, "x", httpx.Headers(), "{}"), "Notion"),
    ("Gemini error", gemini_errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "X"}}), "Gemini"),
    ("timeout (Gemini)", httpx2.ReadTimeout("slow"), "quá chậm"),
    ("timeout (Notion)", httpx.ReadTimeout("slow"), "quá chậm"),
    ("unexpected error", ValueError("a bug"), "gặp lỗi"),
]
for source, error, word in cases:
    def failing(chat_id, text, error=error):
        raise error
    lines, replies = ask("hỏi", failing)
    text = "\n".join(lines)
    check(f"{source}: logged as ERROR with type, message and a traceback",
          f"ERROR [" in text and f"Failed [{source}] {type(error).__name__}" in text and "Traceback" in text)
    check(f"{source}: Done status=error", "Done: status=error" in lines[-1])
    check(f"{source}: the user still gets a friendly reply", len(replies) == 1 and word in replies[0])
    check(f"{source}: 'Reply sent' is logged before Done", " Reply sent: chars=" in lines[-2])

print("--- Reply sent: only when Telegram really accepted the message ---")
lines, replies = ask("dài", lambda chat_id, text: "y" * 9000)
check("a long answer is cut to 4000 characters and the line shows the sent length",
      any("Reply sent: chars=4000" in l for l in lines) and len(replies[0]) == 4000)


async def broken_reply(text):
    raise bot.NetworkError("Telegram is down")


update = fake_update("hỏi")
update.message.reply_text = broken_reply
bot.answer_question = good_answer
before = len(read_log())
try:
    asyncio.run(bot.handle_question(update, context))
except bot.NetworkError:
    pass  # the error handler (on_error) deals with this one, as before
lines = read_log()[before:].splitlines()
check("when Telegram refuses the reply there is NO 'Reply sent' line", not any("Reply sent" in l for l in lines))
check("... and Done says status=error (the user got nothing)", "Done: status=error" in lines[-1])

print("--- Gemini function call + Notion query ---")
shown_story = {"name": "TRUYỆN-BÍ-MẬT", "author": "A", "chapters": 10, "tags": ["Sủng"], "link": "https://example.com/x"}
g._current_chat_id = 1
g._search_state.clear()
g._reset_usage()
g.notion_search.search_stories = lambda **kw: {"total": 5, "skip": 0, "shown": 1, "remaining": 4, "stories": [shown_story]}
lines = new_lines(lambda: g.find_stories(tags=["Sủng"], reading_status="Not started"))
text = "\n".join(lines)
check("function call line has the name and the filters",
      "Gemini function call: find_stories filters=" in text and "'Sủng'" in text and "Not started" in text)
check("Notion line has the number of results and the time", re.search(r"Notion query: results=5 shown=1 time=\d+\.\d\ds", text) is not None)
check("story lists are NOT logged, only counts", "TRUYỆN-BÍ-MẬT" not in text and "example.com" not in text)
check("no warning when there are results", "WARNING" not in text)

g.notion_search.search_stories = lambda **kw: {"total": 0, "skip": 0, "shown": 0, "remaining": 0, "stories": []}
lines = new_lines(lambda: g.find_stories(tags=["Sủng"]))
check("0 results -> a WARNING", any(" WARNING " in l and "0 results" in l for l in lines))


def notion_down(**kw):
    raise notion_errors.APIResponseError("internal_server_error", 500, "Notion is down", httpx.Headers(), "{}")


g.notion_search.search_stories = notion_down
try:
    lines = new_lines(lambda: g.find_stories(tags=["Sủng"]))
    raised = False
except Exception:
    raised = True
    lines = read_log().splitlines()[-30:]
text = "\n".join(lines)
check("a Notion error is logged as one ERROR line with its type and time (the traceback comes from bot.py, once)",
      "ERROR [" in text and "Notion query failed [APIResponseError] after " in text and "Traceback" not in text)

check("the error is still raised (behavior unchanged: the SDK passes it to Gemini)", raised)

g.notion_search.search_stories = lambda **kw: {"total": 5, "skip": 0, "shown": 1, "remaining": 4, "stories": [shown_story]}
g._reset_usage()
all_lines = []
for _ in range(8):
    all_lines += new_lines(lambda: g.find_stories(tags=["Sủng"]))
check("no function-call warning any more (Gemini makes at most one search per question now)",
      not any("function calls reached" in l for l in all_lines))
lines = new_lines(lambda: g._log_usage(force=True))
check("usage line shows the call count", len(lines) == 1 and "function_calls=8" in lines[0])
g._reset_usage()
g._count_usage(httpx2.Response(200, json={"usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3, "totalTokenCount": 10}}))
g._usage["function_calls"] = 2
lines = new_lines(g._log_usage)
check("usage line: model, prompt, output, function_calls and model_requests", f"model={g.GEMINI_MODEL} prompt=7 output=3 function_calls=2 model_requests=1" in lines[0])

print("--- strangers ---")
stranger = fake_update("hi", user_id=987654321)
lines = new_lines(lambda: asyncio.run(bot.reject_stranger(stranger, context)))
check("a stranger gives a WARNING with the id masked", len(lines) == 1 and " WARNING " in lines[0] and "9876***" in lines[0] and "987654321" not in lines[0])
check("... and no reply is sent", stranger.message.replies == [])

# Is the handler really wired in, and does it fire only for strangers?
recorded = []
Application.add_handler = lambda self, handler, group=0: recorded.append((handler, group))
Application.run_polling = lambda self: None
bot.main()


def raw_update(user_id):
    return Update.de_json({"update_id": 1, "message": {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
                          "from": {"id": user_id, "is_bot": False, "first_name": "x"}, "text": "hello"}}, None)


def fired(user_id, group):
    return [h for h, grp in recorded if grp == group and h.check_update(raw_update(user_id))]


check("a stranger's message reaches the warning handler", len(fired(987654321, 1)) == 1)
check("... and matches no normal handler (still ignored)", fired(987654321, 0) == [])
check("your own message does NOT reach the warning handler", fired(123456789, 1) == [])
check("... and reaches the normal question handler", len(fired(123456789, 0)) == 1)

print("--- secrets are never written ---")
log = logging.getLogger("secret.test")
try:
    raise RuntimeError(f"failed calling https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getUpdates")
except RuntimeError:
    log.exception("token %s and key %s and %s", os.environ["NOTION_TOKEN"], os.environ["GEMINI_API_KEY"],
                  "https://x.test/v1?key=SUPERSECRETVALUE&a=1 secret_abcdefghijklmnop bot111222:AAAA-bbbb/x")
content = read_log()
for secret in (FAKE_BOT_TOKEN, "FAKE-bot-token", os.environ["NOTION_TOKEN"], os.environ["GEMINI_API_KEY"],
               "SUPERSECRETVALUE", "secret_abcdefghijklmnop", "AAAA-bbbb"):
    check(f"'{secret[:14]}...' is not in bot.log (also not in the traceback)", secret not in content)
check("... they are replaced by ***", "***" in content)
check("normal text next to a hidden secret is kept", "&a=1" in content and "failed calling" in content)

print("--- log_summary.py ---")
import log_summary

log_summary.LOG_FILE = logging_setup.LOG_FILE
import contextlib
import io
import sys

buffer = io.StringIO()
sys.argv = ["log_summary.py"]
with contextlib.redirect_stdout(buffer):
    log_summary.main()
output = buffer.getvalue()
questions_today = len(re.findall(r"\] Incoming:", read_log()))
check("it counts today's questions", f"Số câu hỏi:          {questions_today}" in output)
check("it counts errors (5 failed questions + 1 reply Telegram refused)", "Số câu bị lỗi:       6" in output)
check("it adds up tokens (the 7+3 test line)", "Tổng prompt tokens:  7" in output and "Tổng output tokens:  3" in output)

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
raise SystemExit(1 if failures else 0)
