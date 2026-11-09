"""The /edit conversation: change one column of one story at a time.

Flow: find the book (type part of its name or author) -> choose it -> choose a column
-> give the new value -> see "before -> after" -> Confirm -> written -> "which column
next?" ... until you press "Done".

How it stays safe:
- Gemini is not involved at all.
- Each change is confirmed and written on its own, one column per write.
- The confirm button carries a one-time code, so an old confirm button (from an
  earlier /edit) can never confirm a different change.
- Only the columns in notion_write.EDITABLE_COLUMNS can be changed; nothing can be deleted.
- Every handler works only for the owner's Telegram user ID.
"""

import asyncio
import logging
import secrets
import warnings

from notion_client import errors as notion_errors
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from services import notion_search
from services import notion_write as w

logger = logging.getLogger(__name__)

# The steps of the conversation (the bot is always "in" exactly one of them)
# (numbered from 100 so they never clash with the /add steps in flows.py)
SEARCH, PICK_BOOK, PICK_COLUMN, VALUE_TEXT, VALUE_TAGS, VALUE_CHOICE, CONFIRM = range(100, 107)

DATA_KEY = "edit"  # where the state of this conversation is kept (per user, in memory)
TAGS_PER_ROW = 3
MAX_SHOWN = 80     # long values (like notes) are shortened in lists

# Columns that are chosen from a fixed list of values, with a short hint for each value
CHOICES = {
    "status": w.STATUS_VALUES,
    "reading_status": w.READING_STATUS_VALUES,
    "record_audio": w.RECORD_AUDIO_VALUES,
}
HINTS = {
    "status": {"Full": "completed", "In progress": "ongoing", "Drop": "abandoned"},
    "reading_status": {"Not started": "not read yet", "In progress": "reading", "Done": "finished",
                       "Beginning + End": "read only start and end", "Drop": "dropped"},
    "record_audio": {},
}
TEXT_PROMPTS = {
    "name": "Nhập TÊN truyện mới:",
    "author": "Nhập TÁC GIẢ mới:",
    "chapters": "Nhập SỐ CHƯƠNG mới (số nguyên, từ 1 trở lên):",
    "link": "Nhập LINK mới (bắt đầu bằng http:// hoặc https://):",
    "note": "Nhập GHI CHÚ mới (sẽ thay thế ghi chú cũ):",
    "date": "Nhập NGÀY mới (ví dụ 16/03/2024):",
}


def show(value) -> str:
    """A value as text for the user."""
    if value is None or value == "" or value == []:
        return "(trống)"
    text = ", ".join(value) if isinstance(value, list) else str(value)
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    return text if len(text) <= MAX_SHOWN else text[:MAX_SHOWN - 1] + "…"


def same_value(column: str, new, current) -> bool:
    """True if saving `new` would change nothing."""
    if column == "tags":
        return {t.strip().casefold() for t in new} == {t.strip().casefold() for t in (current or [])}
    if column == "chapters":
        return current is not None and float(new) == float(current)
    if column in CHOICES:
        return str(new).strip().casefold() == str(current or "").strip().casefold()
    return (new or "").strip() == (current or "").strip()  # text, link, date (None == empty)


def build_edit_handler(only_me, owner_id: int, friendly_error) -> ConversationHandler:
    """Create the /edit conversation.
    only_me: the filter that lets only the owner's messages through.
    friendly_error: turns an exception into a short Vietnamese message."""

    def is_owner(update: Update) -> bool:
        return update.effective_user is not None and update.effective_user.id == owner_id

    def data(context: ContextTypes.DEFAULT_TYPE) -> dict:
        return context.user_data.setdefault(DATA_KEY, {})

    async def drop_buttons(update: Update) -> None:
        """Remove the buttons from the message that was just answered."""
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass

    async def end_with_error(update: Update, context, error: Exception, what: str) -> int:
        """Stop /edit after a failure, saying clearly what happened."""
        logger.exception(what)
        context.user_data.pop(DATA_KEY, None)
        await update.effective_message.reply_text(f"{friendly_error(error)}\nMình dừng /edit.")
        return ConversationHandler.END

    # ---- 1. find the book ---------------------------------------------------

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop("add", None)  # leave /add if it was open (see flows/__init__.py)
        context.user_data[DATA_KEY] = {}  # always start from a clean slate
        await update.message.reply_text(
            "Sửa truyện. Gõ /cancel bất cứ lúc nào để dừng.\n\n"
            "Nhập một phần TÊN truyện hoặc TÊN TÁC GIẢ để tìm:"
        )
        return SEARCH

    async def got_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        try:
            found = await asyncio.to_thread(notion_search.find_books, text)
        except ValueError as error:
            await update.message.reply_text(str(error))
            return SEARCH
        except Exception as error:
            return await end_with_error(update, context, error, "Lỗi khi tìm truyện để sửa")

        books = found["books"]
        if not books:
            await update.message.reply_text(
                f"Không tìm thấy truyện nào có \"{text}\" trong tên hoặc tác giả.\n"
                "Thử từ khóa khác (gõ đúng dấu tiếng Việt):"
            )
            return SEARCH

        data(context)["books"] = books
        rows = []
        for index, book in enumerate(books):
            label = f"{index + 1}. {book['name']} - {book['author']} ({book['chapters']} chương)"
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"edit:book:{index}")])
        intro = "Chọn truyện cần sửa:"
        if found["has_more"]:
            intro = ("Có hơn 10 truyện khớp, mình chỉ hiện 10 đầu. Chọn truyện cần sửa, "
                     "hoặc gõ từ khóa cụ thể hơn để tìm lại:")
        else:
            intro += " (hoặc gõ từ khóa khác để tìm lại)"
        await update.message.reply_text(intro, reply_markup=InlineKeyboardMarkup(rows))
        return PICK_BOOK

    async def pick_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return PICK_BOOK
        query = update.callback_query
        d = data(context)
        await query.answer()
        try:
            book = d["books"][int(query.data.split(":")[2])]
        except (IndexError, ValueError, KeyError):
            return PICK_BOOK
        try:
            story = await asyncio.to_thread(w.get_story, book["id"])
        except ValueError as error:   # e.g. the story was deleted in Notion meanwhile
            await query.edit_message_text(f"Không sửa được truyện này: {error}")
            return PICK_BOOK
        except Exception as error:
            return await end_with_error(update, context, error, "Lỗi khi đọc truyện")

        d.update(page_id=book["id"], name=story["name"], story=story, changes=[], nonce=None)
        await query.edit_message_text(f"Đã chọn: {story['name']}")
        return await ask_column(update, context, first=True)

    # ---- 2. choose a column ------------------------------------------------------

    async def ask_column(update: Update, context, first: bool = False) -> int:
        d = data(context)
        lines = [f"Truyện: {d['name']}", ""]
        for key, (label, _) in w.EDITABLE_COLUMNS.items():
            lines.append(f"{label}: {show(d['story'][key])}")
        lines.append("")
        lines.append("Sửa cột nào? (bấm \"Done\" nếu không sửa nữa)" if first
                     else "Sửa cột nào nữa? (bấm \"Done\" nếu không)")
        buttons = [InlineKeyboardButton(label, callback_data=f"edit:col:{key}")
                   for key, (label, _) in w.EDITABLE_COLUMNS.items()]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton("✅ Done, no more edits", callback_data="edit:finish")])
        await update.effective_message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        return PICK_COLUMN

    async def pick_column(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return PICK_COLUMN
        query = update.callback_query
        d = data(context)
        column = query.data.split(":")[2]
        if column not in w.EDITABLE_COLUMNS:
            await query.answer()
            return PICK_COLUMN
        await query.answer()
        label = w.EDITABLE_COLUMNS[column][0]
        current = d["story"][column]
        d["column"] = column
        await query.edit_message_text(f"Cột: {label}\nHiện tại: {show(current)}")

        if column == "tags":
            try:
                d["valid_tags"] = await asyncio.to_thread(notion_search.get_valid_tags)
            except Exception as error:
                return await end_with_error(update, context, error, "Lỗi khi lấy danh sách tag")
            d["tag_sel"] = list(current)
            await query.message.reply_text(tags_text(d), reply_markup=tags_keyboard(d))
            return VALUE_TAGS

        if column in CHOICES:
            rows = []
            for index, value in enumerate(CHOICES[column]):
                hint = HINTS[column].get(value)
                text = f"{value} - {hint}" if hint else value
                mark = "● " if value == current else ""
                rows.append([InlineKeyboardButton(mark + text, callback_data=f"edit:choice:{index}")])
            rows.append([InlineKeyboardButton("↩️ Choose another column", callback_data="edit:back")])
            await query.message.reply_text("Chọn giá trị mới:", reply_markup=InlineKeyboardMarkup(rows))
            return VALUE_CHOICE

        row = []
        if column in w.CLEARABLE:
            row.append(InlineKeyboardButton("🗑 Clear value", callback_data="edit:clear"))
        row.append(InlineKeyboardButton("↩️ Choose another column", callback_data="edit:back"))
        await query.message.reply_text(TEXT_PROMPTS[column], reply_markup=InlineKeyboardMarkup([row]))
        return VALUE_TEXT

    async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return VALUE_TEXT
        await update.callback_query.answer()
        await drop_buttons(update)
        return await ask_column(update, context)

    # ---- 3. the new value ------------------------------------------------------------

    async def got_text_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        d = data(context)
        column, text = d["column"], update.message.text.strip()
        try:
            if column == "chapters":
                value = w.parse_chapters(text)
            elif column == "date":
                value = w.parse_date(text)
            else:
                value = text
            w.build_column_update(column, value)  # the same rules as the real write
        except ValueError as error:
            await update.message.reply_text(f"{error}\nNhập lại, hoặc bấm \"Chọn cột khác\" ở trên:")
            return VALUE_TEXT
        return await propose(update, context, value)

    async def clear_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return VALUE_TEXT
        d = data(context)
        await update.callback_query.answer()
        await drop_buttons(update)
        if d.get("column") not in w.CLEARABLE:  # only note and date may be emptied
            return await ask_column(update, context)
        return await propose(update, context, None)

    # Tags: tap buttons to select / deselect

    def tags_text(d: dict) -> str:
        return ("Chọn TAGS mới: bấm để chọn hoặc bỏ chọn, xong thì bấm \"Done\".\n"
                f"Đã chọn: {', '.join(d['tag_sel']) or '(chưa chọn)'}")

    def tags_keyboard(d: dict) -> InlineKeyboardMarkup:
        selected = {t.strip().casefold() for t in d["tag_sel"]}
        buttons = []
        for index, tag in enumerate(d["valid_tags"]):
            mark = "✅ " if tag.strip().casefold() in selected else ""
            buttons.append(InlineKeyboardButton(mark + tag.strip(), callback_data=f"edit:tag:{index}"))
        rows = [buttons[i:i + TAGS_PER_ROW] for i in range(0, len(buttons), TAGS_PER_ROW)]
        rows.append([InlineKeyboardButton("✔️ Done", callback_data="edit:tags_done"),
                     InlineKeyboardButton("↩️ Choose another column", callback_data="edit:back")])
        return InlineKeyboardMarkup(rows)

    async def toggle_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return VALUE_TAGS
        query = update.callback_query
        d = data(context)
        await query.answer()
        try:
            tag = d["valid_tags"][int(query.data.split(":")[2])].strip()
        except (IndexError, ValueError, KeyError):
            return VALUE_TAGS
        selected = [t for t in d["tag_sel"] if t.strip().casefold() == tag.casefold()]
        if selected:
            d["tag_sel"] = [t for t in d["tag_sel"] if t not in selected]
        else:
            d["tag_sel"].append(tag)
        try:
            await query.edit_message_text(tags_text(d), reply_markup=tags_keyboard(d))
        except BadRequest:
            pass
        return VALUE_TAGS

    async def tags_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return VALUE_TAGS
        d = data(context)
        if not d.get("tag_sel"):
            await update.callback_query.answer("Hãy chọn ít nhất 1 tag.", show_alert=True)
            return VALUE_TAGS
        await update.callback_query.answer()
        await drop_buttons(update)
        return await propose(update, context, list(d["tag_sel"]))

    # Fixed-list columns: tap a value

    async def pick_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return VALUE_CHOICE
        query = update.callback_query
        d = data(context)
        await query.answer()
        try:
            value = CHOICES[d["column"]][int(query.data.split(":")[2])]
        except (IndexError, ValueError, KeyError):
            return VALUE_CHOICE
        await drop_buttons(update)
        return await propose(update, context, value)

    # ---- 4. show before -> after and ask to confirm ------------------------------------

    async def propose(update: Update, context, new_value) -> int:
        d = data(context)
        column = d["column"]
        label = w.EDITABLE_COLUMNS[column][0]
        current = d["story"][column]

        nothing_changes = (current in (None, "")) if new_value is None else same_value(column, new_value, current)
        if nothing_changes:
            await update.effective_message.reply_text(
                f"Giá trị mới giống giá trị hiện tại của cột {label}, nên không cần sửa."
            )
            return await ask_column(update, context)

        d["new_value"] = new_value
        d["nonce"] = secrets.token_hex(4)  # makes this confirm button single-use and unmistakable
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"edit:confirm:{d['nonce']}"),
            InlineKeyboardButton("↩️ Keep old value", callback_data="edit:discard"),
        ]])
        await update.effective_message.reply_text(
            f"Sửa truyện: {d['name']}\nCột: {label}\n"
            f"Trước: {show(current)}\nSau: {show(new_value)}\n\nGhi thay đổi này vào Notion?",
            reply_markup=keyboard,
        )
        return CONFIRM

    async def discard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return CONFIRM
        data(context)["nonce"] = None
        await update.callback_query.answer()
        await drop_buttons(update)
        await update.effective_message.reply_text("Đã bỏ thay đổi này. Chưa ghi gì vào Notion.")
        return await ask_column(update, context)

    async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return CONFIRM
        query = update.callback_query
        d = data(context)
        if not d.get("nonce") or query.data.split(":")[2] != d["nonce"]:
            await query.answer("Nút này đã cũ, hãy dùng tin nhắn mới nhất.", show_alert=True)
            return CONFIRM
        await query.answer()
        d["nonce"] = None   # use up the code BEFORE writing, so it cannot run twice
        await drop_buttons(update)

        column, label = d["column"], w.EDITABLE_COLUMNS[d["column"]][0]
        reply = update.effective_message.reply_text
        try:
            result = await asyncio.to_thread(w.update_story, d["page_id"], column, d["new_value"])
        except ValueError as error:   # e.g. the story was deleted in Notion meanwhile
            await reply(f"Không sửa được: {error}\nChưa ghi gì vào Notion.")
            return await ask_column(update, context)
        except notion_errors.APIResponseError as error:
            logger.exception("Notion từ chối việc sửa truyện")
            context.user_data.pop(DATA_KEY, None)
            if getattr(error, "code", None) in ("restricted_resource", "unauthorized"):
                await reply(
                    "Notion từ chối việc sửa truyện (mã lỗi: "
                    f"{error.code}). Hãy kiểm tra integration đã được bật "
                    "quyền \"Update content\" chưa. Mình dừng /edit."
                )
            else:  # rate limit, Notion outage, ...: not a permission problem
                await reply(f"{friendly_error(error)}\nHãy xem trong Notion thay đổi đã được ghi chưa. "
                            "Mình dừng /edit.")
            return ConversationHandler.END
        except Exception as error:
            logger.exception("Lỗi khi sửa truyện")
            context.user_data.pop(DATA_KEY, None)
            await reply(f"{friendly_error(error)}\nHãy xem trong Notion xem thay đổi đã được ghi chưa. "
                        "Mình dừng /edit.")
            return ConversationHandler.END

        d["story"][column] = result["after"]
        if column == "name":
            d["name"] = result["after"]
        d["changes"].append(f"{label}: {show(result['before'])} → {show(result['after'])}")
        await reply(f"✅ Đã cập nhật {label}.")
        return await ask_column(update, context)

    # ---- finishing, cancelling, stray messages -------------------------------------------

    async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return PICK_COLUMN
        d = context.user_data.pop(DATA_KEY, {})
        await update.callback_query.answer()
        await drop_buttons(update)
        changes = d.get("changes", [])
        if changes:
            text = f"Xong. Đã sửa {len(changes)} thay đổi cho \"{d['name']}\":\n- " + "\n- ".join(changes)
        else:
            text = "Xong. Không có gì được sửa."
        await update.effective_message.reply_text(text)
        return ConversationHandler.END

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not is_owner(update):
            return ConversationHandler.END
        done = len(context.user_data.pop(DATA_KEY, {}).get("changes", []))
        note = f" {done} thay đổi đã xác nhận trước đó vẫn được giữ trong Notion." if done else ""
        await update.effective_message.reply_text(
            "Đã dừng /edit. Thay đổi chưa xác nhận thì không được ghi." + note
        )
        return ConversationHandler.END

    async def use_the_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Text typed while the bot waits for a button press. Without this, the text
        would fall through to Gemini."""
        await update.message.reply_text("Hãy bấm một nút ở trên, hoặc gõ /cancel để dừng.")

    # ---- put it together ---------------------------------------------------------------

    text_input = only_me & filters.TEXT & ~filters.COMMAND
    stray_text = MessageHandler(text_input, use_the_buttons)
    back = CallbackQueryHandler(go_back, pattern="^edit:back$")

    # python-telegram-bot prints a general warning about "per_message" here. It does not
    # apply to us (we want one conversation per user, which is the default), so hide it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBUserWarning)
        return ConversationHandler(
            entry_points=[CommandHandler("edit", start, filters=only_me)],
            states={
                SEARCH: [MessageHandler(text_input, got_search)],
                PICK_BOOK: [CallbackQueryHandler(pick_book, pattern=r"^edit:book:\d+$"),
                            MessageHandler(text_input, got_search)],  # typing again = search again
                PICK_COLUMN: [CallbackQueryHandler(pick_column, pattern=r"^edit:col:\w+$"),
                              CallbackQueryHandler(finish, pattern="^edit:finish$"), stray_text],
                VALUE_TEXT: [MessageHandler(text_input, got_text_value),
                             CallbackQueryHandler(clear_value, pattern="^edit:clear$"), back],
                VALUE_TAGS: [CallbackQueryHandler(toggle_tag, pattern=r"^edit:tag:\d+$"),
                             CallbackQueryHandler(tags_done, pattern="^edit:tags_done$"), back, stray_text],
                VALUE_CHOICE: [CallbackQueryHandler(pick_choice, pattern=r"^edit:choice:\d+$"), back, stray_text],
                CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^edit:confirm:\w+$"),
                          CallbackQueryHandler(discard, pattern="^edit:discard$"), stray_text],
            },
            fallbacks=[
                CommandHandler("cancel", cancel, filters=only_me),
            ],
            allow_reentry=True,  # typing /edit again in the middle starts over
        )
