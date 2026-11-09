"""Demo nhỏ để XEM các dòng log, không cần mạng và không dùng key thật.
Chạy:  python -m tests.demo_log

Nó giả lập 5 tình huống và in các dòng log ra terminal:
  1. Câu hỏi bình thường (có kết quả; 1 request Gemini, code tự in danh sách)
  2. Câu hỏi không có kết quả (WARNING 0 results)
  3. Lỗi Gemini (hết quota) - bot vẫn trả lời thân thiện
  4. Người lạ nhắn tin (WARNING Blocked)
  5. Token/key bị che thành ***
Log được ghi vào thư mục tạm, KHÔNG đụng vào logs/bot.log thật của bạn."""

import asyncio
import logging
import os
import tempfile
import time
import types as pytypes

# Token giả, đặt TRƯỚC khi import bot (key thật trong .env không được dùng)
os.environ.update(
    BOT_TOKEN="123456789:FAKE-bot-token-abcdefghijklmnop",
    TELEGRAM_USER_ID="123456789",
    NOTION_TOKEN="secret_FAKEnotiontoken1234567890",
    NOTION_DATA_SOURCE_ID="fake-id",
    GEMINI_API_KEY="AIzaFAKEgeminikey1234567890abcdef",
    LOG_LEVEL="INFO",
)

import httpx2
from google.genai import errors as gemini_errors

import logtools

import bot
from services import gemini_agent as g
from services import render

# Bật logging, file log nằm trong thư mục tạm (không đụng vào logs/bot.log thật)
LOG_FILE = logtools.setup_logging(log_dir=tempfile.mkdtemp())

# ---- Notion giả: trả về 7 truyện, hoặc 0 truyện nếu tag là "Không có" ----
STORY = {"name": "Truyện mẫu", "author": "A", "chapters": 10, "tags": ["Sủng"], "link": "https://example.com"}


def fake_notion(tags=None, **kwargs):
    count = 0 if tags == ["Không có"] else 7
    return {"total": count, "skip": 0, "shown": count, "remaining": 0, "stories": [STORY] * count}


g.notion_search.search_stories = fake_notion
g._current_chat_id = 1


# ---- Telegram giả ----
class FakeMessage:
    def __init__(self, text):
        self.text = text

    async def reply_text(self, text):
        print("   --> bot trả lời Telegram:\n" + "\n".join("       " + line for line in text.splitlines()[:6]))


async def _noop(**kwargs):
    pass


context = pytypes.SimpleNamespace(bot=pytypes.SimpleNamespace(send_chat_action=_noop))


def update_from(user_id, text):
    return pytypes.SimpleNamespace(message=FakeMessage(text), effective_user=pytypes.SimpleNamespace(id=user_id),
                                   effective_chat=pytypes.SimpleNamespace(id=1))


def fake_gemini_request(seconds, prompt, output):
    """Giả lập MỘT request tới Gemini mất `seconds` giây (chạy đúng hook đo thời gian thật)."""
    g._mark_request_start(None)
    time.sleep(seconds)
    g._count_usage(httpx2.Response(200, json={"usageMetadata": {
        "promptTokenCount": prompt, "candidatesTokenCount": output, "totalTokenCount": prompt + output}}))


def make_answer(tag):
    """Giả lập answer_question: Gemini được hỏi MỘT lần (chọn bộ lọc), rồi code tự tìm Notion
    và tự in danh sách (không còn request thứ hai)."""
    def answer(chat_id, text):
        g._reset_usage()
        fake_gemini_request(0.4, 1500, 40)       # request duy nhất: Gemini chọn bộ lọc
        result = g.find_stories(tags=[tag], reading_status="Not started")
        g._log_usage(force=True)
        return render.render_result(result, result["filters"], result["continued"])
    return answer


def failing_answer(chat_id, text):
    raise gemini_errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})


def run(title, user_id, text, answer_function):
    print(f"\n===== {title} =====")
    bot.answer_question = answer_function
    update = update_from(user_id, text)
    handler = bot.handle_question if user_id == 123456789 else bot.reject_stranger
    asyncio.run(handler(update, context))


run("1. Câu hỏi bình thường", 123456789, "Gợi ý truyện Sủng chưa đọc dưới 30 chương", make_answer("Sủng"))
run("2. Không có kết quả", 123456789, "Tìm truyện tag Không có", make_answer("Không có"))
run("3. Lỗi Gemini (hết quota)", 123456789, "Gợi ý truyện hài", failing_answer)
run("4. Người lạ nhắn tin", 987654321, "xin chào", None)

print("\n===== 5. Token bị che =====")
try:
    raise RuntimeError("lỗi gọi https://api.telegram.org/bot" + os.environ["BOT_TOKEN"] + "/getUpdates")
except RuntimeError:
    logging.getLogger("demo").exception("thử ghi token %s và key %s", os.environ["NOTION_TOKEN"], os.environ["GEMINI_API_KEY"])

print("\nXong. Các dòng log ở trên là đúng thứ bạn sẽ thấy trong logs/bot.log khi chạy bot thật.")
print("(Muốn xem file log của demo: " + LOG_FILE + ")")
