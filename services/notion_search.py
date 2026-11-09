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
NOTION_DATA_SOURCE_ID = os.getenv("NOTION_DATA_SOURCE_ID")

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
    if not NOTION_TOKEN or not NOTION_DATA_SOURCE_ID:
        raise SystemExit(
            "Thiếu NOTION_TOKEN hoặc NOTION_DATA_SOURCE_ID. Hãy điền vào file .env."
        )
    return Client(auth=NOTION_TOKEN)


@lru_cache(maxsize=1)
def get_valid_tags() -> list[str]:
    """Ask Notion for the list of real tag names (fetched once, then remembered)."""
    info = _client().data_sources.retrieve(data_source_id=NOTION_DATA_SOURCE_ID)
    options = info["properties"][COL_TAGS]["multi_select"]["options"]
    return [o["name"] for o in options]


def _match_option(value: str, valid: list[str], label: str) -> str:
    """Match user text to a real option, ignoring upper/lower case and extra spaces.
    Example: 'cổ đại' matches the Notion tag ' Cổ Đại'."""
    wanted = value.strip().casefold()
    for option in valid:
        if option.strip().casefold() == wanted:
            return option
    raise ValueError(f"{label} '{value}' không hợp lệ. Giá trị hợp lệ: {', '.join(valid)}")


def _tag_names_to_search(real_tag: str) -> list:
    """The tag itself, plus its synonyms (see TAG_SYNONYMS) that really exist in Notion.
    Names come from Notion's own tag list, so they match exactly. A synonym that was
    renamed or deleted in Notion is skipped with a warning in the log, instead of
    making the whole search fail or silently return nothing."""
    group = next((g for g in TAG_SYNONYMS if real_tag.strip() in g), None)
    if group is None:
        return [real_tag]
    by_stripped_name = {t.strip(): t for t in get_valid_tags()}
    names = [by_stripped_name[g] for g in group if g in by_stripped_name]
    missing = [g for g in group if g not in by_stripped_name]
    if missing:
        logger.warning("TAG_SYNONYMS: tag không còn trong Notion, bỏ qua: %s. "
                       "Hãy cập nhật TAG_SYNONYMS trong services/notion_search.py.", missing)
    return names or [real_tag]


def build_filter(tags=None, reading_status=None, status=None, max_chapters=None, author=None):
    """Turn the search conditions into a Notion filter. Every condition must match (AND)."""
    conditions = []

    # Each tag the user asks for must be present on the story
    for tag in tags or []:
        real_tag = _match_option(tag, get_valid_tags(), "Tag")
        # If the tag has synonyms, the story needs ANY ONE of that group
        names = _tag_names_to_search(real_tag)
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


def _to_story(page: dict) -> dict:
    """Keep only the fields we need from one Notion row."""
    props = page["properties"]
    return {
        "name": _plain_text(props[COL_NAME]["title"]),
        "author": _plain_text(props[COL_AUTHOR]["rich_text"]),
        "chapters": props[COL_CHAPTERS]["number"],
        "tags": [t["name"].strip() for t in props[COL_TAGS]["multi_select"]],
        "link": props[COL_LINK]["url"] or page["url"],  # fall back to the Notion page
    }


def find_books(text: str, limit: int = MAX_RESULTS) -> dict:
    """Find books whose name OR author contains the text (used by /edit to pick a book).
    Returns {"books": [...], "has_more": True if there were more than `limit` matches}.
    Each book includes its Notion page "id". Read-only."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Hãy nhập một phần tên truyện hoặc tên tác giả.")
    response = _client().data_sources.query(
        data_source_id=NOTION_DATA_SOURCE_ID,
        page_size=limit,
        filter={"or": [
            {"property": COL_NAME, "title": {"contains": text}},
            {"property": COL_AUTHOR, "rich_text": {"contains": text}},
        ]},
    )
    books = [{"id": page["id"], **_to_story(page)} for page in response["results"]]
    return {"books": books, "has_more": response["has_more"]}


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
    Returns a list of {"id", "name", "author", "chapters", "link", "same"}, where "same"
    says what matched. Read-only."""
    conditions = []
    if name and name.strip():
        conditions.append({"property": COL_NAME, "title": {"contains": name.strip()}})
    if link and link.strip():
        conditions.append({"property": COL_LINK, "url": {"contains": _link_key(link)}})
    if not conditions:
        return []

    response = _client().data_sources.query(
        data_source_id=NOTION_DATA_SOURCE_ID,
        page_size=100,
        filter={"or": conditions} if len(conditions) > 1 else conditions[0],
    )
    found = []
    for page in response["results"]:
        story = _to_story(page)
        stored_link = page["properties"][COL_LINK]["url"] or ""
        same = []
        if name and name.strip() and _same_text(name, story["name"]):
            same.append("tên")
        if link and link.strip() and stored_link and _link_key(link) == _link_key(stored_link):
            same.append("link")
        if same:
            found.append({"id": page["id"], **story, "same": same})
    return found


def search_stories(tags=None, reading_status=None, status=None, max_chapters=None, author=None,
                   skip=0, seed=None):
    """Search the novel table.
    Returns {"total": how many stories match, "skip": where this batch starts,
    "shown": how many are returned, "remaining": how many are left after this batch,
    "stories": at most 10 stories}. Read-only: never changes anything in Notion.

    seed=None keeps Notion's own order. With a seed, all matches are shuffled in a
    random order that depends only on the seed, so asking again with the same seed and
    a larger skip gives the NEXT stories of the same shuffled list (no repeats)."""
    query = {"data_source_id": NOTION_DATA_SOURCE_ID, "page_size": 100}
    notion_filter = build_filter(tags, reading_status, status, max_chapters, author)
    if notion_filter:
        query["filter"] = notion_filter

    # Notion returns results in pages of up to 100. Keep asking until we have every
    # match (the table is small), so we know the real total and can shuffle them all.
    client = _client()
    stories = []
    while True:
        response = client.data_sources.query(**query)
        stories.extend(_to_story(page) for page in response["results"])
        if not response["has_more"] or not response.get("next_cursor"):
            break
        query["start_cursor"] = response["next_cursor"]

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
