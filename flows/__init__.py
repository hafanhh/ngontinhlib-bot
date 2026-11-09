"""/add and /edit as ONE conversation handler.

Why one handler and not two: with two separate conversations, typing /add in the middle
of /edit would start /add while /edit stayed open, and your next answer could be taken
by the wrong one. In one handler the bot is always in exactly one step of one flow, and
typing /add or /edit simply switches to that flow.
"""

import warnings

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler
from telegram.warnings import PTBUserWarning

from flows.add_flow import build_add_handler
from flows.edit_flow import build_edit_handler


def build_flows(only_me, owner_id: int, friendly_error) -> ConversationHandler:
    """Build the /add and /edit flows and join them into one conversation handler."""
    add = build_add_handler(only_me, owner_id, friendly_error)
    edit = build_edit_handler(only_me, owner_id, friendly_error)

    # Each flow has its own /cancel (first fallback) with its own message. Use the right
    # one: the /edit one while /edit is open, otherwise the /add one.
    add_cancel = add.fallbacks[0].callback
    edit_cancel = edit.fallbacks[0].callback

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if "edit" in context.user_data:
            return await edit_cancel(update, context)
        return await add_cancel(update, context)

    # The Hủy button of /add (a button, not a command) stays as it is
    button_fallbacks = [h for h in add.fallbacks + edit.fallbacks if isinstance(h, CallbackQueryHandler)]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBUserWarning)  # same harmless warning as in add_flow.py
        return ConversationHandler(
            entry_points=add.entry_points + edit.entry_points,
            states={**add.states, **edit.states},
            fallbacks=[CommandHandler("cancel", cancel, filters=only_me)] + button_fallbacks,
            allow_reentry=True,  # /add or /edit typed in the middle switches to that flow
        )
