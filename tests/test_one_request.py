"""Tests for the "one Gemini request per question" design, WITHOUT the real Gemini or Notion.
A fake Gemini server on your own computer answers with a scripted reply, and Notion is a fake
table of 25 stories. Checks that: only ONE request is sent per question, the program (not Gemini)
writes the list, the memory stays small, and odd replies from Gemini never crash the bot.
Run with:  python -m tests.test_one_request"""

import http.server
import json
import logging
import threading

from google import genai

from services import gemini_agent as g
from services import notion_search

notion_search.DATA_SOURCE_IDS = notion_search.DATA_SOURCE_IDS[:1]  # these tests use ONE list; test_multi_lists covers several
notion_search.ADD_SOURCE_ID = None

failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


# ---- fake Notion: 25 stories ------------------------------------------------------------
def make_page(number: int) -> dict:
    return {"id": f"p{number}", "url": f"https://notion.so/p{number}", "properties": {
        "Name": {"title": [{"plain_text": f"Truyện số {number:02d}"}]},
        "Author": {"rich_text": [{"plain_text": "Tác giả A"}]},
        "Chapters": {"number": 10 + number},
        "Tags": {"multi_select": [{"name": "Sủng"}, {"name": "HE"}]},
        "Link": {"url": f"https://example.com/{number}"},
    }}


class FakeTable:
    def __init__(self, count=25):
        self.pages = [make_page(i) for i in range(1, count + 1)]
        self.fail = None

    @property
    def data_sources(self):
        return self

    def query(self, **kw):
        if self.fail:
            raise self.fail
        return {"results": self.pages, "has_more": False, "next_cursor": None}


table = FakeTable()
notion_search._client = lambda: table
notion_search.get_valid_tags = lambda source_id=None: ["Sủng", "HE", " Cổ Đại"]

# ---- fake Gemini: replies with whatever the test queued, and remembers every request ----
queue = []      # replies to send, one per request
bodies = []     # the JSON of every request received


class FakeGemini(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        bodies.append(json.loads(self.rfile.read(int(self.headers.get("content-length", 0)))))
        parts = queue.pop(0)
        data = json.dumps({
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
            "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 30, "totalTokenCount": 530},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), FakeGemini)
threading.Thread(target=server.serve_forever, daemon=True).start()
fake_client = genai.Client(api_key="dummy", http_options=g._http_options(base_url=f"http://127.0.0.1:{server.server_address[1]}"))
g._client = lambda: fake_client

# Collect the log lines of the bot
lines = []


class Grab(logging.Handler):
    def emit(self, record):
        lines.append(record.getMessage())


g.logger.addHandler(Grab())
g.logger.setLevel(logging.INFO)


def call(name="find_stories", **args):
    """A scripted reply: Gemini asks for a function call."""
    return [{"functionCall": {"name": name, "args": args}}]


def ask(chat_id, question, reply):
    queue.append(reply)
    before = len(bodies)
    lines.clear()
    answer = g.answer_question(chat_id, question)
    return answer, len(bodies) - before


def contents_of(request_body):
    return request_body["contents"]


g._history.clear()
g._search_state.clear()

print("--- one question = ONE request to Gemini ---")
answer, sent = ask(1, "Gợi ý truyện sủng HE chưa đọc", call(tags=["sủng", "HE"], reading_status="Not started"))
check("exactly one request was sent (no second request to 'read the results')", sent == 1)
check("the program wrote the answer: header with total, conditions and 'random'",
      answer.startswith("Tìm thấy 25 truyện (tags sủng + HE, chưa đọc). Gợi ý ngẫu nhiên 10 truyện:"))
check("the list has 10 numbered stories with author, chapters, tags and link",
      sum(1 for l in answer.splitlines() if l[:3].strip(". ").isdigit() or l.split(".")[0].isdigit()) == 10
      and "Tác giả A" in answer and "chương" in answer and "Tags: Sủng, HE" in answer and "https://example.com/" in answer)
check("the footer says how many are left and how to continue", 'Còn 15 truyện nữa, nhắn "xem thêm" để xem tiếp.' in answer)
check("plain text only (no Markdown)", "*" not in answer and "#" not in answer)
check("log: one model request, one function call, usage line says 1 and 1",
      any(l.startswith("Gemini model_request=1 ") for l in lines) and not any(l.startswith("Gemini model_request=2") for l in lines)
      and any("function_calls=1 model_requests=1" in l for l in lines))

body = bodies[-1]
declarations = [f for t in body["tools"] for f in t.get("functionDeclarations", [])]
check("find_stories is offered to Gemini with the real tag names", len(declarations) == 1 and declarations[0]["name"] == "find_stories"
      and "Sủng" in declarations[0]["description"] and "Cổ Đại" in declarations[0]["description"])
check("the tool description is short (no result format, no shuffle text)", "xáo trộn" not in declarations[0]["description"]
      and len(declarations[0]["description"]) < 900)
check("the output limit is set", body["generationConfig"]["maxOutputTokens"] == g.MAX_OUTPUT_TOKENS)
check("the system prompt is the short one", body["systemInstruction"]["parts"][0]["text"] == g.SYSTEM_PROMPT and len(g.SYSTEM_PROMPT) < 1700)

print("--- the memory is small and 'xem thêm' continues the same search ---")
check("the memory holds 1 exchange of 4 messages", len(g._history[1]) == 1 and len(g._history[1][0]) == 4)
remembered = json.dumps([[m.model_dump(mode="json", exclude_none=True) for m in ex] for ex in g._history[1]], ensure_ascii=False)
check("... without any story name, author or link", "Truyện số" not in remembered and "example.com" not in remembered and "Tác giả A" not in remembered)
check("... but with one line about what was shown", "Đã hiển thị truyện 1-10 trong 25 truyện" in remembered and "chưa đọc" in remembered)
first_names = {l.split(". ", 1)[1].split(" - ")[0] for l in answer.splitlines() if l[:1].isdigit()}

answer2, sent = ask(1, "xem thêm", call(next_page=True))
check("'xem thêm' also costs one request", sent == 1)
check("the second batch continues the numbering and the conditions", answer2.startswith("Tiếp theo, truyện 11-20 trong 25 truyện (tags sủng + HE, chưa đọc):"))
names2 = {l.split(". ", 1)[1].split(" - ")[0] for l in answer2.splitlines() if l[:1].isdigit()}
check("no story is repeated", len(names2) == 10 and not (names2 & first_names))
sent_contents = contents_of(bodies[-1])
roles = [c["role"] for c in sent_contents]
check("the request carries the earlier exchange: user, model call, user result, model note, new question",
      roles == ["user", "model", "user", "model", "user"] and "functionCall" in sent_contents[1]["parts"][0]
      and "functionResponse" in sent_contents[2]["parts"][0])
check("the earlier result sent back to Gemini holds only numbers", "Truyện số" not in json.dumps(sent_contents, ensure_ascii=False))

answer3, _ = ask(1, "tiếp đi", call(next_page=True))
answer4, _ = ask(1, "tiếp nữa", call(next_page=True))
check("the last batch has 5 stories and no 'xem thêm' footer", answer3.count("\n   https://") == 5 and "xem thêm" not in answer3)
check("after the end: 'Đã hết rồi'", answer4.startswith("Đã hết rồi: bạn đã xem hết 25 truyện"))

print("--- Gemini answers with text only (no search) ---")
answer, sent = ask(2, "Chào bạn", [{"text": "Mình chỉ tìm truyện trong thư viện của bạn thôi nhé."}])
check("the text is returned as it is, one request", sent == 1 and answer == "Mình chỉ tìm truyện trong thư viện của bạn thôi nhé.")
check("log says no function call and function_calls=0", any("without a function call" in l for l in lines) and any("function_calls=0" in l for l in lines))
check("the memory has 2 messages", len(g._history[2][0]) == 2)

print("--- odd replies never crash the bot ---")
answer, _ = ask(3, "?", call(name="delete_everything", x=1))
check("an unknown function -> a polite message", answer == g._UNDERSTAND_ERROR)
answer, _ = ask(3, "?", call(tags=[1, 2]))
check("tags that are not text -> a polite message", answer == g._UNDERSTAND_ERROR)
answer, _ = ask(3, "?", call(max_chapters="rất nhiều"))
check("max_chapters that is not a number -> a polite message", answer == g._UNDERSTAND_ERROR)
answer, _ = ask(3, "?", call(tags="Sủng", max_chapters=30.0, invented="x"))
check("a single tag as text, 30.0 as a number and an invented argument are all fixed or ignored",
      answer.startswith("Tìm thấy 25 truyện (tag Sủng, tối đa 30 chương)"))
answer, _ = ask(3, "?", call(tags=["Không có tag này"]))
check("a wrong tag -> the program's own Vietnamese error with the valid values", "không hợp lệ" in answer and "Sủng" in answer)
answer, _ = ask(3, "?", [{"text": ""}])
check("an empty reply -> a safe fallback message", "chưa tạo được câu trả lời" in answer)
answer, _ = ask(3, "?", call(next_page=True) + call(tags=["HE"]))
check("two calls at once: only the first is used, with a warning",
      "Tiếp theo" in answer or "Tìm thấy" in answer or "Chưa có" in answer)
check("... and the warning is logged", any("2 function calls at once" in l for l in lines))
g._search_state.clear()
answer, _ = ask(4, "xem thêm", call(next_page=True))
check("'xem thêm' with no earlier search -> asks to search again", "Chưa có lần tìm trước" in answer)

print("--- a text answer with a empty search: zero results ---")
table.pages, saved = [], table.pages
answer, _ = ask(5, "tìm", call(tags=["HE"]))
table.pages = saved
check("0 results: says so, with the conditions", answer.startswith("Không tìm thấy truyện nào khớp: tag HE.") and "bỏ bớt" in answer)
check("log has the 0 results warning", any("0 results" in l for l in lines))

print("--- a Notion failure reaches the caller (bot.py shows the friendly message) ---")
table.fail = RuntimeError("notion down")
queue.append(call(tags=["HE"]))
try:
    g.answer_question(6, "tìm")
    raised = False
except RuntimeError:
    raised = True
table.fail = None
check("the error is raised, not swallowed", raised)
check("the usage line was still logged (the Gemini tokens were used)", any(l.startswith("usage:") for l in lines))
check("a failed question is not remembered", g._history[6] == [])

print("--- memory limit and Gemini's thought signature ---")
g._history.clear()
for n in range(8):
    ask(7, f"câu {n}", call(tags=["HE"]))
check(f"only the last {g.MAX_EXCHANGES} questions are kept", len(g._history[7]) == g.MAX_EXCHANGES)
check("the oldest are the ones forgotten", g._history[7][0][0].parts[0].text == "câu 3")
check("a request carries at most 5 old exchanges (20 messages) + the new question", len(contents_of(bodies[-1])) == 4 * g.MAX_EXCHANGES + 1)

g._history.clear()
queue.append([{"functionCall": {"name": "find_stories", "args": {"tags": ["HE"]}}, "thoughtSignature": "U0lHTkFUVVJFLTEyMw=="}])  # (the signature is base64 of SIGNATURE-123)
g.answer_question(8, "có chữ ký")
queue.append(call(next_page=True))
g.answer_question(8, "xem thêm")
check("the thought signature Gemini sent is given back unchanged in the next request",
      "U0lHTkFUVVJFLTEyMw==" in json.dumps(bodies[-1]))

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
raise SystemExit(1 if failures else 0)
