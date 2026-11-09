"""Tests for error handling: the message the user sees for each kind of error, network
errors from the Gemini library, logging that must not reveal secrets, and timeouts.
It only talks to servers on your own computer (never to the internet), except for reading
settings from your .env.
Run with:  python -m tests.test_errors"""

import logging
import socket
import threading
import types as pytypes

import httpx
import httpx2
from google import genai
from google.genai import errors as gemini_errors
from google.genai import types
from notion_client import errors as notion_errors
from telegram.error import NetworkError

import bot
from services import gemini_agent, notion_search

# Switch logging on the way the bot does, but with a temporary log folder (never your real log),
# so the "quiet http libraries" checks below test the real setup. Then remove the handlers,
# so this test's deliberate errors are not printed.
import tempfile

import logtools

logtools.setup_logging(log_dir=tempfile.mkdtemp())
for handler in list(logging.getLogger().handlers):
    logging.getLogger().removeHandler(handler)
    handler.close()

failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def notion_error(code: str, status: int):
    return notion_errors.APIResponseError(code, status, "x", httpx.Headers(), "{}")


def gemini_error(cls, code: int):
    return cls(code, {"error": {"code": code, "message": "x", "status": "X"}})


say = bot.friendly_error

print("--- Notion errors: each one names its real cause ---")
check("rate_limited -> slow down, not 'check your token'",
      "giới hạn tốc độ" in say(notion_error("rate_limited", 429)) and "NOTION_TOKEN" not in say(notion_error("rate_limited", 429)))
check("a 429 status alone is also a rate limit", "giới hạn tốc độ" in say(notion_error("something_else", 429)))
for status in (500, 502, 503):
    message = say(notion_error("internal_server_error", status))
    check(f"Notion {status} -> Notion has a problem, not 'check your token'", "gặp sự cố" in message and "NOTION_TOKEN" not in message)
for code in ("unauthorized", "restricted_resource", "object_not_found"):
    check(f"{code} -> check NOTION_TOKEN and sharing", "NOTION_TOKEN" in say(notion_error(code, 401)))
other = say(notion_error("validation_error", 400))
check("any other Notion error shows its code", "validation_error" in other and "NOTION_TOKEN" not in other)
check("Notion timeout", "quá chậm" in say(notion_errors.RequestTimeoutError()))

print("--- Gemini errors ---")
check("429 -> quota", "quota" in say(gemini_error(gemini_errors.ClientError, 429)))
check("401 and 403 -> check the API key", all("GEMINI_API_KEY" in say(gemini_error(gemini_errors.ClientError, c)) for c in (401, 403)))
check("404 -> check the model name", "GEMINI_MODEL" in say(gemini_error(gemini_errors.ClientError, 404)))
check("other 4xx -> rephrase", "diễn đạt lại" in say(gemini_error(gemini_errors.ClientError, 400)))
check("5xx -> Gemini has a problem", "gặp sự cố" in say(gemini_error(gemini_errors.ServerError, 503)))

print("--- network errors from BOTH http libraries ---")
check("httpx.ConnectError (Notion, Telegram) -> network message", "kết nối mạng" in say(httpx.ConnectError("x")))
check("httpx2.ConnectError (Gemini) -> network message, not the generic one", "kết nối mạng" in say(httpx2.ConnectError("x")))
check("httpx.ReadTimeout -> timeout message", "quá chậm" in say(httpx.ReadTimeout("x")))
check("httpx2.ReadTimeout -> timeout message", "quá chậm" in say(httpx2.ReadTimeout("x")))
check("anything unknown -> a safe generic message", "gặp lỗi" in say(ValueError("boom")))

print("--- real network failures against servers on this computer ---")
# 1) nothing is listening on port 9 -> connection refused
refused = genai.Client(api_key="dummy", http_options=types.HttpOptions(base_url="http://127.0.0.1:9", timeout=3000))
try:
    refused.models.generate_content(model="m", contents="hi")
    message = "(no error)"
except Exception as error:
    message = say(error)
check("Gemini library + refused connection -> 'Lỗi kết nối mạng'", "kết nối mạng" in message)

# 2) a server that accepts the connection but never answers (a hang)
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(5)
held = []
threading.Thread(target=lambda: [held.append(server.accept()) for _ in range(5)], daemon=True).start()
hung = genai.Client(api_key="dummy", http_options=types.HttpOptions(
    base_url=f"http://127.0.0.1:{server.getsockname()[1]}", timeout=1000))
try:
    hung.models.generate_content(model="m", contents="hi")
    message = "(no error)"
except Exception as error:
    message = say(error)
check("Gemini library + a hang -> gives up after the timeout, with the timeout message", "quá chậm" in message)

print("--- the real Gemini client has a timeout (a hang cannot freeze the bot) ---")
real_client = gemini_agent._client()
check("timeout is set to the configured value", real_client._api_client._http_options.timeout == gemini_agent.GEMINI_TIMEOUT_MS)
check("... which is 60 seconds", gemini_agent.GEMINI_TIMEOUT_MS == 60_000)

print("--- Notion client is created once and reused ---")
check("same client object every time", notion_search._client() is notion_search._client())

print("--- logging never shows request URLs of any http library ---")
for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
    check(f"logger '{name}' is quiet (WARNING or higher)", logging.getLogger(name).getEffectiveLevel() >= logging.WARNING)

print("--- the catch-all error handler ---")
lines = []


class Grab(logging.Handler):
    def emit(self, record):
        lines.append((record.levelname, record.getMessage(), record.exc_info))


bot.logger.addHandler(Grab())
import asyncio

secret_error = NetworkError("HTTPSConnectionPool: https://api.telegram.org/bot123456:SECRET-TOKEN/getUpdates failed")
asyncio.run(bot.on_error(None, pytypes.SimpleNamespace(error=secret_error)))
check("a Telegram network error is logged as one short warning", len(lines) == 1 and lines[0][0] == "WARNING" and lines[0][2] is None)
check("... and its text (which can contain the bot token) is NOT logged", "SECRET-TOKEN" not in lines[0][1] and "123456" not in lines[0][1])
lines.clear()
asyncio.run(bot.on_error(None, pytypes.SimpleNamespace(error=ValueError("a real bug"))))
check("any other error is logged as an ERROR with its traceback", len(lines) == 1 and lines[0][0] == "ERROR" and lines[0][2] is not None)

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
raise SystemExit(1 if failures else 0)
