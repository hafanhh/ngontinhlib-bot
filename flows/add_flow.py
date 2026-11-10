"""The /add conversation: the bot asks for the new story's details one by one.

How it stays safe:
- Gemini is not involved at all. The bot asks fixed questions and checks every answer
  with the same rules as notion_write.py.
- Nothing is written until you press "Confirm" on the final summary.
- A button only works while its conversation is active, so each button press happens once.
- Every handler works only for the owner's Telegram user ID.
"""

import asyncio
import logging
import secrets
import warnings

from notion_client import errors as notion_errors
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from flows import flowlog
from services import notion_search
from services import notion_write as w

logger = logging.getLogger(__name__)

# The steps of the conversation (the bot is always "in" exactly one of them), in order:
#   NAME -> LINK -> (DUPLICATE, only if a similar story exists) -> AUTHOR -> CHAPTERS
#   -> TAGS (tap buttons) -> STATUS (tap a button) -> CONFIRM (press Confirm to write)
NAME, LINK, DUPLICATE, AUTHOR, CHAPTERS, TAGS, STATUS, CONFIRM = range(8)

DATA_KEY = "add"  # where the answers so far are kept (per user, in memory)
TAGS_PER_ROW = 3  # how many tag buttons fit on one row of the keyboard

CANCEL_BUTTON = InlineKeyboardButton("❌ Cancel", callback_data="add:cancel")


def build_add_handler(only_me, owner_id: int, friendly_error) -> ConversationHandler:
    """Create the /add conversation.
    only_me: the filter that lets only the owner's messages through.
    friendly_error: turns an exception into a short Vietnamese message."""

    def is_owner(update: Update) -> bool:
        """True only if the update comes from the owner. A second safety check on top of the
        only_me filter: button presses (callbacks) are checked here too."""
        return update.effective_user is not None and update.effective_user.id == owner_id

    def data(context: ContextTypes.DEFAULT_TYPE) -> dict:
        """The answers collected so far in this /add (kept in memory, one set per user).
        Keys: name, link, author, chapters, tags, valid_tags, status, nonce."""
        return context.user_data.setdefault(DATA_KEY, {})

    def log(context, name: str, text: str = "", *args, **kwargs) -> None:
        """Log one /add step (see flows/flowlog.py)."""
        flowlog.step(logger, context, DATA_KEY, name, text, *args, **kwargs)

    async def drop_buttons(update: Update) -> None:
        """Remove the buttons from the message that was just answered."""
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass  # the message had no buttons any more: nothing to do

    # ---- start -----------------------------------------------------------

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """/add: begin a fresh conversation and ask for the name. Closes /edit if it was open."""
        context.user_data.pop("edit", None)  # leave /edit if it was open (see flows/__init__.py)
        context.user_data[DATA_KEY] = {}  # always start from a clean slate
        number = notion_search.list_number(notion_search.add_source_id())
        where = f" vào List {number}" if number else ""
        flowlog.begin(update, context, DATA_KEY, logger, target_list=number or "?")
        await update.message.reply_text(
            f"Thêm truyện mới{where}. Gõ /cancel bất cứ lúc nào để dừng (chưa ghi gì vào Notion).\n\n"
            "1/6 - Nhập TÊN truyện:"
        )
        return NAME

    # ---- text steps ------------------------------------------------------

    @flowlog.tracked(DATA_KEY)
    async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 1: check the typed name (same rules as the real write), then ask for the link."""
        try:
            # build_column_update applies the same rules as the real write
            w.build_column_update("name", update.message.text)
        except ValueError as error:
            log(context, "name", "rejected (%s)", error)
            await update.message.reply_text(f"{error}\nNhập lại TÊN truyện:")
            return NAME
        data(context)["name"] = update.message.text.strip()
        log(context, "name", "ok length=%d", len(data(context)["name"]))
        await update.message.reply_text("2/6 - Nhập LINK truyện (bắt đầu bằng http:// hoặc https://):")
        return LINK

    @flowlog.tracked(DATA_KEY)
    async def got_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 2: check the link, then look in ALL lists for the same name or link.
        If something similar exists, warn and let the owner decide; otherwise ask for the author."""
        try:
            w.build_column_update("link", update.message.text)
        except ValueError as error:
            log(context, "link", "rejected (%s)", error)
            await update.message.reply_text(f"{error}\nNhập lại LINK:")
            return LINK
        d = data(context)
        d["link"] = update.message.text.strip()
        log(context, "link", "ok host=%s", flowlog.host_of(d["link"]))

        # Now we know the name and the link: is the story already in the table?
        try:
            duplicates = await asyncio.to_thread(notion_search.find_duplicates, d["name"], d["link"])
        except Exception as error:
            logger.exception("Lỗi khi kiểm tra truyện trùng")
            context.user_data.pop(DATA_KEY, None)
            await update.message.reply_text(
                f"{friendly_error(error)}\nMình dừng /add, chưa ghi gì vào Notion."
            )
            return ConversationHandler.END

        found_in = sorted({dup["list"] for dup in duplicates})
        log(context, "duplicate check", "found=%d in_lists=%s%s", len(duplicates), found_in or "-",
            " -> asking the owner" if duplicates else "")
        if duplicates:
            lines = ["⚠️ Trong bảng đã có truyện giống:"]
            for dup in duplicates[:5]:
                lines.append(f"- {dup['name']} | {dup['author']} | {dup['chapters']} chương "
                             f"(trùng {' và '.join(dup['same'])}, List {dup['list']})\n  {dup['link']}")
            lines.append("\nBạn vẫn muốn thêm truyện này?")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add anyway", callback_data="add:dup_yes"), CANCEL_BUTTON,
            ]])
            await update.message.reply_text("\n".join(lines), reply_markup=keyboard)
            return DUPLICATE

        await update.message.reply_text("3/6 - Nhập TÁC GIẢ:")
        return AUTHOR

    @flowlog.tracked(DATA_KEY)
    async def dup_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """The owner pressed 'Add anyway' after the duplicate warning: carry on with the author."""
        if not is_owner(update):
            return DUPLICATE
        await update.callback_query.answer()
        await drop_buttons(update)
        log(context, "duplicate", "owner chose to add anyway")
        await update.effective_message.reply_text("3/6 - Nhập TÁC GIẢ:")
        return AUTHOR

    @flowlog.tracked(DATA_KEY)
    async def got_author(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 3: check the author, then ask for the number of chapters."""
        try:
            w.build_column_update("author", update.message.text)
        except ValueError as error:
            log(context, "author", "rejected (%s)", error)
            await update.message.reply_text(f"{error}\nNhập lại TÁC GIẢ:")
            return AUTHOR
        data(context)["author"] = update.message.text.strip()
        log(context, "author", "ok length=%d", len(data(context)["author"]))
        await update.message.reply_text("4/6 - Nhập SỐ CHƯƠNG (số nguyên, từ 1 trở lên):")
        return CHAPTERS

    @flowlog.tracked(DATA_KEY)
    async def got_chapters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 4: check the chapter count, load the tags of the list we add to,
        and show the tag buttons."""
        try:
            chapters = w.parse_chapters(update.message.text)
        except ValueError as error:
            log(context, "chapters", "rejected (%s)", error)
            await update.message.reply_text(f"{error}\nNhập lại SỐ CHƯƠNG:")
            return CHAPTERS
        d = data(context)
        d["chapters"] = chapters
        log(context, "chapters", "ok value=%d", chapters)
        d["tags"] = []

        try:
            d["valid_tags"] = await asyncio.to_thread(  # tags of the list we add to
                notion_search.get_valid_tags, notion_search.add_source_id()
            )
        except Exception as error:
            logger.exception("Lỗi khi lấy danh sách tag")
            context.user_data.pop(DATA_KEY, None)
            await update.message.reply_text(
                f"{friendly_error(error)}\nMình dừng /add, chưa ghi gì vào Notion."
            )
            return ConversationHandler.END

        log(context, "tags", "buttons shown: %d tags available", len(d["valid_tags"]))
        await update.message.reply_text(tags_text(d), reply_markup=tags_keyboard(d))
        return TAGS

    # ---- tags (tap buttons to select) --------------------------------------

    def tags_text(d: dict) -> str:
        """The text above the tag buttons, with the tags chosen so far."""
        chosen = ", ".join(t.strip() for t in d["tags"]) or "(chưa chọn)"
        return ("5/6 - Chọn TAGS: bấm để chọn hoặc bỏ chọn, xong thì bấm \"Done\".\n"
                f"Đã chọn: {chosen}")

    def tags_keyboard(d: dict) -> InlineKeyboardMarkup:
        """The tag buttons: one per tag of the add list (✅ on the chosen ones),
        then Done and Cancel."""
        buttons = []
        for index, tag in enumerate(d["valid_tags"]):
            mark = "✅ " if tag in d["tags"] else ""
            buttons.append(InlineKeyboardButton(mark + tag.strip(), callback_data=f"add:tag:{index}"))
        rows = [buttons[i:i + TAGS_PER_ROW] for i in range(0, len(buttons), TAGS_PER_ROW)]
        rows.append([InlineKeyboardButton("✔️ Done", callback_data="add:tags_done"), CANCEL_BUTTON])
        return InlineKeyboardMarkup(rows)

    @flowlog.tracked(DATA_KEY)
    async def toggle_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """A tag button was tapped: select the tag, or unselect it if it was already chosen."""
        if not is_owner(update):
            return TAGS
        query = update.callback_query
        d = data(context)
        await query.answer()
        try:
            tag = d["valid_tags"][int(query.data.split(":")[2])]
        except (IndexError, ValueError, KeyError):
            return TAGS
        if tag in d["tags"]:
            d["tags"].remove(tag)
        else:
            d["tags"].append(tag)
        try:
            await query.edit_message_text(tags_text(d), reply_markup=tags_keyboard(d))
        except BadRequest:
            pass
        return TAGS

    @flowlog.tracked(DATA_KEY)
    async def tags_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """'Done' pressed on the tags: at least one tag is needed, then ask for the status."""
        if not is_owner(update):
            return TAGS
        query = update.callback_query
        d = data(context)
        if not d.get("tags"):
            log(context, "tags", "Done pressed with no tag chosen")
            await query.answer("Hãy chọn ít nhất 1 tag.", show_alert=True)
            return TAGS
        await query.answer()
        log(context, "tags", "done, chosen=%d", len(d["tags"]))
        chosen = ", ".join(t.strip() for t in d["tags"])
        await query.edit_message_text(f"5/6 - Tags: {chosen}")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Full - completed (default)", callback_data="add:status:Full")],
            [InlineKeyboardButton("In progress - ongoing", callback_data="add:status:In progress")],
            [InlineKeyboardButton("Drop - abandoned", callback_data="add:status:Drop")],
            [CANCEL_BUTTON],
        ])
        await query.message.reply_text("6/6 - Chọn STATUS (tình trạng của truyện):", reply_markup=keyboard)
        return STATUS

    # ---- status and the final confirmation ------------------------------------

    @flowlog.tracked(DATA_KEY)
    async def got_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """A status button was tapped: show the final summary with Confirm / Cancel.
        Nothing is written to Notion yet."""
        if not is_owner(update):
            return STATUS
        query = update.callback_query
        d = data(context)
        await query.answer()
        d["status"] = query.data.split(":", 2)[2]
        log(context, "status", "%s", d["status"])
        await query.edit_message_text(f"6/6 - Status: {d['status']}")

        summary = (
            "Kiểm tra lại truyện sắp thêm vào Notion:\n\n"
            f"Tên: {d['name']}\n"
            f"Link: {d['link']}\n"
            f"Tác giả: {d['author']}\n"
            f"Số chương: {d['chapters']}\n"
            f"Tags: {', '.join(t.strip() for t in d['tags'])}\n"
            f"Status: {d['status']}\n"
            "Reading Status: Not started (mặc định)\n"
            "Record Audio: Not started (mặc định)\n"
            "Ghi chú, Ngày: để trống\n\n"
            "Thêm truyện này?"
        )
        # A one-time code in the button: an old summary's button (from an earlier /add that
        # was left open) can never confirm this new data, and a used button is dead.
        d["nonce"] = secrets.token_hex(4)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"add:confirm:{d['nonce']}"), CANCEL_BUTTON,
        ]])
        log(context, "summary", "shown, waiting for Confirm (nothing written yet)")
        await query.message.reply_text(summary, reply_markup=keyboard)
        return CONFIRM

    @flowlog.tracked(DATA_KEY)
    async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """The ONLY place that writes. Runs when the owner presses Confirm on the summary.
        The button's one-time code is checked and used up BEFORE the write, so an old or
        double-pressed button can never add the story twice."""
        if not is_owner(update):
            return CONFIRM
        query = update.callback_query
        d = data(context)
        if not d.get("nonce") or query.data.split(":")[2] != d["nonce"]:
            logger.warning("/add confirm refused: this button is old or was already used")
            await query.answer("Nút này đã cũ, hãy dùng tin nhắn mới nhất.", show_alert=True)
            return CONFIRM
        await query.answer()
        await drop_buttons(update)  # so the button cannot be pressed twice
        d = context.user_data.pop(DATA_KEY, {})  # using up the code BEFORE the write
        reply = update.effective_message.reply_text
        logger.info("/add confirm: writing to Notion (list %s)",
                    notion_search.list_number(notion_search.add_source_id()) or "?")
        try:
            result = await asyncio.to_thread(
                w.add_story, d["name"], d["author"], d["chapters"], d["tags"], d["link"], d["status"]
            )
        except ValueError as error:
            await reply(f"Không thêm được: {error}\nChưa ghi gì vào Notion.")
        except notion_errors.APIResponseError as error:
            logger.exception("Notion từ chối việc thêm truyện")
            if getattr(error, "code", None) in ("restricted_resource", "unauthorized"):
                await reply(
                    "Notion từ chối việc thêm truyện (mã lỗi: "
                    f"{error.code}). Hãy kiểm tra integration đã được bật "
                    "quyền \"Insert content\" chưa. Hãy xem trong Notion để chắc là chưa có truyện này."
                )
            else:  # rate limit, Notion outage, ...: not a permission problem
                await reply(f"{friendly_error(error)}\nHãy xem trong Notion truyện đã được thêm chưa "
                            "trước khi thử lại, để tránh thêm trùng.")
        except Exception as error:
            logger.exception("Lỗi khi thêm truyện")
            await reply(f"{friendly_error(error)}\nHãy kiểm tra trong Notion trước khi thử lại, "
                        "để tránh thêm trùng.")
        else:
            logger.info("/add done: added name=%r list=%s id=%s page_id=%s total_time=%.1fs",
                        flowlog.short(result["name"]), result.get("list") or "?", result.get("code") or "-",
                        result["id"], flowlog.elapsed(d))
            where = f" vào List {result['list']}" if result.get("list") else ""
            code = f" (ID {result['code']})" if result.get("code") else ""
            await reply(f"✅ Đã thêm{where}{code}: {result['name']}\n{result['url']}")
        return ConversationHandler.END

    # ---- cancelling and stray messages ----------------------------------------

    @flowlog.tracked(DATA_KEY)
    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """/cancel or the Cancel button: stop /add. Nothing was written."""
        if not is_owner(update):
            return ConversationHandler.END
        d = context.user_data.pop(DATA_KEY, None)
        logger.info("/add cancelled at step '%s' after %.1fs: nothing was written",
                    flowlog.last_step(d), flowlog.elapsed(d))
        if update.callback_query:
            await update.callback_query.answer()
            await drop_buttons(update)
        await update.effective_message.reply_text("Đã hủy. Chưa ghi gì vào Notion.")
        return ConversationHandler.END

    @flowlog.tracked(DATA_KEY)
    async def use_the_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Text typed while the bot waits for a button press. Without this, the text
        would fall through to Gemini."""
        logger.info("/add stray text at step '%s' (ignored: a button was expected)",
                    flowlog.last_step(context.user_data.get(DATA_KEY)))
        await update.message.reply_text("Hãy bấm một nút ở trên, hoặc gõ /cancel để dừng.")

    # ---- put it together --------------------------------------------------------

    text_input = only_me & filters.TEXT & ~filters.COMMAND
    stray_text = MessageHandler(text_input, use_the_buttons)

    # python-telegram-bot prints a general warning about "per_message" here. It does not
    # apply to us (we want one conversation per user, which is the default), so hide it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBUserWarning)
        return ConversationHandler(
            entry_points=[CommandHandler("add", start, filters=only_me)],
            states={
                NAME: [MessageHandler(text_input, got_name)],
                LINK: [MessageHandler(text_input, got_link)],
                DUPLICATE: [CallbackQueryHandler(dup_yes, pattern="^add:dup_yes$"), stray_text],
                AUTHOR: [MessageHandler(text_input, got_author)],
                CHAPTERS: [MessageHandler(text_input, got_chapters)],
                TAGS: [
                    CallbackQueryHandler(toggle_tag, pattern=r"^add:tag:\d+$"),
                    CallbackQueryHandler(tags_done, pattern="^add:tags_done$"),
                    stray_text,
                ],
                STATUS: [CallbackQueryHandler(got_status, pattern="^add:status:"), stray_text],
                CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^add:confirm:\w+$"), stray_text],
            },
            fallbacks=[
                CommandHandler("cancel", cancel, filters=only_me),
                CallbackQueryHandler(cancel, pattern="^add:cancel$"),
            ],
            allow_reentry=True,  # typing /add again in the middle starts over
        )
