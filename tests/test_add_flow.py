"""Test of the /add conversation WITHOUT Telegram and WITHOUT writing to Notion.
It sends scripted messages and button presses to the real handlers, using a fake
Telegram connection and a fake Notion that only records what would be sent.
Run with:  python -m tests.test_add_flow
(The only real Notion call is reading the tag list, which is read-only.)"""

import asyncio
import json
import logging
import os
import tempfile

import httpx
from notion_client import errors as notion_errors
from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from telegram.request import BaseRequest

from services import notion_search
from services import notion_write as w
from flows.add_flow import build_add_handler

# Some tests cause errors on purpose; hide the bot's error logs so the output stays clean
logging.disable(logging.CRITICAL)

OWNER, STRANGER = 111, 222
failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


# ---- fake Telegram connection -------------------------------------------------

class FakeRequest(BaseRequest):
    """Replaces the internet connection: records what the bot sends, answers 'ok'."""

    def __init__(self):
        self.calls = []  # (method, parameters)
        self.keyboards = []  # callback_data of the buttons of every keyboard the bot sent
        self._message_id = 1000

    @property
    def read_timeout(self):
        return 5.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((name, params))
        markup = params.get("reply_markup")
        if markup:
            markup = json.loads(markup) if isinstance(markup, str) else markup
            self.keyboards.append([b["callback_data"] for row in markup["inline_keyboard"] for b in row])
        if name == "getMe":
            result = {"id": 999, "is_bot": True, "first_name": "Bot", "username": "bot"}
        elif name in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            self._message_id += 1
            result = {"message_id": params.get("message_id", self._message_id), "date": 1700000000,
                      "chat": {"id": params.get("chat_id", OWNER), "type": "private"},
                      "text": params.get("text", "")}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


# ---- fake Notion --------------------------------------------------------------

IDS = w._tag_ids()          # real tag list (read-only), loaded before faking
VALID_TAGS = notion_search.get_valid_tags()
TABLE_ID = notion_search.NOTION_DATA_SOURCE_ID


def stored_story(name, link, page_id="old-1"):
    return {"object": "page", "id": page_id, "url": f"https://notion.so/{page_id}",
            "properties": {
                "Name": {"title": [{"plain_text": name}]},
                "Author": {"rich_text": [{"plain_text": "Ai Đó"}]},
                "Chapters": {"number": 20},
                "Tags": {"multi_select": [{"name": "Sủng"}]},
                "Link": {"url": link},
            }}


class FakeNotion:
    def __init__(self):
        self.created = []
        self.existing = []          # what a table query returns
        self.query_error = None
        self.create_error = None
        self.pages = self
        self.data_sources = self

    def query(self, **kw):
        if self.query_error:
            raise self.query_error
        return {"results": self.existing, "has_more": False, "next_cursor": None}

    def create(self, **kw):
        if self.create_error:
            raise self.create_error
        self.created.append(kw)
        return {"id": "new-page", "url": "https://notion.so/new-page"}


notion = FakeNotion()
w._client = lambda: notion
notion_search._client = lambda: notion
w.WRITE_LOG_PATH = os.path.join(tempfile.mkdtemp(), "writes.log")


# ---- driver: sends updates, reads what the bot answered -------------------------

only_me = filters.User(user_id=OWNER)
reached_gemini = []   # texts that fell through to the (stubbed) Gemini handler


async def gemini_stub(update, context):
    reached_gemini.append(update.message.text)


def friendly_error_stub(error):
    return f"[lỗi thân thiện: {type(error).__name__}]"


class Chat:
    def __init__(self, app, request):
        self.app, self.request, self.n = app, request, 0
        self.mark = 0
        self.last_message_id = 1

    def _next(self):
        self.n += 1
        return self.n

    async def say(self, text, user=OWNER):
        message = {"message_id": self._next(), "date": 1700000000,
                   "chat": {"id": user, "type": "private"},
                   "from": {"id": user, "is_bot": False, "first_name": "U"}, "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.app.process_update(Update.de_json({"update_id": self._next(), "message": message}, self.app.bot))

    async def press(self, data, user=OWNER):
        message = {"message_id": self.last_message_id, "date": 1700000000, "chat": {"id": user, "type": "private"}}
        query = {"id": str(self._next()), "from": {"id": user, "is_bot": False, "first_name": "U"},
                 "chat_instance": "x", "data": data, "message": message}
        await self.app.process_update(Update.de_json({"update_id": self._next(), "callback_query": query}, self.app.bot))

    def confirm_data(self):
        """callback_data of the 'Confirm' button in the most recent summary."""
        for keyboard in reversed(self.request.keyboards):
            for data in keyboard:
                if data.startswith("add:confirm:"):
                    return data
        raise AssertionError("no confirm button was sent")

    # What the bot did since the last time we looked
    def new_calls(self):
        calls = self.request.calls[self.mark:]
        self.mark = len(self.request.calls)
        return calls

    def texts(self, calls):
        return [p.get("text", "") for name, p in calls if name in ("sendMessage", "editMessageText")]

    def buttons(self, calls):
        """callback_data of the buttons in the last keyboard the bot sent."""
        found = []
        for name, p in calls:
            markup = p.get("reply_markup")
            if markup:
                markup = json.loads(markup) if isinstance(markup, str) else markup
                found = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        return found

    def labels(self, calls):
        found = []
        for name, p in calls:
            markup = p.get("reply_markup")
            if markup:
                markup = json.loads(markup) if isinstance(markup, str) else markup
                found = [b["text"] for row in markup["inline_keyboard"] for b in row]
        return found

    def alerts(self, calls):
        return [p for name, p in calls if name == "answerCallbackQuery" and p.get("show_alert")]


async def new_chat():
    request = FakeRequest()
    app = Application.builder().token("1:TEST").request(request).build()
    app.add_handler(build_add_handler(only_me, OWNER, friendly_error_stub))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, gemini_stub))
    await app.initialize()
    chat = Chat(app, request)
    chat.new_calls()  # forget getMe
    return chat


def tag_button(name: str) -> str:
    return f"add:tag:{[t.strip() for t in VALID_TAGS].index(name)}"


async def main():
    # ---------------- happy path ----------------
    print("--- happy path: all steps, then confirm ---")
    chat = await new_chat()
    await chat.say("/add")
    out = chat.texts(chat.new_calls())
    check("/add asks for the name first", any("1/6" in t and "TÊN" in t for t in out))

    await chat.say("Truyện thử nghiệm")
    check("then asks for the link", any("2/6" in t for t in chat.texts(chat.new_calls())))
    await chat.say("example.com/abc")
    check("a link without http is refused and re-asked", any("http" in t and "Nhập lại LINK" in t for t in chat.texts(chat.new_calls())))
    await chat.say("https://example.com/abc")
    check("then asks for the author (no duplicate found)", any("3/6" in t for t in chat.texts(chat.new_calls())))
    await chat.say("Tác Giả Thử")
    check("then asks for the chapters", any("4/6" in t for t in chat.texts(chat.new_calls())))
    await chat.say("abc")
    check("chapters 'abc' is refused", any("Nhập lại SỐ CHƯƠNG" in t for t in chat.texts(chat.new_calls())))
    await chat.say("0")
    check("chapters 0 is refused", any("Nhập lại SỐ CHƯƠNG" in t for t in chat.texts(chat.new_calls())))
    await chat.say("45")
    calls = chat.new_calls()
    check("then shows a button for every tag", len([b for b in chat.buttons(calls) if b.startswith("add:tag:")]) == len(VALID_TAGS))

    await chat.say("xin chào, đây là chữ")
    calls = chat.new_calls()
    check("typing text while buttons are expected is NOT sent to Gemini", reached_gemini == [])
    check("... and the bot says to use the buttons", any("bấm một nút" in t for t in chat.texts(calls)))

    await chat.press("add:tags_done")
    calls = chat.new_calls()
    check("'Done' with no tag chosen shows an alert", len(chat.alerts(calls)) == 1)
    await chat.press(tag_button("Sủng"))
    await chat.press(tag_button("Cổ Đại"))
    calls = chat.new_calls()
    check("selected tags are marked with a tick", any(l.startswith("✅") and "Cổ Đại" in l for l in chat.labels(calls)))
    check("selected tags are listed in the message", any("Sủng, Cổ Đại" in t for t in chat.texts(calls)))
    await chat.press(tag_button("Sủng"))      # un-select
    await chat.press(tag_button("Ngọt"))
    chat.new_calls()
    await chat.press("add:tags_done")
    calls = chat.new_calls()
    check("then asks for the status with 3 choices", sorted(b for b in chat.buttons(calls) if b.startswith("add:status:"))
          == ["add:status:Drop", "add:status:Full", "add:status:In progress"])
    await chat.press("add:status:Full")
    out = "\n".join(chat.texts(chat.new_calls()))
    check("summary shows everything the user gave", all(x in out for x in (
        "Truyện thử nghiệm", "https://example.com/abc", "Tác Giả Thử", "45", "Cổ Đại, Ngọt", "Status: Full")))
    check("summary shows the defaults", "Reading Status: Not started" in out and "Record Audio: Not started" in out)
    check("NOTHING is written before the confirm button", notion.created == [])

    confirm_button = chat.confirm_data()
    check("the confirm button carries a one-time code", confirm_button.startswith("add:confirm:") and len(confirm_button) > len("add:confirm:"))
    await chat.press(confirm_button)
    out = "\n".join(chat.texts(chat.new_calls()))
    check("exactly one story is created", len(notion.created) == 1)
    props = notion.created[0]["properties"]
    check("created in the novel table", notion.created[0]["parent"]["data_source_id"] == TABLE_ID)
    check("values written are right", props["Name"]["title"][0]["text"]["content"] == "Truyện thử nghiệm"
          and props["Link"]["url"] == "https://example.com/abc" and props["Chapters"]["number"] == 45
          and props["Author"]["rich_text"][0]["text"]["content"] == "Tác Giả Thử")
    check("tags written are the chosen ones (Cổ Đại, Ngọt)",
          [t["id"] for t in props["Tags"]["multi_select"]] == [IDS[" Cổ Đại"], IDS["Ngọt"]])
    check("defaults: Status Full, Reading Status and Record Audio Not started",
          props["Status"]["status"]["name"] == "Full" and props["Reading Status "]["status"]["name"] == "Not started"
          and props["Record Audio"]["status"]["name"] == "Not started")
    check("Note and Date are not sent", "Note" not in props and "Date" not in props)
    check("tells the user it worked, with the link", "Đã thêm" in out and "notion.so/new-page" in out)
    await chat.press(confirm_button)
    check("pressing confirm a second time writes nothing more", len(notion.created) == 1)

    # ---------------- other status ----------------
    print("--- status can be changed ---")
    notion.created.clear()
    chat = await new_chat()
    for step in ("/add", "Tên", "https://example.com/s", "Tác giả", "10"):
        await chat.say(step)
    await chat.press(tag_button("Hài hước"))
    await chat.press("add:tags_done")
    await chat.press("add:status:In progress")
    await chat.press(chat.confirm_data())
    check("status In progress is written", notion.created[0]["properties"]["Status"]["status"]["name"] == "In progress")

    # ---------------- duplicates ----------------
    print("--- duplicate warning ---")
    notion.created.clear()
    notion.existing = [stored_story("TRUYỆN thử  nghiệm", "http://www.example.com/abc/")]
    chat = await new_chat()
    for step in ("/add", "truyện thử nghiệm", "https://example.com/abc"):
        await chat.say(step)
    calls = chat.new_calls()
    out = "\n".join(chat.texts(calls))
    check("warns about the existing story", "đã có truyện giống" in out and "TRUYỆN thử  nghiệm" in out and "trùng tên và link" in out)
    check("offers 'add anyway' and cancel", chat.buttons(calls) == ["add:dup_yes", "add:cancel"])
    await chat.say("gõ chữ lung tung")
    check("text at the warning is not sent to Gemini", reached_gemini == [])
    await chat.press("add:dup_yes")
    check("'add anyway' continues with the author", any("3/6" in t for t in chat.texts(chat.new_calls())))

    chat = await new_chat()
    for step in ("/add", "truyện thử nghiệm", "https://example.com/abc"):
        await chat.say(step)
    chat.new_calls()
    await chat.press("add:cancel")
    out = "\n".join(chat.texts(chat.new_calls()))
    check("cancelling at the warning writes nothing", "Đã hủy" in out and notion.created == [])
    notion.existing = []

    # ---------------- cancel ----------------
    print("--- cancel ---")
    chat = await new_chat()
    for step in ("/add", "Tên", "https://example.com/c", "Tác giả"):
        await chat.say(step)
    chat.new_calls()
    await chat.say("/cancel")
    check("/cancel stops the conversation", any("Đã hủy" in t for t in chat.texts(chat.new_calls())))
    await chat.say("45")
    check("after cancelling, text goes back to Gemini", reached_gemini == ["45"])
    reached_gemini.clear()

    print("--- /add again in the middle starts over ---")
    chat = await new_chat()
    for step in ("/add", "Tên cũ", "https://example.com/d"):
        await chat.say(step)
    chat.new_calls()
    await chat.say("/add")
    check("restarts at step 1", any("1/6" in t for t in chat.texts(chat.new_calls())))

    # ---------------- strangers ----------------
    print("--- someone who is not the owner ---")
    chat = await new_chat()
    await chat.say("/add", user=STRANGER)
    check("a stranger gets no reply to /add", chat.new_calls() == [])
    await chat.say("hello", user=STRANGER)
    check("a stranger's text reaches nothing", chat.new_calls() == [] and reached_gemini == [])
    notion.created.clear()
    await chat.press("add:confirm:abcd", user=STRANGER)
    check("a stranger pressing confirm writes nothing", notion.created == [])

    # ---------------- errors ----------------
    print("--- errors ---")
    notion.query_error = httpx.ConnectError("down")
    chat = await new_chat()
    for step in ("/add", "Tên", "https://example.com/e"):
        await chat.say(step)
    out = "\n".join(chat.texts(chat.new_calls()))
    check("duplicate check failing stops /add safely", "lỗi thân thiện: ConnectError" in out and "chưa ghi gì" in out)
    notion.query_error = None

    error = notion_errors.APIResponseError("restricted_resource", 403, "no", httpx.Headers(), "{}")
    notion.create_error = error
    chat = await new_chat()
    for step in ("/add", "Tên", "https://example.com/f", "Tác giả", "5"):
        await chat.say(step)
    await chat.press(tag_button("Sủng"))
    await chat.press("add:tags_done")
    await chat.press("add:status:Full")
    chat.new_calls()
    await chat.press(chat.confirm_data())
    out = "\n".join(chat.texts(chat.new_calls()))
    check("Notion refusing the write explains what to check", "Insert content" in out and "restricted_resource" in out)
    notion.create_error = None
    await chat.say("45")
    check("conversation ended after the error", reached_gemini == ["45"])
    reached_gemini.clear()

    # A Notion error that is NOT about permissions must not send you to check permissions
    for code, status in (("rate_limited", 429), ("internal_server_error", 500)):
        notion.create_error = notion_errors.APIResponseError(code, status, "x", httpx.Headers(), "{}")
        chat = await new_chat()
        for step in ("/add", "Tên", "https://example.com/g", "Tác giả", "5"):
            await chat.say(step)
        await chat.press(tag_button("Sủng"))
        await chat.press("add:tags_done")
        await chat.press("add:status:Full")
        chat.new_calls()
        await chat.press(chat.confirm_data())
        out = "\n".join(chat.texts(chat.new_calls()))
        check(f"{code}: does NOT blame the permission, tells you to check Notion for a duplicate",
              "Insert content" not in out and "lỗi thân thiện" in out and "tránh thêm trùng" in out)
    notion.create_error = None
    reached_gemini.clear()

    print("--- the confirm button cannot be mixed up with an older one ---")
    notion.created.clear()
    chat = await new_chat()
    for step in ("/add", "Truyện CŨ", "https://example.com/old", "Tác giả", "5"):
        await chat.say(step)
    await chat.press(tag_button("Sủng"))
    await chat.press("add:tags_done")
    await chat.press("add:status:Full")
    old_confirm = chat.confirm_data()
    for step in ("/add", "Truyện MỚI", "https://example.com/new", "Tác giả", "6"):   # start again; the old summary is still on screen
        await chat.say(step)
    await chat.press(tag_button("Ngọt"))
    await chat.press("add:tags_done")
    await chat.press("add:status:Full")
    new_confirm = chat.confirm_data()
    chat.new_calls()
    check("each summary has its own code", old_confirm != new_confirm)
    await chat.press(old_confirm)
    calls = chat.new_calls()
    check("the OLD summary's button is refused with an alert and writes nothing", len(chat.alerts(calls)) == 1 and notion.created == [])
    await chat.press(new_confirm)
    check("the new button writes the NEW story, not the old one",
          len(notion.created) == 1 and notion.created[0]["properties"]["Name"]["title"][0]["text"]["content"] == "Truyện MỚI")

    chat = await new_chat()
    await chat.say("/add")
    await chat.say("x" * 2001)
    check("a name that is too long is refused", any("quá dài" in t for t in chat.texts(chat.new_calls())))

    lines = open(w.WRITE_LOG_PATH, encoding="utf-8").read().splitlines()
    check("each real write was logged (3 successful adds)", len(lines) == 3)

    print()
    print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
    raise SystemExit(1 if failures else 0)


asyncio.run(main())
