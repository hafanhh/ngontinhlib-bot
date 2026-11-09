"""The ONLY place that writes to Notion.

Safety rules built into this file:
- There is no delete/archive function, and none will be added.
- add_story only creates a new row. update_story changes ONE column of ONE existing
  row per call, and only columns listed in EDITABLE_COLUMNS.
- update_story refuses any page that is not a live row of the novel table.
- Every write is appended to writes.log (with before/after values) so it can be
  undone by hand.

Gemini must NEVER call these functions. Only the bot's /add and /edit conversations
call them, and only after you press the confirm button.
"""

import json
import logging
import os
from datetime import datetime
from functools import lru_cache

from services import notion_search
from services.notion_search import (
    COL_AUTHOR,
    COL_CHAPTERS,
    COL_LINK,
    COL_NAME,
    COL_READING_STATUS,
    COL_STATUS,
    COL_TAGS,
    READING_STATUS_VALUES,
    STATUS_VALUES,
    _match_option,
)

logger = logging.getLogger(__name__)

COL_NOTE = "Note"
COL_RECORD_AUDIO = "Record Audio"
COL_DATE = "Date"
RECORD_AUDIO_VALUES = ["Not started", "In progress", "Done"]
MAX_TEXT_LENGTH = 2000  # Notion's limit for one piece of text
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRITE_LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "writes.log")

# The columns /edit may change: key -> (Vietnamese label shown to you, Notion column name).
# A column that is not in this list can never be changed by the bot.
EDITABLE_COLUMNS = {
    "name": ("Tên truyện", COL_NAME),
    "author": ("Tác giả", COL_AUTHOR),
    "chapters": ("Số chương", COL_CHAPTERS),
    "tags": ("Tags", COL_TAGS),
    "link": ("Link", COL_LINK),
    "status": ("Status (tình trạng truyện)", COL_STATUS),
    "reading_status": ("Reading Status (tiến độ đọc)", COL_READING_STATUS),
    "record_audio": ("Record Audio", COL_RECORD_AUDIO),
    "note": ("Ghi chú", COL_NOTE),
    "date": ("Ngày", COL_DATE),
}
# Only these two may be emptied. Every other column must always have a value.
CLEARABLE = {"note", "date"}


def _client():
    """The Notion client (same token as for reading)."""
    return notion_search._client()


@lru_cache(maxsize=1)
def _tag_ids() -> dict:
    """Map each real tag name to its Notion option id.
    We write tags by id, because one tag (' Cổ Đại') has a space at the start of its
    name and Notion could otherwise create a duplicate tag instead of using it."""
    info = _client().data_sources.retrieve(data_source_id=notion_search.NOTION_DATA_SOURCE_ID)
    options = info["properties"][COL_TAGS]["multi_select"]["options"]
    return {o["name"]: o["id"] for o in options}


def _text(value: str) -> list:
    """Notion's format for a piece of text."""
    return [{"type": "text", "text": {"content": value}}]


def _check_text(value: str, label: str) -> str:
    value = value.strip()
    if len(value) > MAX_TEXT_LENGTH:
        raise ValueError(f"{label} quá dài (tối đa {MAX_TEXT_LENGTH} ký tự).")
    return value


def _check_chapters(chapters) -> None:
    if isinstance(chapters, bool) or not isinstance(chapters, (int, float)) or chapters < 1:
        raise ValueError("Số chương phải là một số từ 1 trở lên.")


def _check_link(link: str) -> str:
    link = link.strip()
    if not link.startswith(("http://", "https://")):
        raise ValueError("Link phải bắt đầu bằng http:// hoặc https://.")
    return link


def _tags_property(tags) -> dict:
    """Check the tags and build the Notion value. Unknown tags are rejected
    (this raises ValueError listing the valid tags)."""
    ids = _tag_ids()
    chosen = []
    for tag in tags:
        real = _match_option(tag, list(ids), "Tag")
        if ids[real] not in [c["id"] for c in chosen]:
            chosen.append({"id": ids[real]})
    return {"multi_select": chosen}


# ---------------------------------------------------------------------------
# Turning what you type into values (the bot uses these for /add and /edit)
# ---------------------------------------------------------------------------

def parse_chapters(text: str) -> int:
    """'120' -> 120. Raises ValueError if it is not a whole number of at least 1."""
    text = (text or "").strip()
    if not text.isdigit():
        raise ValueError("Số chương phải là một số nguyên, ví dụ 120.")
    chapters = int(text)
    _check_chapters(chapters)
    return chapters


def parse_date(text: str) -> str:
    """'16/03/2024', '16-03-2024' or '2024-03-16' -> '2024-03-16'.
    Raises ValueError for anything else (including dates that do not exist)."""
    text = (text or "").strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError("Ngày không hợp lệ. Hãy nhập dạng 16/03/2024.")


# ---------------------------------------------------------------------------
# Building the data to send (no network, easy to test)
# ---------------------------------------------------------------------------

def build_add_properties(name, author, chapters, tags, link, status="Full"):
    """Check the inputs and build the Notion properties for a new story.
    Required: name, author, chapters, tags, link. Raises ValueError with a clear
    message (in Vietnamese, naming the missing item) if anything is missing or wrong."""
    # Collect every missing required item, so the user can be told all of them at once
    missing = []
    if not (name or "").strip():
        missing.append("tên truyện")
    if not (author or "").strip():
        missing.append("tác giả")
    if chapters is None:
        missing.append("số chương")
    if not tags:
        missing.append("tags")
    if not (link or "").strip():
        missing.append("link")
    if missing:
        raise ValueError("Còn thiếu: " + ", ".join(missing) + ".")

    name = _check_text(name, "Tên truyện")
    author = _check_text(author, "Tên tác giả")
    _check_chapters(chapters)
    link = _check_link(link)

    # Defaults for a new story: Status = Full (unless told otherwise),
    # Reading Status = Not started, Record Audio = Not started.
    # Note and Date are left out on purpose, so they stay empty.
    return {
        COL_NAME: {"title": _text(name)},
        COL_AUTHOR: {"rich_text": _text(author)},
        COL_CHAPTERS: {"number": chapters},
        COL_TAGS: _tags_property(tags),
        COL_LINK: {"url": link},
        COL_STATUS: {"status": {"name": _match_option(status, STATUS_VALUES, "Status")}},
        COL_READING_STATUS: {"status": {"name": "Not started"}},
        COL_RECORD_AUDIO: {"status": {"name": "Not started"}},
    }


def build_column_update(column: str, value) -> dict:
    """Build the Notion properties for changing ONE column. `column` is a key of
    EDITABLE_COLUMNS. For note and date, an empty value (None or '') clears the column."""
    if column not in EDITABLE_COLUMNS:
        raise ValueError(f"Không được sửa cột '{column}'.")
    notion_name = EDITABLE_COLUMNS[column][1]
    label = EDITABLE_COLUMNS[column][0]

    empty = value is None or (isinstance(value, (str, list)) and not value)
    if empty:
        if column not in CLEARABLE:
            raise ValueError(f"Cột {label} không được để trống.")
        # Clearing a note or a date
        return {notion_name: {"date": None} if column == "date" else {"rich_text": []}}

    if column == "name":
        return {notion_name: {"title": _text(_check_text(value, label))}}
    if column in ("author", "note"):
        return {notion_name: {"rich_text": _text(_check_text(value, label))}}
    if column == "chapters":
        _check_chapters(value)
        return {notion_name: {"number": value}}
    if column == "tags":
        return {notion_name: _tags_property(value)}
    if column == "link":
        return {notion_name: {"url": _check_link(value)}}
    if column == "date":
        return {notion_name: {"date": {"start": parse_date(value)}}}

    # The three status columns
    allowed = {
        "status": STATUS_VALUES,
        "reading_status": READING_STATUS_VALUES,
        "record_audio": RECORD_AUDIO_VALUES,
    }[column]
    return {notion_name: {"status": {"name": _match_option(value, allowed, label)}}}


# ---------------------------------------------------------------------------
# Reading back a page, and the write log
# ---------------------------------------------------------------------------

def _same_id(a: str, b: str) -> bool:
    """Notion ids can be written with or without dashes."""
    return (a or "").replace("-", "").lower() == (b or "").replace("-", "").lower()


def _check_page_in_table(page: dict) -> None:
    """Refuse to touch anything that is not a live row of the novel table."""
    parent = page.get("parent") or {}
    if page.get("object") != "page" or not _same_id(
        parent.get("data_source_id"), notion_search.NOTION_DATA_SOURCE_ID
    ):
        raise ValueError("Trang này không thuộc bảng truyện, nên mình không sửa.")
    if page.get("in_trash") or page.get("archived"):
        raise ValueError("Truyện này đã bị xóa khỏi bảng, nên mình không sửa.")


def read_column(page: dict, column: str):
    """The current value of one column, in a simple form (text, number, list or None)."""
    prop = page["properties"][EDITABLE_COLUMNS[column][1]]
    if column == "name":
        return "".join(p["plain_text"] for p in prop["title"])
    if column in ("author", "note"):
        return "".join(p["plain_text"] for p in prop["rich_text"])
    if column == "chapters":
        return prop["number"]
    if column == "tags":
        return [t["name"].strip() for t in prop["multi_select"]]
    if column == "link":
        return prop["url"]
    if column == "date":
        return prop["date"]["start"] if prop["date"] else None
    return prop["status"]["name"] if prop["status"] else None


def get_story(page_id: str) -> dict:
    """Read one story's current values for all editable columns (used by /edit to show
    the current value before you change it). Read-only."""
    page = _client().pages.retrieve(page_id=page_id)
    _check_page_in_table(page)
    return {column: read_column(page, column) for column in EDITABLE_COLUMNS}


def _log_write(action: str, **details) -> None:
    """Append one line to writes.log. A failure to log never hides a finished write."""
    entry = {"time": datetime.now().isoformat(timespec="seconds"), "action": action, **details}
    try:
        os.makedirs(os.path.dirname(WRITE_LOG_PATH), exist_ok=True)
        with open(WRITE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Không ghi được writes.log")
    logger.info("Notion write: %s", action)


# ---------------------------------------------------------------------------
# The two write functions
# ---------------------------------------------------------------------------

def add_story(name, author, chapters, tags, link, status="Full") -> dict:
    """Add ONE new story (a new row). Never changes existing rows.
    name, author, chapters, tags and link are all required."""
    props = build_add_properties(name, author, chapters, tags, link, status)
    page = _client().pages.create(
        parent={"type": "data_source_id", "data_source_id": notion_search.NOTION_DATA_SOURCE_ID},
        properties=props,
    )
    result = {"id": page["id"], "url": page.get("url"), "name": name.strip()}
    _log_write("add", page_id=page["id"], name=result["name"], author=author,
               chapters=chapters, tags=tags, link=link, status=status)
    return result


def update_story(page_id: str, column: str, value) -> dict:
    """Change ONE column of ONE existing story. Nothing else is touched.
    Returns the old and new value of that column."""
    props = build_column_update(column, value)
    client = _client()

    # Look at the page first: it must be a live row of our table, and we keep its old value
    page = client.pages.retrieve(page_id=page_id)
    _check_page_in_table(page)
    story_name = read_column(page, "name")
    before = read_column(page, column)

    updated = client.pages.update(page_id=page_id, properties=props)
    after = read_column(updated, column)

    _log_write("update", page_id=page_id, name=story_name, column=column, before=before, after=after)
    return {"id": page_id, "name": story_name, "column": column, "before": before, "after": after}
