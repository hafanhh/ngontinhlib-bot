"""Test of the /edit conversation WITHOUT Telegram and WITHOUT writing to Notion.
It sends scripted messages and button presses to the real handlers, using a fake
Telegram connection and a fake Notion that only records what would be sent.
Run with:  python -m tests.test_edit_flow
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
from flows.edit_flow import build_edit_handler

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
NAME_OF = {tag_id: name for name, tag_id in IDS.items()}
VALID_TAGS = notion_search.get_valid_tags()
TABLE_ID = notion_search.NOTION_DATA_SOURCE_ID


def make_page(page_id, name, author="Lựu Cảnh", chapters=30, tags=("Sủng",), link="https://truyenfull.vn/a/"):
    return {
        "object": "page", "id": page_id, "in_trash": False, "url": f"https://notion.so/{page_id}",
        "parent": {"type": "data_source_id", "data_source_id": TABLE_ID},
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Author": {"rich_text": [{"plain_text": author}]},
            "Chapters": {"number": chapters},
            "Tags": {"multi_select": [{"name": t} for t in tags]},
            "Link": {"url": link},
            "Status": {"status": {"name": "Full"}},
            "Reading Status ": {"status": {"name": "Not started"}},
            "Record Audio": {"status": {"name": "Not started"}},
            "Note": {"rich_text": [{"plain_text": "ghi chú cũ"}]},
            "Date": {"date": {"start": "2024-03-16"}},
        },
    }


def to_read_shape(payload: dict) -> dict:
    """Turn a value we SEND to Notion into the shape Notion gives back when READING."""
    if "title" in payload or "rich_text" in payload:
        key = "title" if "title" in payload else "rich_text"
        return {key: [{"plain_text": t["text"]["content"]} for t in payload[key]]}
    if "multi_select" in payload:
        return {"multi_select": [{"name": NAME_OF[o["id"]]} for o in payload["multi_select"]]}
    return payload


class FakeNotion:
    def __init__(self):
        self.reset()
        self.pages = self
        self.data_sources = self

    def reset(self):
        self.store = {"p1": make_page("p1", "Truyện A"), "p2": make_page("p2", "Truyện B", author="Khác", chapters=12)}
        self.search_results = [self.store["p1"], self.store["p2"]]
        self.has_more = False
        self.updates = []           # (page_id, properties)
        self.update_error = None
        self.query_error = None

    def query(self, **kw):
        if self.query_error:
            raise self.query_error
        return {"results": self.search_results, "has_more": self.has_more, "next_cursor": None}

    def retrieve(self, page_id):
        return self.store[page_id]

    def update(self, page_id, properties):
        if self.update_error:
            raise self.update_error
        self.updates.append((page_id, properties))
        page = self.store[page_id]
        for column, payload in properties.items():
            page["properties"][column] = to_read_shape(payload)
        return page


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
        self.app, self.request, self.n, self.mark = app, request, 0, 0

    def _next(self):
        self.n += 1
        return self.n

    async def say(self, text, user=OWNER):
        message = {"message_id": self._next(), "date": 1700000000, "chat": {"id": user, "type": "private"},
                   "from": {"id": user, "is_bot": False, "first_name": "U"}, "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.app.process_update(Update.de_json({"update_id": self._next(), "message": message}, self.app.bot))

    async def press(self, data, user=OWNER):
        message = {"message_id": 1, "date": 1700000000, "chat": {"id": user, "type": "private"}}
        query = {"id": str(self._next()), "from": {"id": user, "is_bot": False, "first_name": "U"},
                 "chat_instance": "x", "data": data, "message": message}
        await self.app.process_update(Update.de_json({"update_id": self._next(), "callback_query": query}, self.app.bot))

    def new_calls(self):
        calls = self.request.calls[self.mark:]
        self.mark = len(self.request.calls)
        return calls

    @staticmethod
    def texts(calls):
        return [p.get("text", "") for name, p in calls if name in ("sendMessage", "editMessageText")]

    @staticmethod
    def keyboard(calls):
        """(label, callback_data) of every button in the last keyboard the bot sent."""
        found = []
        for name, p in calls:
            markup = p.get("reply_markup")
            if markup:
                markup = json.loads(markup) if isinstance(markup, str) else markup
                found = [(b["text"], b["callback_data"]) for row in markup["inline_keyboard"] for b in row]
        return found

    @staticmethod
    def alerts(calls):
        return [p for name, p in calls if name == "answerCallbackQuery" and p.get("show_alert")]

    def confirm_data(self, calls):
        """The callback_data of the 'Confirm' button in the last keyboard."""
        return next(d for label, d in self.keyboard(calls) if "Confirm" in label)


async def new_chat():
    request = FakeRequest()
    app = Application.builder().token("1:TEST").request(request).build()
    app.add_handler(build_edit_handler(only_me, OWNER, friendly_error_stub))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, gemini_stub))
    await app.initialize()
    chat = Chat(app, request)
    chat.new_calls()  # forget getMe
    return chat


def tag_button(name: str) -> str:
    return f"edit:tag:{[t.strip() for t in VALID_TAGS].index(name)}"


async def open_story(chat, text="Lựu", book=0):
    """/edit -> search -> pick a book. Returns the calls of the last step (the column list)."""
    await chat.say("/edit")
    await chat.say(text)
    chat.new_calls()
    await chat.press(f"edit:book:{book}")
    return chat.new_calls()


async def main():
    # ---------------- happy path ----------------
    print("--- happy path: find a book, then change several columns ---")
    chat = await new_chat()
    await chat.say("/edit")
    check("/edit asks for part of a name or author", any("TÊN truyện hoặc TÊN TÁC GIẢ" in t for t in chat.texts(chat.new_calls())))
    await chat.say("Lựu")
    calls = chat.new_calls()
    labels = [l for l, d in chat.keyboard(calls)]
    check("shows the matching books as buttons", [d for l, d in chat.keyboard(calls)] == ["edit:book:0", "edit:book:1"]
          and "Truyện A" in labels[0] and "Lựu Cảnh" in labels[0])
    await chat.press("edit:book:0")
    calls = chat.new_calls()
    out = "\n".join(chat.texts(calls))
    check("shows the chosen book and ALL its current values", all(x in out for x in (
        "Truyện A", "Lựu Cảnh", "30", "Sủng", "https://truyenfull.vn/a/", "Reading Status (reading progress): Not started",
        "Note: ghi chú cũ", "2024-03-16")))
    check("offers every editable column plus 'Done'", [d for l, d in chat.keyboard(calls)]
          == [f"edit:col:{k}" for k in w.EDITABLE_COLUMNS] + ["edit:finish"])

    # -- a column with a fixed list of values
    await chat.press("edit:col:reading_status")
    calls = chat.new_calls()
    labels = [l for l, d in chat.keyboard(calls)]
    check("fixed-list column shows its values with hints, current one marked",
          "● Not started - not read yet" in labels and "Done - finished" in labels)
    await chat.press("edit:choice:2")   # Done
    calls = chat.new_calls()
    out = "\n".join(chat.texts(calls))
    check("shows before and after", "Trước: Not started" in out and "Sau: Done" in out)
    check("NOTHING is written before confirming", notion.updates == [])
    await chat.press(chat.confirm_data(calls))
    calls = chat.new_calls()
    check("exactly one write, one column, to the right story",
          len(notion.updates) == 1 and notion.updates[0][0] == "p1" and set(notion.updates[0][1]) == {"Reading Status "})
    check("value written is Done", notion.updates[0][1]["Reading Status "]["status"]["name"] == "Done")
    out = "\n".join(chat.texts(calls))
    check("says it worked, then asks which column NEXT", "Đã cập nhật" in out and "Sửa cột nào nữa?" in out)
    check("the column list now shows the new value", "Reading Status (reading progress): Done" in out)

    # -- a free-text column (note), with validation of the typed value
    await chat.press("edit:col:note")
    calls = chat.new_calls()
    check("note offers 'clear' and 'choose another column'", [d for l, d in chat.keyboard(calls)] == ["edit:clear", "edit:back"])
    await chat.say("Hay lắm")
    calls = chat.new_calls()
    out = "\n".join(chat.texts(calls))
    check("note: old -> new is shown (replaced, not appended)", "Trước: ghi chú cũ" in out and "Sau: Hay lắm" in out)
    await chat.press(chat.confirm_data(calls))
    chat.new_calls()
    check("note written", notion.updates[-1][1]["Note"]["rich_text"][0]["text"]["content"] == "Hay lắm")

    # -- chapters: validation, discard, unchanged value
    await chat.press("edit:col:chapters")
    chat.new_calls()
    await chat.say("abc")
    check("chapters 'abc' is refused", any("Nhập lại" in t for t in chat.texts(chat.new_calls())))
    await chat.say("0")
    check("chapters 0 is refused", any("Nhập lại" in t for t in chat.texts(chat.new_calls())))
    await chat.say("50")
    calls = chat.new_calls()
    check("valid chapters go to the confirm step", "Sau: 50" in "\n".join(chat.texts(calls)) and "Trước: 30" in "\n".join(chat.texts(calls)))
    writes_before = len(notion.updates)
    await chat.press("edit:discard")
    out = "\n".join(chat.texts(chat.new_calls()))
    check("'Keep old value' writes nothing and returns to the column list", len(notion.updates) == writes_before
          and "Chưa ghi gì" in out and "Sửa cột nào nữa?" in out)
    await chat.press("edit:col:chapters")
    chat.new_calls()
    await chat.say("30")
    out = "\n".join(chat.texts(chat.new_calls()))
    check("a value equal to the current one is not written", "giống giá trị hiện tại" in out and len(notion.updates) == writes_before)

    # -- tags
    await chat.press("edit:col:tags")
    calls = chat.new_calls()
    labels = [l for l, d in chat.keyboard(calls)]
    check("tags: shows every tag, the current one ticked", len([d for l, d in chat.keyboard(calls) if d.startswith("edit:tag:")]) == len(VALID_TAGS)
          and "✅ Sủng" in labels)
    await chat.press(tag_button("Sủng"))      # un-select the only tag
    chat.new_calls()
    await chat.press("edit:tags_done")
    check("tags: 'Done' with nothing chosen shows an alert", len(chat.alerts(chat.new_calls())) == 1)
    await chat.press(tag_button("Ngọt"))
    chat.new_calls()
    await chat.press("edit:tags_done")
    calls = chat.new_calls()
    check("tags: before and after are shown", "Trước: Sủng" in "\n".join(chat.texts(calls)) and "Sau: Ngọt" in "\n".join(chat.texts(calls)))
    await chat.press(chat.confirm_data(calls))
    chat.new_calls()
    check("tags written by id", [t["id"] for t in notion.updates[-1][1]["Tags"]["multi_select"]] == [IDS["Ngọt"]])

    # -- clearing the date
    await chat.press("edit:col:date")
    chat.new_calls()
    await chat.press("edit:clear")
    calls = chat.new_calls()
    check("date: clearing shows 'Sau: (trống)'", "Sau: (trống)" in "\n".join(chat.texts(calls)))
    await chat.press(chat.confirm_data(calls))
    chat.new_calls()
    check("date cleared in Notion", notion.updates[-1][1]["Date"] == {"date": None})

    # -- a column that must keep a value has no 'clear'
    await chat.press("edit:col:name")
    calls = chat.new_calls()
    check("name has no 'clear' button", [d for l, d in chat.keyboard(calls)] == ["edit:back"])
    await chat.say("Tên mới của truyện")
    calls = chat.new_calls()
    await chat.press(chat.confirm_data(calls))
    out = "\n".join(chat.texts(chat.new_calls()))
    check("after renaming, the list shows the new name", "Truyện: Tên mới của truyện" in out)

    await chat.press("edit:col:link")
    chat.new_calls()
    await chat.say("khong-phai-link")
    check("a link without http is refused", any("http" in t and "Nhập lại" in t for t in chat.texts(chat.new_calls())))
    await chat.press("edit:back")
    check("'Choose another column' goes back to the column list", any("Sửa cột nào nữa?" in t for t in chat.texts(chat.new_calls())))

    await chat.press("edit:finish")
    out = "\n".join(chat.texts(chat.new_calls()))
    check("'Done' lists what was changed", "Đã sửa 5 thay đổi" in out and "Reading Status" in out and "Tags" in out and "Date" in out)
    check("5 writes in total, each one column only", len(notion.updates) == 5 and all(len(p) == 1 for _, p in notion.updates))
    check("no write ever touched another story", {pid for pid, _ in notion.updates} == {"p1"})
    check("the other story is unchanged", notion.store["p2"] == make_page("p2", "Truyện B", author="Khác", chapters=12))

    # ---------------- search ----------------
    print("--- finding the book ---")
    notion.reset()
    chat = await new_chat()
    await chat.say("/edit")
    notion.search_results = []
    chat.new_calls()
    await chat.say("không có truyện này")
    check("no result: says so and asks again", any("Không tìm thấy" in t for t in chat.texts(chat.new_calls())))
    notion.search_results = [notion.store["p1"], notion.store["p2"]]
    notion.has_more = True
    await chat.say("Lựu")
    check("more than 10 results: says only 10 are shown", any("hơn 10" in t for t in chat.texts(chat.new_calls())))
    notion.has_more = False
    await chat.say("tìm lại")
    check("typing again while choosing searches again", [d for l, d in chat.keyboard(chat.new_calls())] == ["edit:book:0", "edit:book:1"])
    await chat.say("/cancel")
    chat.new_calls()

    # ---------------- confirm safety ----------------
    print("--- confirm buttons are single-use and cannot be mixed up ---")
    notion.reset()
    chat = await new_chat()
    await open_story(chat)
    await chat.press("edit:col:author")
    chat.new_calls()
    await chat.say("Người A")
    old_confirm = chat.confirm_data(chat.new_calls())
    await chat.say("/edit")                       # start over while a confirm button is still on screen
    chat.new_calls()
    await chat.say("Lựu")
    chat.new_calls()
    await chat.press("edit:book:0")
    chat.new_calls()
    await chat.press("edit:col:author")
    chat.new_calls()
    await chat.say("Người B")
    new_confirm = chat.confirm_data(chat.new_calls())
    check("each confirm button has its own code", old_confirm != new_confirm)
    await chat.press(old_confirm)
    calls = chat.new_calls()
    check("the OLD confirm button is refused with an alert", len(chat.alerts(calls)) == 1 and notion.updates == [])
    await chat.press(new_confirm)
    chat.new_calls()
    check("the new confirm button writes 'Người B' (not 'Người A')",
          len(notion.updates) == 1 and notion.updates[0][1]["Author"]["rich_text"][0]["text"]["content"] == "Người B")
    await chat.press(new_confirm)
    check("pressing the same confirm twice writes only once", len(notion.updates) == 1)
    await chat.say("/cancel")

    # ---------------- cancel / strangers ----------------
    print("--- cancel and strangers ---")
    notion.reset()
    chat = await new_chat()
    await open_story(chat)
    await chat.press("edit:col:note")
    chat.new_calls()
    await chat.say("ghi chú chưa xác nhận")
    chat.new_calls()
    await chat.say("/cancel")
    check("/cancel stops and says nothing unconfirmed was written",
          any("Đã dừng" in t and "chưa xác nhận" in t for t in chat.texts(chat.new_calls())) and notion.updates == [])
    await chat.say("45")
    check("after cancelling, text goes back to Gemini", reached_gemini == ["45"])
    reached_gemini.clear()

    chat = await new_chat()
    await open_story(chat)
    await chat.say("chữ gõ nhầm")
    check("text while the column list is shown does NOT go to Gemini", reached_gemini == []
          and any("bấm một nút" in t for t in chat.texts(chat.new_calls())))
    await chat.say("/cancel")

    chat = await new_chat()
    await chat.say("/edit", user=STRANGER)
    check("a stranger gets no reply to /edit", chat.new_calls() == [])
    await chat.press("edit:col:note", user=STRANGER)
    await chat.press("edit:confirm:abcd", user=STRANGER)
    check("a stranger's button presses write nothing", notion.updates == [])

    # ---------------- errors ----------------
    print("--- errors ---")
    chat = await new_chat()
    notion.query_error = httpx.ConnectError("down")
    await chat.say("/edit")
    await chat.say("Lựu")
    check("a failing search stops /edit with a clear message", any("ConnectError" in t and "dừng" in t for t in chat.texts(chat.new_calls())))
    notion.query_error = None

    chat = await new_chat()
    await open_story(chat)
    await chat.press("edit:col:note")
    chat.new_calls()
    await chat.say("thử lỗi")
    calls = chat.new_calls()
    notion.update_error = notion_errors.APIResponseError("restricted_resource", 403, "no", httpx.Headers(), "{}")
    await chat.press(chat.confirm_data(calls))
    out = "\n".join(chat.texts(chat.new_calls()))
    check("Notion refusing the write explains what to check", "Update content" in out and "restricted_resource" in out)
    notion.update_error = None
    await chat.say("45")
    check("conversation ended after the error", reached_gemini == ["45"])
    reached_gemini.clear()

    # A Notion error that is NOT about permissions must not send you to check permissions
    for code, status in (("rate_limited", 429), ("internal_server_error", 500)):
        chat = await new_chat()
        await open_story(chat)
        await chat.press("edit:col:note")
        chat.new_calls()
        await chat.say("thử lỗi khác")
        calls = chat.new_calls()
        notion.update_error = notion_errors.APIResponseError(code, status, "x", httpx.Headers(), "{}")
        await chat.press(chat.confirm_data(calls))
        out = "\n".join(chat.texts(chat.new_calls()))
        check(f"{code}: does NOT blame the permission, tells you to check whether it was written",
              "Update content" not in out and "lỗi thân thiện" in out and "đã được ghi chưa" in out)
        notion.update_error = None
        await chat.say("45")
        reached_gemini.clear()

    notion.store["p1"]["in_trash"] = True
    chat = await new_chat()
    await chat.say("/edit")
    await chat.say("Lựu")
    chat.new_calls()
    await chat.press("edit:book:0")
    check("a story deleted in Notion cannot be edited", any("đã bị xóa" in t for t in chat.texts(chat.new_calls())))
    notion.store["p1"]["in_trash"] = False

    lines = open(w.WRITE_LOG_PATH, encoding="utf-8").read().splitlines()
    check("every real write was logged (5 + 1)", len(lines) == 6)

    # ---------------- /add and /edit together, as in the real bot ----------------
    print("--- /add and /edit together (one combined handler) ---")
    from flows import build_flows
    notion.reset()
    request = FakeRequest()
    app = Application.builder().token("1:TEST").request(request).build()
    app.add_handler(build_flows(only_me, OWNER, friendly_error_stub))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, gemini_stub))
    await app.initialize()
    both = Chat(app, request)
    both.new_calls()

    await both.say("/add")
    await both.say("Tên truyện dở dang")
    both.new_calls()
    await both.say("/edit")
    out = "\n".join(both.texts(both.new_calls()))
    check("/edit during /add switches to /edit", "TÊN truyện hoặc TÊN TÁC GIẢ" in out)
    await both.say("Lựu")
    calls = both.new_calls()
    check("the next text is an /edit search (not an /add answer, not Gemini)",
          [d for l, d in both.keyboard(calls)] == ["edit:book:0", "edit:book:1"] and reached_gemini == [])

    await both.say("/add")
    out = "\n".join(both.texts(both.new_calls()))
    check("/add during /edit switches to /add, starting at step 1", "1/6" in out)
    await both.say("Một cái tên")
    out = "\n".join(both.texts(both.new_calls()))
    check("the next text is an /add answer (the link question comes next)", "2/6" in out and reached_gemini == [])

    await both.say("/cancel")
    out = "\n".join(both.texts(both.new_calls()))
    check("/cancel during /add uses the /add message", "Đã hủy" in out and "Chưa ghi gì" in out)
    await both.say("45")
    check("after cancelling, text goes back to Gemini", reached_gemini == ["45"])
    reached_gemini.clear()

    # /cancel during /edit, after one confirmed change
    await both.say("/edit")
    await both.say("Lựu")
    both.new_calls()
    await both.press("edit:book:0")
    both.new_calls()
    await both.press("edit:col:note")
    both.new_calls()
    await both.say("ghi chú thử")
    await both.press(both.confirm_data(both.new_calls()))
    both.new_calls()
    writes = len(notion.updates)
    await both.say("/cancel")
    out = "\n".join(both.texts(both.new_calls()))
    check("/cancel during /edit uses the /edit message and keeps the confirmed change",
          "Đã dừng /edit" in out and "1 thay đổi đã xác nhận" in out and len(notion.updates) == writes == 1)
    await both.say("45")
    check("after cancelling /edit, text goes back to Gemini", reached_gemini == ["45"])
    reached_gemini.clear()

    # a stale button of the other flow does nothing
    await both.say("/add")
    both.new_calls()
    await both.press("edit:col:note")
    await both.press("edit:finish")
    check("buttons of /edit do nothing while /add is open", both.new_calls() == [] or all(
        name == "answerCallbackQuery" for name, _ in both.new_calls()))
    await both.say("/cancel")

    print()
    print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
    raise SystemExit(1 if failures else 0)


asyncio.run(main())
