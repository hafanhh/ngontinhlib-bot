"""Tests for the token-usage log line, WITHOUT calling the real Gemini.
A small fake Gemini server runs on your own computer and answers with made-up token counts.
Run with:  python -m tests.test_usage
(The only real call is reading the tag list from Notion, which is read-only.)"""

import http.server
import json
import logging
import re
import threading
import time

import httpx2
from google import genai

from services import gemini_agent as g

g.notion_search.EXTRA_SOURCE_IDS = []  # this test is about token counting, list 1 is enough
g.notion_search.NOTION_ADD_DATA_SOURCE_ID = None  # and /add writes to list 1 here

failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


# ---- a fake Gemini server ---------------------------------------------------------

mode = {"kind": "ok", "delay": 0}   # what the fake server does next: ok | thinking | error (+ a delay in seconds)
requests_seen = []


class FakeGemini(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        requests_seen.append(self.path)
        time.sleep(mode["delay"])
        if mode["kind"] == "error":
            self._reply(500, {"error": {"code": 500, "message": "boom", "status": "INTERNAL"}})
            return
        usage = {"promptTokenCount": 520, "candidatesTokenCount": 140, "totalTokenCount": 660}
        if mode["kind"] == "thinking":
            usage.update(thoughtsTokenCount=30, cachedContentTokenCount=100, totalTokenCount=690)
        self._reply(200, {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "Xin chào"}]},
                            "finishReason": "STOP", "index": 0}],
            "usageMetadata": usage,
        })

    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), FakeGemini)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE_URL = f"http://127.0.0.1:{server.server_address[1]}"

# Collect the 'usage:' log lines the bot would write
usage_lines = []
request_lines = []   # the 'Gemini model_request=...' lines (one per request)


class Grab(logging.Handler):
    def emit(self, record):
        if record.getMessage().startswith("usage:"):
            usage_lines.append(record.getMessage())
        elif record.getMessage().startswith("Gemini model_request="):
            request_lines.append(record.getMessage())


g.logger.addHandler(Grab())
g.logger.setLevel(logging.INFO)

print("--- the counting hook ignores anything odd and never raises ---")
g._reset_usage()
g._count_usage(httpx2.Response(200, content=b"this is not json"))
g._count_usage(httpx2.Response(200, json=["a", "list"]))
g._count_usage(httpx2.Response(429, json={"error": {"code": 429, "message": "quota"}}))
g._count_usage(httpx2.Response(200, json={"candidates": []}))
check("a non-JSON body, a list, an error response and a body without usage: counted as requests, but no tokens",
      g._usage["model_requests"] == 4 and g._usage["prompt"] == 0 and g._usage["total"] == 0 and not g._seen)
g._reset_usage()
g._count_usage(httpx2.Response(200, json={"usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 10}}))
check("a response with usage counts, and missing fields count as 0",
      g._usage["model_requests"] == 1 and g._usage["prompt"] == 10 and g._usage["output"] == 0 and g._usage["total"] == 10)

print("--- usage is added up over every request of a question ---")
g._reset_usage()
client = genai.Client(api_key="dummy", http_options=g._http_options(base_url=BASE_URL))
mode["kind"] = "ok"
first = client.models.generate_content(model="m", contents="hi")
second = client.models.generate_content(model="m", contents="hi again")
check("the library still returns the answer normally (the hook does not disturb it)", first.text == "Xin chào" and second.text == "Xin chào")
check("2 requests are added up: prompt 1040, output 280, total 1320",
      (g._usage["model_requests"], g._usage["prompt"], g._usage["output"], g._usage["total"]) == (2, 1040, 280, 1320))
usage_lines.clear()
g._log_usage()
check("the log line has the readable format", usage_lines == [f"usage: model={g.GEMINI_MODEL} prompt=1040 output=280 function_calls=0 model_requests=2 total=1320"])
g._reset_usage()
usage_lines.clear()
g._log_usage()
check("nothing is logged when no request was counted", usage_lines == [])

print("--- every Gemini request is timed ---")
g._reset_usage()
request_lines.clear()
mode["kind"], mode["delay"] = "ok", 0.3
timed_client = genai.Client(api_key="dummy", http_options=g._http_options(base_url=BASE_URL))
timed_client.models.generate_content(model="m", contents="slow one")
mode["delay"] = 0
timed_client.models.generate_content(model="m", contents="fast one")
check("one line per request, numbered 1, 2", len(request_lines) == 2 and "model_request=1 " in request_lines[0] and "model_request=2 " in request_lines[1])
seconds = [float(re.search(r"time=([\d.]+)s", l).group(1)) for l in request_lines]
check("the slow request shows its time (at least 0.30s) and the fast one is much shorter", seconds[0] >= 0.3 and seconds[1] < seconds[0])
check("the line has status and the tokens of that request", "status=200 prompt=520 output=140" in request_lines[0])
g._reset_usage()
request_lines.clear()
mode["kind"] = "error"
try:
    timed_client.models.generate_content(model="m", contents="fails")
except Exception:
    pass
check("a failed request is timed too, with its status", len(request_lines) == 1 and "status=500" in request_lines[0] and "prompt=n/a output=n/a" in request_lines[0])
mode["kind"] = "ok"
g._reset_usage()
request_lines.clear()
g._count_usage(httpx2.Response(200, json={}))  # a response that came without the 'request started' hook
check("without a start time the time is n/a, nothing crashes", len(request_lines) == 1 and "time=n/a" in request_lines[0])

print("--- a missing value is logged as n/a, never a crash ---")
g._reset_usage()
g._count_usage(httpx2.Response(200, json={"usageMetadata": {"promptTokenCount": 10}}))
usage_lines.clear()
g._log_usage()
check("output and total were not sent -> n/a",
      usage_lines == [f"usage: model={g.GEMINI_MODEL} prompt=10 output=n/a function_calls=0 model_requests=1 total=n/a"])
g._reset_usage()
usage_lines.clear()
g._log_usage(force=True)
check("a successful question with no usage at all still logs a line, all n/a",
      usage_lines == [f"usage: model={g.GEMINI_MODEL} prompt=n/a output=n/a function_calls=0 model_requests=0 total=n/a"])

print("--- answer_question writes one line per question ---")
# One client, kept alive for the whole test, like the cached client in the real bot
# (the library closes a client's connection when the client is garbage-collected)
fake_client = genai.Client(api_key="dummy", http_options=g._http_options(base_url=BASE_URL))
g._client = lambda: fake_client
g._history.clear()
usage_lines.clear()
request_lines.clear()
answer = g.answer_question(501, "Xin chào")
check("the answer is returned", answer == "Xin chào")
check("exactly one usage line, in the example format",
      usage_lines == [f"usage: model={g.GEMINI_MODEL} prompt=520 output=140 function_calls=0 model_requests=1 total=660"])
g.answer_question(501, "Câu thứ hai")
check("each question numbers its requests from 1 again (2 questions, 1 request each)",
      len(request_lines) == 2 and all(l.startswith("Gemini model_request=1 ") for l in request_lines))
check("the counters restart for each question (not 1040 on the second)",
      len(usage_lines) == 2 and usage_lines[1] == usage_lines[0])

mode["kind"] = "thinking"
usage_lines.clear()
g.answer_question(501, "Câu thứ ba")
check("thinking and cached tokens are added only when there are some",
      usage_lines == [f"usage: model={g.GEMINI_MODEL} prompt=520 output=140 function_calls=0 model_requests=1 thinking=30 cached=100 total=690"])

print("--- a failed question ---")
mode["kind"] = "error"
usage_lines.clear()
try:
    g.answer_question(501, "Câu bị lỗi")
    raised = False
except Exception:
    raised = True
check("the error still reaches the caller (the bot shows its friendly message)", raised)
check("a request that failed uses no tokens, so no stale numbers are logged", usage_lines == [])

g._history.clear()
print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
raise SystemExit(1 if failures else 0)
