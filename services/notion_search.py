"""Reads your Notion novel lists (this file never changes anything in Notion).

- search_stories: the main search. It reads ALL lists (list 1 + the extra lists) as if they
  were one table, shuffles the matches with a seed (so "xem thêm" never repeats), and
  returns at most 10 stories at a time.
- find_books / find_duplicates: small lookups used by /edit (list 1 only) and by the
  /add duplicate warning (all lists).
- Each list has its OWN tag options, so tags are always checked against the list they
  are used in (get_valid_tags(source_id)).
- Which lists are used comes from .env (each value is a Notion "data source ID"):
    NOTION_DATA_SOURCE_ID       ALL your lists, comma-separated. They are used for the search,
                                the /add duplicate warning and /edit (which works in any of them)
    NOTION_ADD_DATA_SOURCE_ID   the ONE list where /add puts new stories (empty = the first)

Writing to Notion lives in notion_write.py only.
"""

import logging
import os
import random
from functools import lru_cache

from dotenv import load_dotenv
from notion_client import Client

# Read the secrets from the .env file into environment variables
load_dotenv()

logger = logging.getLogger(__name__)

NOTION_TOKEN = os.getenv("NOTION_TOKEN")


def _split_ids(text) -> list:
    """'id1, id2,,id3' -> ['id1', 'id2', 'id3'] (spaces and blanks removed, no repeats)."""
    ids = []
    for part in (text or "").split(","):
        part = part.strip()
        if part and part not in ids:
            ids.append(part)
    return ids


# The lists, as set in .env. The ORDER matters: the first one is list 1, the next list 2...
DATA_SOURCE_IDS = _split_ids(os.getenv("NOTION_DATA_SOURCE_ID"))        # all lists: search, duplicates, /edit
ADD_SOURCE_ID = (os.getenv("NOTION_ADD_DATA_SOURCE_ID") or "").strip() or None  # where /add writes

# Column names in the Notion table. They must match EXACTLY.
# Note: "Reading Status " really has a space at the end in Notion!
COL_NAME = "Name"
COL_AUTHOR = "Author"
COL_CHAPTERS = "Chapters"
COL_TAGS = "Tags"
COL_STATUS = "Status"
COL_READING_STATUS = "Reading Status "
COL_LINK = "Link"

# Allowed values for the two "status" columns
STATUS_VALUES = ["Full", "In progress", "Drop"]
READING_STATUS_VALUES = ["Not started", "In progress", "Done", "Beginning + End", "Drop"]

MAX_RESULTS = 10

# Tags that mean the same thing. Searching for one also finds the others.
# To add more, add another list of names (they must match the Notion tag names).
TAG_SYNONYMS = [
    ["Trùng sinh", "Trọng sinh"],
]


@lru_cache(maxsize=1)
def _client() -> Client:
    """The Notion client: created once and reused (a missing .env value stops the bot with a
    clear message, and is not cached, so fixing .env and retrying works)."""
    if not NOTION_TOKEN or not all_sources():
        raise SystemExit(
            "Thiếu NOTION_TOKEN hoặc NOTION_DATA_SOURCE_ID. Hãy điền vào file .env."
        )
    return Client(auth=NOTION_TOKEN)


def all_sources() -> list:
    """Every list we work with (search, /add duplicate warning and /edit), in .env order
    (the first is "list 1"), then the /add list if it is not in there already (so a story
    just added can always be found and edited)."""
    sources = list(DATA_SOURCE_IDS)
    if ADD_SOURCE_ID and ADD_SOURCE_ID not in sources:
        sources.append(ADD_SOURCE_ID)
    return sources


def main_source_id() -> str | None:
    """The first list. It is the default when a function is not told which list to use."""
    sources = all_sources()
    return sources[0] if sources else None


def add_source_id() -> str | None:
    """The list /add writes new stories to (the first list if NOTION_ADD_DATA_SOURCE_ID is empty)."""
    return ADD_SOURCE_ID or main_source_id()


def list_number(source_id: str) -> int:
    """Position of a list in the search order (1 = the first list), or 0 if unknown."""
    sources = all_sources()
    return sources.index(source_id) + 1 if source_id in sources else 0


def get_valid_tags(source_id: str = None) -> list[str]:
    """The real tag names of ONE list. No source_id means the first list.
    Each list has its own tag options."""
    return _fetch_tags(source_id or main_source_id())


@lru_cache(maxsize=None)
def _fetch_tags(source_id: str) -> list[str]:
    """Ask Notion for the tag names of one list (fetched once, then remembered)."""
    info = _client().data_sources.retrieve(data_source_id=source_id)
    options = info["properties"][COL_TAGS]["multi_select"]["options"]
    return [o["name"] for o in options]


def get_all_tags() -> list[str]:
    """Tag names of all searched lists together (first list first, no repeats)."""
    tags = []
    for source_id in all_sources():
        for tag in get_valid_tags(source_id):
            if tag not in tags:
                tags.append(tag)
    return tags


def _match_option(value: str, valid: list[str], label: str) -> str:
    """Match user text to a real option, ignoring upper/lower case and extra spaces.
    Example: 'cổ đại' matches the Notion tag ' Cổ Đại'."""
    wanted = value.strip().casefold()
    for option in valid:
        if option.strip().casefold() == wanted:
            return option
    raise ValueError(f"{label} '{value}' không hợp lệ. Giá trị hợp lệ: {', '.join(valid)}")


def _tag_names_to_search(real_tag: str, valid_tags: list) -> list:
    """The tag itself, plus its synonyms (see TAG_SYNONYMS) that really exist in Notion.
    `valid_tags` is the real tag list of the list being searched. Names come from it, so they match exactly. A synonym that was
    renamed or deleted in Notion is skipped with a warning in the log, instead of
    making the whole search fail or silently return nothing."""
    group = next((g for g in TAG_SYNONYMS if real_tag.strip() in g), None)
    if group is None:
        return [real_tag]
    by_stripped_name = {t.strip(): t for t in valid_tags}
    names = [by_stripped_name[g] for g in group if g in by_stripped_name]
    missing = [g for g in group if g not in by_stripped_name]
    if missing:
        logger.warning("TAG_SYNONYMS: tag không còn trong Notion, bỏ qua: %s. "
                       "Hãy cập nhật TAG_SYNONYMS trong services/notion_search.py.", missing)
    return names or [real_tag]


class NoMatchInThisList(Exception):
    """A tag that this list does not have at all, so no story here can match."""


def build_filter(tags=None, reading_status=None, status=None, max_chapters=None, author=None,
                 source_id=None, skip_missing_tags=False):
    """Turn the search conditions into a Notion filter. Every condition must match (AND).
    source_id picks the list (None = list 1). With skip_missing_tags=True, a tag that this
    list does not have raises NoMatchInThisList instead of ValueError (the caller has
    already checked that the tag exists in at least one list)."""
    conditions = []
    valid_tags = get_valid_tags(source_id) if tags else []  # only ask Notion when needed

    # Each tag the user asks for must be present on the story
    for tag in tags or []:
        try:
            real_tag = _match_option(tag, valid_tags, "Tag")
        except ValueError:
            if skip_missing_tags:
                raise NoMatchInThisList(tag)
            raise
        # If the tag has synonyms, the story needs ANY ONE of that group
        names = _tag_names_to_search(real_tag, valid_tags)
        options = [{"property": COL_TAGS, "multi_select": {"contains": n}} for n in names]
        conditions.append(options[0] if len(options) == 1 else {"or": options})

    if reading_status:
        real = _match_option(reading_status, READING_STATUS_VALUES, "Reading Status")
        conditions.append({"property": COL_READING_STATUS, "status": {"equals": real}})

    if status:
        real = _match_option(status, STATUS_VALUES, "Status")
        conditions.append({"property": COL_STATUS, "status": {"equals": real}})

    # "max_chapters" is inclusive: 100 means 100 chapters or fewer
    if max_chapters is not None:
        conditions.append(
            {"property": COL_CHAPTERS, "number": {"less_than_or_equal_to": max_chapters}}
        )

    if author:
        conditions.append({"property": COL_AUTHOR, "rich_text": {"contains": author.strip()}})

    return {"and": conditions} if conditions else None


def _plain_text(rich_text_list: list) -> str:
    """Join Notion's text pieces into one normal string."""
    return "".join(piece["plain_text"] for piece in rich_text_list)


COL_ID = "ID"  # the column that numbers the stories of a list (L1-1, L2-15, L3-90 ...)


def id_label(page: dict) -> str | None:
    """The ID of a row as text (like 'L3-90'), or None if the list has no ID or it is empty.
    The ID column is Notion's automatic ID in some lists and plain text in others."""
    prop = (page.get("properties") or {}).get(COL_ID) or {}
    if prop.get("type") == "rich_text":  # a text ID column: the text itself
        return "".join(p.get("plain_text", "") for p in prop["rich_text"]).strip() or None
    unique = prop.get("unique_id") if prop.get("type") == "unique_id" else None
    if not unique or unique.get("number") is None:
        return None
    # Notion's automatic ID is a prefix + a number, like "L3" + 90
    return f"{unique['prefix']}-{unique['number']}" if unique.get("prefix") else str(unique["number"])


def _to_story(page: dict) -> dict:
    """Keep only the fields we need from one Notion row."""
    props = page["properties"]
    return {
        "code": id_label(page),
        "name": _plain_text(props[COL_NAME]["title"]),
        "author": _plain_text(props[COL_AUTHOR]["rich_text"]),
        "chapters": props[COL_CHAPTERS]["number"],
        "tags": [t["name"].strip() for t in props[COL_TAGS]["multi_select"]],
        "link": props[COL_LINK]["url"] or page["url"],  # fall back to the Notion page
    }


def find_books(text: str, limit: int = MAX_RESULTS) -> dict:
    """Find books whose name OR author contains the text (used by /edit to pick a book).
    Looks in every list (see all_sources), in .env order.
    Returns {"books": [...], "has_more": True if there were more than `limit` matches}.
    Each book has its Notion page "id", the list it is in ("source_id" and "list", the list
    number) and its ID "code" (like L3-91). Read-only."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Hãy nhập một phần tên truyện hoặc tên tác giả.")
    books, has_more = [], False
    for source_id in all_sources():
        response = _client().data_sources.query(
            data_source_id=source_id,
            page_size=limit,
            filter={"or": [
                {"property": COL_NAME, "title": {"contains": text}},
                {"property": COL_AUTHOR, "rich_text": {"contains": text}},
            ]},
        )
        has_more = has_more or response["has_more"]
        for page in response["results"]:
            books.append({"id": page["id"], "source_id": source_id, "list": list_number(source_id),
                          **_to_story(page)})
    if len(books) > limit:  # more matches than the buttons can show
        books, has_more = books[:limit], True
    return {"books": books, "has_more": has_more}


def _same_text(a: str, b: str) -> bool:
    """Compare names ignoring upper/lower case and extra spaces."""
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def _link_key(url: str) -> str:
    """Make two spellings of the same link comparable (http/https, www, trailing slash)."""
    url = url.strip().casefold()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.removeprefix("www.").rstrip("/")


def find_duplicates(name: str = None, link: str = None) -> list:
    """Find stories that already have this exact name or this link (used before /add).
    Looks in ALL lists (list 1 and the extra ones). Returns a list of
    {"id", "name", "author", "chapters", "link", "same", "list"}, where "same" says what
    matched and "list" is the list number (1 = the main list). Read-only."""
    conditions = []
    if name and name.strip():
        conditions.append({"property": COL_NAME, "title": {"contains": name.strip()}})
    if link and link.strip():
        conditions.append({"property": COL_LINK, "url": {"contains": _link_key(link)}})
    if not conditions:
        return []

    found = []
    for number, source_id in enumerate(all_sources(), start=1):
        response = _client().data_sources.query(
            data_source_id=source_id,
            page_size=100,
            filter={"or": conditions} if len(conditions) > 1 else conditions[0],
        )
        for page in response["results"]:
            story = _to_story(page)
            stored_link = page["properties"][COL_LINK]["url"] or ""
            same = []
            if name and name.strip() and _same_text(name, story["name"]):
                same.append("tên")
            if link and link.strip() and stored_link and _link_key(link) == _link_key(stored_link):
                same.append("link")
            if same:
                found.append({"id": page["id"], **story, "same": same, "list": number})
    return found


def _query_all(source_id: str, notion_filter) -> list:
    """Every story of ONE list that matches the filter (Notion sends up to 100 per page)."""
    query = {"data_source_id": source_id, "page_size": 100}
    if notion_filter:
        query["filter"] = notion_filter
    client = _client()
    stories = []
    while True:
        response = client.data_sources.query(**query)
        stories.extend(_to_story(page) for page in response["results"])
        if not response["has_more"] or not response.get("next_cursor"):
            return stories
        query["start_cursor"] = response["next_cursor"]


def search_stories(tags=None, reading_status=None, status=None, max_chapters=None, author=None,
                   skip=0, seed=None):
    """Search ALL lists (list 1 + the extra lists) as if they were one table.
    Returns {"total": how many stories match, "skip": where this batch starts,
    "shown": how many are returned, "remaining": how many are left after this batch,
    "stories": at most 10 stories}. Read-only: never changes anything in Notion.

    The matches of every list are joined (list 1 first). A story with the same link in
    two lists is counted once. seed=None keeps that order. With a seed, all matches are
    shuffled in a random order that depends only on the seed, so asking again with the
    same seed and a larger skip gives the NEXT stories of the same shuffled list."""
    sources = all_sources()
    many = len(sources) > 1

    # A tag must exist in at least one list, otherwise it is a typo (clear error message)
    if tags:
        all_tags = get_all_tags()
        for tag in tags:
            _match_option(tag, all_tags, "Tag")

    # Each list is asked separately (each has its own tag options). We keep asking until
    # we have every match (the tables are small), so we know the real total and can shuffle.
    stories = []
    seen_links = set()
    for number, source_id in enumerate(sources, start=1):
        try:
            notion_filter = build_filter(tags, reading_status, status, max_chapters, author,
                                         source_id=source_id, skip_missing_tags=many)
        except NoMatchInThisList:
            continue  # this list does not have the tag, so nothing here can match
        try:
            found = _query_all(source_id, notion_filter)
        except Exception:
            logger.warning("Notion: searching list %d of %d failed", number, len(sources))
            raise
        for story in found:
            key = _link_key(story["link"])
            if key not in seen_links:
                seen_links.add(key)
                stories.append(story)

    if seed is not None:
        random.Random(seed).shuffle(stories)

    batch = stories[skip:skip + MAX_RESULTS]
    return {
        "total": len(stories),
        "skip": skip,
        "shown": len(batch),
        "remaining": max(0, len(stories) - skip - len(batch)),
        "stories": batch,
    }
