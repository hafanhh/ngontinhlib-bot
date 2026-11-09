import logging
import os
import random
import threading
import time
from functools import lru_cache
from typing import List, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

from services import notion_search, render

# Read the secrets from the .env file into environment variables
load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# The model name can be changed in .env without touching the code
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.6-flash"

# Give up on a Gemini request after this long. Without a limit, one hung request would
# hold the lock below forever and every later question would wait for it.
GEMINI_TIMEOUT_MS = 60_000

# How many of your recent questions the bot remembers per chat. Each one is stored in a very
# short form (the question, the search Gemini chose, and one line about what was shown),
# never the story list, so remembering a few costs very little.
MAX_EXCHANGES = 5

# Never let one answer from Gemini grow long: it only has to pick search conditions or say one line
MAX_OUTPUT_TOKENS = 512

# The rules Gemini follows. It only CHOOSES the search conditions: the program searches Notion
# and writes the list itself (services/render.py). Short on purpose: it is sent with every question.
SYSTEM_PROMPT = """Bạn chuyển câu hỏi tìm truyện của người dùng thành lời gọi công cụ find_stories.
Chương trình tự tìm và tự hiển thị danh sách, bạn KHÔNG viết lại danh sách.
- Câu hỏi về truyện: luôn gọi find_stories. Không bịa truyện.
- Chỉ dùng tag trong danh sách hợp lệ. Không khớp tag nào: hỏi lại ngắn gọn, đừng đoán.
- Nhiều tag = truyện phải có TẤT CẢ các tag.
- "dưới N chương" = max_chapters N-1. "tối đa N" hoặc "không quá N" = N.
- Xin gợi ý hoặc tìm truyện để đọc mà không nói tiến độ đọc: dùng reading_status="Not started".
- Không áp dụng mặc định đó khi hỏi theo tác giả hoặc tên truyện, hoặc khi người dùng nhắc tiến độ đọc (đã đọc, đang đọc, bỏ dở...).
- "xem thêm", "tiếp đi", "còn truyện nào khác không": gọi find_stories(next_page=true), không nhắc lại điều kiện.
- Câu nối tiếp ("chỉ lấy truyện đã hoàn thôi"): dựa vào lịch sử, gọi lại với đầy đủ điều kiện mới.
- Câu hỏi không liên quan đến tìm truyện: trả lời một câu ngắn rằng bạn chỉ tìm truyện.
- Trả lời bằng tiếng Việt, văn bản thuần (không Markdown), ngắn gọn.
"""


# What each chat last searched, so "xem thêm" can continue the same shuffled list:
# chat_id -> {"filters": the search conditions, "seed": the shuffle, "skip": how many were shown}
_search_state: dict = {}
_current_chat_id = 0  # set by answer_question while it holds the lock


def find_stories(
    tags: Optional[List[str]] = None,
    reading_status: Optional[str] = None,
    status: Optional[str] = None,
    max_chapters: Optional[int] = None,
    author: Optional[str] = None,
    next_page: Optional[bool] = False,
) -> dict:
    """(The real description is filled in below, because it needs the live tag list.)"""
    state = _search_state.get(_current_chat_id)
    if next_page and not state:
        # Nothing to continue (the search is forgotten when the bot restarts). Say so,
        # instead of silently running a brand-new search.
        return {"error": "Chưa có lần tìm trước để xem thêm. Hãy hỏi lại câu tìm truyện."}
    continued = bool(next_page)
    if continued:
        # "Show more": the SAME search (the filters we stored, not the ones Gemini repeated),
        # same shuffle, next batch.
        filters, seed, skip = state["filters"], state["seed"], state["skip"]
    else:
        # A new search: new random shuffle, starting from the first batch
        filters = dict(tags=tags, reading_status=reading_status, status=status,
                       max_chapters=max_chapters, author=author)
        seed, skip = random.getrandbits(32), 0

    # function_calls counts how many times Gemini called this function in the question.
    # (It is NOT model_requests, the number of requests we sent to Gemini: now always 1.)
    _usage["function_calls"] += 1

    # Log which function Gemini called and the filters it chose (handy when a result looks wrong)
    logger.info("Gemini function call: find_stories filters=%s skip=%s continued=%s", filters, skip, continued)
    started = time.monotonic()
    try:
        result = notion_search.search_stories(**filters, skip=skip, seed=seed)
    except ValueError as error:
        # A wrong tag/value: the message (Vietnamese, with the valid values) is shown to the user
        logger.info("Notion query rejected the filters: %s", error)
        return {"error": str(error)}
    except Exception as error:
        # A real Notion problem (network, rate limit, permission...). One line with the timing here;
        # the traceback and the friendly message to the user come from bot.py.
        logger.error("Notion query failed [%s] after %.2fs: %s",
                     type(error).__name__, time.monotonic() - started, error)
        raise
    # Only counts are logged, never the stories themselves
    logger.info("Notion query: results=%d shown=%d time=%.2fs",
                result["total"], result["shown"], time.monotonic() - started)
    if result["total"] == 0:
        logger.warning("Notion returned 0 results for these filters")

    _search_state[_current_chat_id] = {"filters": filters, "seed": seed, "skip": skip + result["shown"]}
    result["continued"] = continued
    result["filters"] = filters  # the conditions actually used (render.py shows them to the user)
    return result


@lru_cache(maxsize=1)
def _config() -> types.GenerateContentConfig:
    """Build the Gemini settings once. The tool description lists the real tags from
    Notion, so Gemini can only pick tags that exist."""
    valid_tags = ", ".join(t.strip() for t in notion_search.get_valid_tags())
    find_stories.__doc__ = f"""Tìm truyện trong thư viện Notion của người dùng. Mọi điều kiện đều tùy chọn, ghép bằng AND.
- tags: danh sách tag, truyện phải có TẤT CẢ. Chỉ dùng tag sau: {valid_tags}. ("Trùng sinh" và "Trọng sinh" là một.)
- reading_status: tiến độ ĐỌC của người dùng: "Not started" (chưa đọc), "In progress" (đang đọc), "Done" (đã đọc xong), "Beginning + End" (chỉ đọc đầu và cuối), "Drop" (đã bỏ).
- status: tình trạng của TRUYỆN: "Full" (đã hoàn), "In progress" (đang ra), "Drop" (tác giả bỏ dở).
- max_chapters: số chương tối đa (tính cả số này).
- author: tên tác giả hoặc một phần tên.
- next_page: true CHỈ khi muốn xem thêm của CHÍNH lần tìm trước (các tham số khác bị bỏ qua). Mặc định false.
"""
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[find_stories],  # Gemini may ask for this; WE run it (see answer_question)
        # The SDK must NOT run the tool and send the result back to Gemini (that was a second,
        # expensive request): we run it ourselves and write the list in render.py.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.3,
    )


# Numbers of the question being answered, added up over EVERY request it made.
#   model_requests = how many times we sent a request to Gemini (usually 2: one where Gemini
#                    asks for the search, one where it writes the answer)
#   function_calls = how many times Gemini called our find_stories function (usually 1)
# The response object the library returns only reports the LAST request, so we add up the
# token numbers ourselves with the hooks below. Only one question runs at a time (see _lock),
# so one set of counters is enough.
_usage: dict = {}


def _reset_usage() -> None:
    _usage.update(model_requests=0, prompt=0, output=0, thinking=0, cached=0, total=0, function_calls=0)
    _seen.clear()
    _timing["started"] = None


# Which usage fields Gemini really sent for this question. A field it never sent is
# logged as "n/a" instead of a misleading 0.
_seen: set = set()
# When the request that is running right now was sent (to time each Gemini request)
_timing: dict = {"started": None}

_reset_usage()


def _mark_request_start(request) -> None:
    """Runs just before every HTTP request to Gemini: remembers the time."""
    _timing["started"] = time.monotonic()


def _count_usage(response) -> None:
    """Runs after every HTTP response from Gemini: logs how long this request took and adds
    its token usage to _usage. It must never break a request, so any problem here is ignored."""
    try:
        response.read()
        started, _timing["started"] = _timing["started"], None
        seconds = f"{time.monotonic() - started:.2f}s" if started is not None else "n/a"
        _usage["model_requests"] += 1

        try:
            usage = response.json().get("usageMetadata")
        except Exception:
            usage = None  # not JSON, or not the shape we expect
        counts = {}
        if isinstance(usage, dict):
            for key, field in (("prompt", "promptTokenCount"), ("output", "candidatesTokenCount"),
                               ("thinking", "thoughtsTokenCount"), ("cached", "cachedContentTokenCount"),
                               ("total", "totalTokenCount")):
                if isinstance(usage.get(field), int):
                    _usage[key] += usage[field]
                    _seen.add(key)
                    counts[key] = usage[field]

        # One line per request, so you can see which one is slow
        logger.info("Gemini model_request=%d time=%s status=%s prompt=%s output=%s",
                    _usage["model_requests"], seconds, response.status_code,
                    counts.get("prompt", "n/a"), counts.get("output", "n/a"))
    except Exception:
        pass


def _log_usage(force: bool = False) -> None:
    """Write one line to the log, e.g.
    usage: model=gemini-3.6-flash prompt=3203 output=163 function_calls=1 model_requests=2 total=3366
    A value Gemini did not send shows as n/a. Nothing is written when Gemini reported no token
    numbers at all (a failed request), unless force=True (used when the question succeeded)."""
    if not _seen and not force:
        return

    def show(key):
        return _usage[key] if key in _seen else "n/a"

    parts = [f"model={GEMINI_MODEL}", f"prompt={show('prompt')}", f"output={show('output')}",
             f"function_calls={_usage['function_calls']}", f"model_requests={_usage['model_requests']}"]
    if _usage["thinking"]:
        parts.append(f"thinking={_usage['thinking']}")
    if _usage["cached"]:
        parts.append(f"cached={_usage['cached']}")
    parts.append(f"total={show('total')}")
    logger.info("usage: %s", " ".join(parts))


def _http_options(**extra) -> types.HttpOptions:
    """Settings for talking to Gemini: the timeout, and the hooks that time each request
    and count token usage."""
    return types.HttpOptions(
        timeout=GEMINI_TIMEOUT_MS,
        client_args={"event_hooks": {"request": [_mark_request_start], "response": [_count_usage]}},
        **extra,
    )


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise SystemExit("Thiếu GEMINI_API_KEY. Hãy điền vào file .env.")
    return genai.Client(api_key=GEMINI_API_KEY, http_options=_http_options())


# Conversation memory, in memory only (lost when the bot restarts):
# chat_id -> list of exchanges (oldest first). One exchange is the few messages of one question.
# One lock so two messages never run at once.
_history: dict = {}
_lock = threading.Lock()

# The only arguments find_stories accepts (anything else Gemini invents is dropped)
_FIND_ARGS = ("tags", "reading_status", "status", "max_chapters", "author", "next_page")
_UNDERSTAND_ERROR = "Mình chưa hiểu rõ điều kiện tìm. Bạn thử nói lại cụ thể hơn nhé."


def _clean_args(args: dict) -> Optional[dict]:
    """Check the arguments Gemini chose and fix harmless type slips. None = unusable."""
    clean = {k: args[k] for k in _FIND_ARGS if args.get(k) is not None}
    tags = clean.get("tags")
    if isinstance(tags, str):
        clean["tags"] = tags = [tags]
    if tags is not None and not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        return None
    for key in ("reading_status", "status", "author"):
        if key in clean and not isinstance(clean[key], str):
            return None
    if "max_chapters" in clean:
        value = clean["max_chapters"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            return None
        clean["max_chapters"] = int(value)
    if "next_page" in clean:
        clean["next_page"] = bool(clean["next_page"])
    return clean


def _text_of(content) -> str:
    """The plain text Gemini wrote (empty when it only asked for a function call)."""
    parts = (content.parts if content else None) or []
    return "".join(p.text for p in parts if p.text and not p.thought).strip()


def _run_search(call) -> tuple:
    """Run find_stories with the arguments Gemini chose.
    Returns (text for the user, small result for the memory, one line for the memory)."""
    args = _clean_args(dict(call.args or {})) if call.name == "find_stories" else None
    if args is None:
        logger.warning("Gemini asked for an unusable function call: name=%s", call.name)
        return _UNDERSTAND_ERROR, {"error": _UNDERSTAND_ERROR}, _UNDERSTAND_ERROR
    result = find_stories(**args)
    if "error" in result:  # a wrong tag/value, or "xem thêm" with nothing to continue
        return result["error"], {"error": result["error"]}, result["error"]
    filters, continued = result["filters"], result["continued"]
    note = render.history_note(result, filters, continued)
    # The memory gets only the numbers, never the stories
    brief = {k: result[k] for k in ("total", "skip", "shown", "remaining")}
    return render.render_result(result, filters, continued), brief, note


def answer_question(chat_id: int, question: str) -> str:
    """Send the question to Gemini (ONE request). Gemini only picks the search conditions;
    our code queries Notion and writes the list. The chat_id keeps each conversation
    separate, so follow-up questions work."""
    global _current_chat_id
    with _lock:
        _current_chat_id = chat_id  # find_stories reads this to keep one search state per chat
        exchanges = _history.setdefault(chat_id, [])
        user = types.Content(role="user", parts=[types.Part(text=question)])
        contents = [message for exchange in exchanges for message in exchange] + [user]

        _reset_usage()
        answered = False
        try:
            response = _client().models.generate_content(model=GEMINI_MODEL, contents=contents, config=_config())
            answered = True
            content = response.candidates[0].content if response.candidates else None
            calls = [p for p in ((content.parts if content else None) or []) if p.function_call]

            if calls:
                if len(calls) > 1:
                    logger.warning("Gemini asked for %d function calls at once; only the first is used", len(calls))
                call = calls[0].function_call
                text, brief, note = _run_search(call)
                # Memory of this exchange: the question, the call Gemini made (kept exactly as it
                # sent it, as the API wants), the numbers we got back, and one line about what was shown
                kept = []
                for part in content.parts:
                    kept.append(part)
                    if part.function_call:
                        break
                exchange = [
                    user,
                    types.Content(role="model", parts=kept),
                    types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
                        id=call.id, name=call.name, response=brief))]),
                    types.Content(role="model", parts=[types.Part(text=note)]),
                ]
            else:
                # No search: Gemini answered with a short text (a question back, or "I only search stories")
                text = _text_of(content) or "Mình chưa tạo được câu trả lời. Bạn thử hỏi lại nhé."
                logger.info("Gemini answered without a function call")
                exchange = [user, types.Content(role="model", parts=[types.Part(text=text)])]
        finally:
            # Also when the question failed half-way (those tokens were still used).
            # After a successful Gemini request the line is always written, with n/a for anything missing.
            _log_usage(force=answered)

        exchanges.append(exchange)
        del exchanges[:-MAX_EXCHANGES]  # forget the oldest questions
    return text
