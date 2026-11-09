import asyncio
import logging
import os
import time

import httpx
from dotenv import load_dotenv
from google.genai import errors as gemini_errors
from notion_client import errors as notion_errors
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    import httpx2  # google-genai uses this library: its network errors are httpx2 errors, not httpx ones
except ImportError:  # (only if a future google-genai stops using it)
    httpx2 = None

import logging_setup
from flows import build_flows
from services.gemini_agent import answer_question

# Logging is set up in logging_setup.py: terminal + logs/bot.log, level from LOG_LEVEL in .env,
# a req_id on every line of a question, and secrets hidden. Keep httpx quiet there (URLs hold tokens).
logging_setup.setup_logging()
logger = logging.getLogger(__name__)

# Read the secrets from the .env file into environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")

# Stop early with a clear message if .env is not filled in properly
if not BOT_TOKEN:
    raise SystemExit("Thiếu BOT_TOKEN. Hãy điền vào file .env (xem .env.example).")
if not TELEGRAM_USER_ID or not TELEGRAM_USER_ID.strip().isdigit():
    raise SystemExit("TELEGRAM_USER_ID phải là một dãy số. Hãy điền vào file .env.")
for key in ("NOTION_TOKEN", "NOTION_DATA_SOURCE_ID", "GEMINI_API_KEY"):
    if not os.getenv(key):
        raise SystemExit(f"Thiếu {key}. Hãy điền vào file .env (xem .env.example).")

# Telegram user IDs are numbers, so convert the text from .env to a number
ALLOWED_USER_ID = int(TELEGRAM_USER_ID.strip())

# Telegram refuses messages longer than 4096 characters
TELEGRAM_MAX_LENGTH = 4000


# Network and timeout errors come from two different libraries (see the import of httpx2)
NETWORK_ERRORS = (httpx.TransportError,) + ((httpx2.TransportError,) if httpx2 else ())
TIMEOUT_ERRORS = (httpx.TimeoutException,) + ((httpx2.TimeoutException,) if httpx2 else ())


def friendly_error(error: Exception) -> str:
    """Turn a technical error into a short Vietnamese message that says what to check."""
    if isinstance(error, notion_errors.APIResponseError):
        code = getattr(error, "code", None)
        status = getattr(error, "status", 0) or 0
        if code == "rate_limited" or status == 429:
            return "Notion đang giới hạn tốc độ. Bạn thử lại sau ít phút nhé."
        if status >= 500:
            return "Notion đang gặp sự cố. Bạn thử lại sau nhé."
        if code in ("unauthorized", "restricted_resource", "object_not_found"):
            return ("Mình không đọc được bảng Notion. Hãy kiểm tra NOTION_TOKEN và việc đã "
                    "share bảng cho integration chưa.")
        return f"Notion từ chối yêu cầu (mã lỗi: {code}). Bạn thử lại sau, hoặc xem file logs/bot.log."
    if isinstance(error, notion_errors.RequestTimeoutError):
        return "Notion trả lời quá chậm. Bạn thử lại sau nhé."
    if isinstance(error, gemini_errors.ClientError):
        if error.code == 429:
            return "Gemini đang quá giới hạn sử dụng (hết quota). Bạn thử lại sau ít phút nhé."
        if error.code in (401, 403):
            return "Gemini từ chối khóa API. Hãy kiểm tra GEMINI_API_KEY trong file .env."
        if error.code == 404:
            return "Không tìm thấy model Gemini. Hãy kiểm tra GEMINI_MODEL trong file .env."
        return "Gemini không nhận yêu cầu này. Bạn thử diễn đạt lại nhé."
    if isinstance(error, gemini_errors.ServerError):
        return "Gemini đang gặp sự cố. Bạn thử lại sau ít phút nhé."
    if isinstance(error, TIMEOUT_ERRORS):
        return "Máy chủ trả lời quá chậm (hết thời gian chờ). Bạn thử lại sau nhé."
    if isinstance(error, NETWORK_ERRORS):
        return "Lỗi kết nối mạng. Hãy kiểm tra internet rồi thử lại nhé."
    return "Xin lỗi, mình gặp lỗi khi tìm truyện. Bạn thử lại sau nhé."


def error_source(error: Exception) -> str:
    """Say which part failed, for the log (the user sees friendly_error instead)."""
    if isinstance(error, httpx2.TimeoutException if httpx2 else ()):
        return "timeout (Gemini)"
    if isinstance(error, (httpx.TimeoutException, notion_errors.RequestTimeoutError)):
        return "timeout (Notion)"
    if isinstance(error, notion_errors.NotionClientErrorBase):
        return "Notion error"
    if isinstance(error, gemini_errors.APIError):
        return "Gemini error"
    return "unexpected error"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches errors that no handler dealt with (for example Telegram itself being
    unreachable) and writes ONE short line to the log instead of a long traceback."""
    error = context.error
    if isinstance(error, NetworkError):
        # Only the type is logged: the full text of a Telegram network error can contain the bot token
        logger.warning("Lỗi mạng khi nói chuyện với Telegram (%s). Bot sẽ tự thử lại.", type(error).__name__)
    else:
        logger.error("Lỗi không xử lý được", exc_info=error)


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs when you send /ping. Replies with 'pong'."""
    await update.message.reply_text("pong")


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs for any normal text message: asks Gemini (which searches Notion) and replies."""
    text = update.message.text
    # A new short id for this question: it appears in every log line of the question
    logging_setup.new_req_id()
    started = time.monotonic()
    status = "error"  # becomes "ok" only if the answer was made AND sent to Telegram
    # Only the length and the first 50 characters of the question are logged (repr hides line breaks)
    logger.info("Incoming: user=%s length=%d text=%r",
                logging_setup.mask_user_id(update.effective_user.id), len(text), text[:50])
    try:
        # Show "typing..." while we work
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        answered = False
        try:
            # answer_question waits for the network, so run it in a separate thread
            # to keep the bot responsive
            # The chat id keeps this conversation's history separate (for follow-up questions)
            answer = await asyncio.to_thread(
                answer_question, update.effective_chat.id, text
            )
            answered = True
        except Exception as error:
            # Full details (with traceback) go to the terminal and bot.log; you get a short, clear message
            logger.exception("Failed [%s] %s: %s", error_source(error), type(error).__name__, error)
            answer = friendly_error(error)
        reply = answer[:TELEGRAM_MAX_LENGTH]
        await update.message.reply_text(reply)
        # Only reached when Telegram accepted the message (no line = the reply did not get through)
        logger.info("Reply sent: chars=%d", len(reply))
        status = "ok" if answered else "error"
    finally:
        logger.info("Done: status=%s total_time=%.2fs", status, time.monotonic() - started)
        logging_setup.clear_req_id()


async def reject_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs for messages from anyone who is NOT you. It only writes a warning to the log
    (user id hidden) and sends NO reply, so strangers still get no sign of life."""
    user = update.effective_user
    logger.warning("Blocked: message from a user that is not allowed (user=%s)",
                   logging_setup.mask_user_id(user.id) if user else "unknown")


def main() -> None:
    # Create the bot application using the token
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)

    # Only messages from the allowed user get through; everyone else is ignored
    only_me = filters.User(user_id=ALLOWED_USER_ID)

    # /ping command -> ping()
    app.add_handler(CommandHandler("ping", ping, filters=only_me))
    # /add (add a story) and /edit (change a story): the bot asks one question at a time.
    # Registered BEFORE the free-text handler below, so answers go here and not to Gemini.
    app.add_handler(build_flows(only_me, ALLOWED_USER_ID, friendly_error))
    # Any text that is not a command -> handle_question()
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, handle_question))
    # Anything from someone else: log a warning only (group 1 = a separate list of handlers)
    app.add_handler(MessageHandler(~only_me, reject_stranger), group=1)

    print("Bot đang chạy... Nhấn Ctrl+C để dừng.")
    # Keep asking Telegram "any new messages?" until you stop the program
    app.run_polling()


if __name__ == "__main__":
    main()
