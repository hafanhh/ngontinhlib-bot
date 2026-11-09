"""Tests for the Notion write code and the book finder, WITHOUT touching your table.
It replaces the Notion connection with a fake one that only records what would be sent.
Run with:  python -m tests.test_write
(The only real Notion call is reading the tag list, which is read-only.)"""

import os
import tempfile

from services import notion_search
from services import notion_write as w

TABLE_ID = notion_search.NOTION_DATA_SOURCE_ID
failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def raises(func, *args, **kwargs) -> str | None:
    """Return the ValueError message if func raises ValueError, else None."""
    try:
        func(*args, **kwargs)
    except ValueError as error:
        return str(error)
    return None


# Read the real tag list first (read-only), then switch to the fake client
IDS = w._tag_ids()
NAME_OF = {tag_id: name for name, tag_id in IDS.items()}


def to_read_shape(payload: dict) -> dict:
    """Turn a value we SEND to Notion into the shape Notion gives back when READING."""
    if "title" in payload or "rich_text" in payload:
        key = "title" if "title" in payload else "rich_text"
        return {key: [{"plain_text": t["text"]["content"]} for t in payload[key]]}
    if "multi_select" in payload:
        return {"multi_select": [{"name": NAME_OF[o["id"]]} for o in payload["multi_select"]]}
    return payload  # number, url, status, date already have the same shape


def make_page(page_id="page-1", name="Truyện thử", author="Tác Giả", link="https://truyenfull.vn/a/"):
    return {
        "object": "page", "id": page_id, "in_trash": False, "url": f"https://notion.so/{page_id}",
        "parent": {"type": "data_source_id", "data_source_id": TABLE_ID},
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Author": {"rich_text": [{"plain_text": author}]},
            "Chapters": {"number": 30},
            "Tags": {"multi_select": [{"name": "Sủng"}]},
            "Link": {"url": link},
            "Status": {"status": {"name": "Full"}},
            "Reading Status ": {"status": {"name": "Not started"}},
            "Record Audio": {"status": {"name": "Not started"}},
            "Note": {"rich_text": [{"plain_text": "ghi chú cũ"}]},
            "Date": {"date": {"start": "2024-03-16"}},
        },
    }


class FakePages:
    """Pretends to be Notion's pages API and records every call."""

    def __init__(self):
        self.calls = []
        self.page = make_page()

    def create(self, **kw):
        self.calls.append(("create", kw))
        return {"id": "new-page", "url": "https://notion.so/new"}

    def retrieve(self, page_id):
        self.calls.append(("retrieve", page_id))
        return self.page

    def update(self, page_id, properties):
        self.calls.append(("update", page_id, properties))
        page = dict(self.page, properties=dict(self.page["properties"]))
        for column, payload in properties.items():
            page["properties"][column] = to_read_shape(payload)
        return page


class FakeDataSources:
    """Pretends to be Notion's table query and records every call."""

    def __init__(self):
        self.calls = []
        self.results = []
        self.has_more = False

    def query(self, **kw):
        self.calls.append(kw)
        return {"results": self.results, "has_more": self.has_more, "next_cursor": None}


class FakeClient:
    def __init__(self):
        self.pages = FakePages()
        self.data_sources = FakeDataSources()


# Use a temporary log file and the fake client, so nothing real is touched
fake = FakeClient()
w._client = lambda: fake
notion_search._client = lambda: fake
w.WRITE_LOG_PATH = os.path.join(tempfile.mkdtemp(), "writes.log")

# A complete, valid story (name, author, chapters, tags, link)
GOOD = ("Truyện mới", "Tác Giả", 50, ["Sủng"], "https://example.com/x")

print("--- add_story ---")
result = w.add_story("  Truyện mới  ", author="Tác Giả", chapters=50, tags=["sủng", "cổ đại", "Sủng"],
                     link="https://example.com/x")
kind, kw = fake.pages.calls[-1]
props = kw["properties"]
check("creates exactly one page", kind == "create" and len(fake.pages.calls) == 1)
check("parent is the novel table", kw["parent"]["data_source_id"] == TABLE_ID)
check("name is trimmed", props["Name"]["title"][0]["text"]["content"] == "Truyện mới")
check("Status defaults to Full", props["Status"]["status"]["name"] == "Full")
check("Reading Status is Not started", props["Reading Status "]["status"]["name"] == "Not started")
check("Record Audio is Not started", props["Record Audio"]["status"]["name"] == "Not started")
check("Note and Date are never set (stay empty)", "Note" not in props and "Date" not in props)
check("tags written by id, duplicates removed", len(props["Tags"]["multi_select"]) == 2
      and all("id" in t and "name" not in t for t in props["Tags"]["multi_select"]))
check("only the expected columns are sent",
      set(props) == {"Name", "Status", "Reading Status ", "Record Audio", "Author", "Chapters", "Link", "Tags"})
check("returns id and url", result["id"] == "new-page" and result["url"])
check("status can be overridden",
      w.build_add_properties(*GOOD, status="in progress")["Status"]["status"]["name"] == "In progress")
check("1 chapter is accepted", w.build_add_properties("A", "B", 1, ["Sủng"], "https://x.com")["Chapters"]["number"] == 1)

print("--- add_story: required fields (name, author, chapters, tags, link) ---")
calls_before = len(fake.pages.calls)
for label, args in [
    ("name", ("   ", "Tác giả", 10, ["Sủng"], "https://x.com")),
    ("author", ("Tên", "  ", 10, ["Sủng"], "https://x.com")),
    ("author (None)", ("Tên", None, 10, ["Sủng"], "https://x.com")),
    ("chapters", ("Tên", "Tác giả", None, ["Sủng"], "https://x.com")),
    ("tags (empty list)", ("Tên", "Tác giả", 10, [], "https://x.com")),
    ("tags (None)", ("Tên", "Tác giả", 10, None, "https://x.com")),
    ("link", ("Tên", "Tác giả", 10, ["Sủng"], "")),
]:
    message = raises(w.add_story, *args) or ""
    check(f"missing {label} is rejected", message.startswith("Còn thiếu"))
message = raises(w.add_story, "", "", None, None, None) or ""
check("all missing items are listed together",
      all(item in message for item in ("tên truyện", "tác giả", "số chương", "tags", "link")))

print("--- add_story rejects bad values (and writes nothing) ---")
check("unknown tag", "không hợp lệ" in (raises(w.add_story, "A", "B", 5, ["Ngôn tình"], "https://x.com") or ""))
check("negative chapters", raises(w.add_story, "A", "B", -1, ["Sủng"], "https://x.com") is not None)
check("zero chapters", raises(w.add_story, "A", "B", 0, ["Sủng"], "https://x.com") is not None)
check("chapters as text", raises(w.add_story, "A", "B", "50", ["Sủng"], "https://x.com") is not None)
check("link without http", raises(w.add_story, "A", "B", 5, ["Sủng"], "example.com") is not None)
check("bad status", raises(w.add_story, "A", "B", 5, ["Sủng"], "https://x.com", status="Maybe") is not None)
check("name too long", raises(w.add_story, "x" * 2001, "B", 5, ["Sủng"], "https://x.com") is not None)
check("no Notion call was made for any bad input", len(fake.pages.calls) == calls_before)

print("--- typed input helpers ---")
check("parse_chapters('120')", w.parse_chapters(" 120 ") == 120)
check("parse_chapters rejects text, 0 and decimals",
      all(raises(w.parse_chapters, t) for t in ("abc", "0", "1.5", "-3", "")))
check("parse_date accepts 3 formats", {w.parse_date(t) for t in ("16/03/2024", "16-03-2024", "2024-03-16")} == {"2024-03-16"})
check("parse_date rejects non-dates", all(raises(w.parse_date, t) for t in ("31/02/2024", "hôm qua", "")))

print("--- update_story: one column at a time ---")
CASES = [  # (column, new value, expected 'after')
    ("name", "Tên mới", "Tên mới"),
    ("author", "Người khác", "Người khác"),
    ("chapters", 99, 99),
    ("tags", ["cổ đại", "Ngọt"], ["Cổ Đại", "Ngọt"]),
    ("link", "https://example.com/new", "https://example.com/new"),
    ("status", "in progress", "In progress"),
    ("reading_status", "done", "Done"),
    ("record_audio", "in progress", "In progress"),
    ("note", "Hay lắm", "Hay lắm"),
    ("date", "20/05/2024", "2024-05-20"),
]
for column, value, expected in CASES:
    fake.pages.calls.clear()
    r = w.update_story("page-1", column, value)
    sent = [c for c in fake.pages.calls if c[0] == "update"]
    notion_column = w.EDITABLE_COLUMNS[column][1]
    check(f"{column}: exactly one update, one column ({notion_column!r})",
          len(sent) == 1 and set(sent[0][2]) == {notion_column})
    check(f"{column}: after = {expected!r}", r["after"] == expected)
check("before values are returned", w.update_story("page-1", "note", "x")["before"] == "ghi chú cũ")
check("note is replaced, not appended", w.update_story("page-1", "note", "mới")["after"] == "mới")
check("story name is returned", w.update_story("page-1", "chapters", 5)["name"] == "Truyện thử")

print("--- update_story: clearing ---")
check("empty note clears it", w.update_story("page-1", "note", "")["after"] == "")
check("None clears the date", w.update_story("page-1", "date", None)["after"] is None)
for column in ("name", "author", "chapters", "tags", "link", "status", "reading_status", "record_audio"):
    check(f"{column} cannot be emptied", "không được để trống" in (raises(w.build_column_update, column, "") or ""))

print("--- update_story rejects bad input (and writes nothing) ---")
fake.pages.calls.clear()
check("column not in the list (Notion id)", raises(w.update_story, "page-1", "id", "x") is not None)
check("column not in the list (a made-up one)", raises(w.update_story, "page-1", "Created time", "x") is not None)
check("Notion column name instead of key", raises(w.update_story, "page-1", "Reading Status ", "Done") is not None)
check("bad reading status", raises(w.update_story, "page-1", "reading_status", "Finished") is not None)
check("bad record audio", raises(w.update_story, "page-1", "record_audio", "Maybe") is not None)
check("unknown tag", raises(w.update_story, "page-1", "tags", ["Ngôn tình"]) is not None)
check("zero chapters", raises(w.update_story, "page-1", "chapters", 0) is not None)
check("link without http", raises(w.update_story, "page-1", "link", "abc") is not None)
check("not a date", raises(w.update_story, "page-1", "date", "hôm qua") is not None)
check("no Notion call for any bad input", fake.pages.calls == [])
fake.pages.page["parent"] = {"type": "data_source_id", "data_source_id": "some-other-table"}
check("refuses a page from another table", "không thuộc bảng" in (raises(w.update_story, "page-1", "note", "x") or ""))
fake.pages.page["parent"] = {"type": "data_source_id", "data_source_id": TABLE_ID}
fake.pages.page["in_trash"] = True
check("refuses a deleted page", "đã bị xóa" in (raises(w.update_story, "page-1", "note", "x") or ""))
check("refuses to read a deleted page", raises(w.get_story, "page-1") is not None)
check("no update call was made for refused pages", not [c for c in fake.pages.calls if c[0] == "update"])
fake.pages.page["in_trash"] = False

print("--- get_story ---")
story = w.get_story("page-1")
check("returns all editable columns", set(story) == set(w.EDITABLE_COLUMNS))
check("values are readable", story["name"] == "Truyện thử" and story["chapters"] == 30
      and story["tags"] == ["Sủng"] and story["date"] == "2024-03-16")

print("--- find_books (used by /edit to pick a book) ---")
fake.data_sources.results = [make_page("p1", "Truyện A"), make_page("p2", "Truyện B")]
found = notion_search.find_books("  truyện ", limit=10)
sent = fake.data_sources.calls[-1]
check("returns books with their page id", [b["id"] for b in found["books"]] == ["p1", "p2"])
check("searches name OR author", [c["property"] for c in sent["filter"]["or"]] == ["Name", "Author"])
check("page size is the limit", sent["page_size"] == 10)
check("has_more is False when all fit", found["has_more"] is False)
fake.data_sources.has_more = True
check("has_more is passed on", notion_search.find_books("x")["has_more"] is True)
fake.data_sources.has_more = False
check("empty text is rejected", raises(notion_search.find_books, "   ") is not None)

print("--- find_duplicates (used before /add) ---")
fake.data_sources.calls.clear()
fake.data_sources.results = [
    make_page("d1", name="  TRUYỆN   thử ", link="https://other.com/1"),        # same name (case/space differ)
    make_page("d2", name="Truyện thử phần 2", link="https://other.com/2"),      # name only CONTAINS it: not a duplicate
    make_page("d3", name="Tên khác hẳn", link="http://www.truyenfull.vn/a"),   # same link (http, www, no slash)
    make_page("d4", name="Truyện thử", link="https://truyenfull.vn/a/"),        # both same
]
dups = notion_search.find_duplicates(name="Truyện thử", link="https://truyenfull.vn/a/")
by_id = {d["id"]: d["same"] for d in dups}
check("same name (ignoring case and spaces) is a duplicate", by_id.get("d1") == ["tên"])
check("a name that only contains it is NOT a duplicate", "d2" not in by_id)
check("same link (http/www/slash differences ignored) is a duplicate", by_id.get("d3") == ["link"])
check("both matching is reported as both", by_id.get("d4") == ["tên", "link"])
check("uses one query with OR", len(fake.data_sources.calls) == 1 and "or" in fake.data_sources.calls[0]["filter"])
fake.data_sources.calls.clear()
check("nothing to check means no query", notion_search.find_duplicates() == [] and fake.data_sources.calls == [])

print("--- no delete anywhere ---")
source = open(w.__file__, encoding="utf-8").read()
code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", '"""')))
check("no delete/archive/trash calls in the code",
      not any(word in code for word in (".delete(", "in_trash=", "archived=", ".blocks.")))

print("--- write log ---")
lines = open(w.WRITE_LOG_PATH, encoding="utf-8").read().splitlines()
updates_run = len(CASES) + 3 + 2  # CASES + 3 single checks + 2 clearing checks
check(f"every successful write is logged (1 add + {updates_run} updates)", len(lines) == 1 + updates_run)

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
raise SystemExit(1 if failures else 0)
