"""Tests for working with several Notion lists, WITHOUT real Notion.
It replaces Notion with three fake lists and checks: the settings, the search, the /add
duplicate warning, where /add writes, the ID of a new story, and editing in any list.
Run with:  python -m tests.test_multi_lists"""

import os
import tempfile

from services import notion_search as ns
from services import notion_write as w

failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def raises(func, *args, **kwargs) -> str:
    """The ValueError message if func raises ValueError, else ''."""
    try:
        func(*args, **kwargs)
    except ValueError as error:
        return str(error)
    return ""


def page(pid: str, name: str, link: str, tags=("Sủng",), source="id1", code=None) -> dict:
    """A row of a fake list, in the shape Notion returns it."""
    prefix, _, number = (code or "").partition("-")
    return {"id": pid, "object": "page", "url": f"https://notion.so/{pid}",
            "parent": {"type": "data_source_id", "data_source_id": source},
            "properties": {
                "Name": {"title": [{"plain_text": name}]},
                "Author": {"rich_text": [{"plain_text": "Tác giả"}]},
                "Chapters": {"number": 20},
                "Tags": {"multi_select": [{"name": t} for t in tags]},
                "Link": {"url": link},
                "Status": {"status": {"name": "Full"}},
                "Reading Status ": {"status": {"name": "Not started"}},
                "Record Audio": {"status": {"name": "Not started"}},
                "Note": {"rich_text": []},
                "Date": {"date": None},
                "ID": {"type": "unique_id", "unique_id": {"prefix": prefix, "number": int(number)}} if code else {},
            }}


# Three lists. List 3 is the only one with the tag "Cải trang". "Dup" is in lists 1 and 2.
TABLES = {
    "id1": {"tags": ["Sủng", "HE"], "pages": [page("a1", "Truyện A1", "https://x.com/a1", code="L1-1"),
                                              page("dup1", "Dup", "https://x.com/dup", code="L1-2")]},
    "id2": {"tags": ["Sủng", "HE"], "pages": [page("b1", "Truyện B1", "https://x.com/b1", source="id2", code="L2-1"),
                                              page("dup2", "Dup", "http://www.x.com/dup/", source="id2", code="L2-2")]},
    "id3": {"tags": ["Sủng", "Cải trang"],
            "pages": [page("c1", "Truyện C1", "https://x.com/c1", ["Cải trang"], source="id3", code="L3-91")]},
}


class FakePages:
    """Pretends to be Notion's pages: records what is created and what is updated."""

    def __init__(self):
        self.created = []
        self.updates = []

    def create(self, parent, properties):
        self.created.append({"parent": parent, "properties": properties})
        return {"id": "new-page", "url": "https://notion.so/new-page", "properties": {
            "ID": {"type": "unique_id", "unique_id": {"prefix": "L3", "number": 90}}}}

    def _find(self, page_id):
        for table in TABLES.values():
            for p in table["pages"]:
                if p["id"] == page_id:
                    return p
        raise KeyError(page_id)

    def retrieve(self, page_id):
        return self._find(page_id)

    def update(self, page_id, properties):
        """Apply the change and answer in the shape Notion uses when READING a page."""
        self.updates.append((page_id, properties))
        updated = dict(self._find(page_id))
        source = updated["parent"]["data_source_id"]
        tag_name = {f"{source}:{t}": t for t in TABLES[source]["tags"]}  # option id -> tag name
        changed = {}
        for column, value in properties.items():
            if "rich_text" in value:
                changed[column] = {"rich_text": [{"plain_text": t["text"]["content"]} for t in value["rich_text"]]}
            elif "multi_select" in value:
                changed[column] = {"multi_select": [{"name": tag_name[o["id"]]} for o in value["multi_select"]]}
            else:
                changed[column] = value
        updated["properties"] = {**updated["properties"], **changed}
        return updated


class FakeNotion:
    def __init__(self):
        self.queries = []
        self.broken = None  # a list id that fails
        self.pages = FakePages()

    @property
    def data_sources(self):
        return self

    def retrieve(self, data_source_id):
        options = [{"name": t, "id": f"{data_source_id}:{t}"} for t in TABLES[data_source_id]["tags"]]
        return {"properties": {"Tags": {"multi_select": {"options": options}}}}

    def query(self, **kw):
        self.queries.append(kw)
        if kw["data_source_id"] == self.broken:
            raise RuntimeError("list not shared")
        return {"results": TABLES[kw["data_source_id"]]["pages"], "has_more": False, "next_cursor": None}


fake = FakeNotion()
ns._client = lambda: fake
ns._fetch_tags.cache_clear()
w._fetch_tag_ids.cache_clear()
w.WRITE_LOG_PATH = os.path.join(tempfile.mkdtemp(), "writes.log")  # never touch the real log
names = lambda r: [s["name"] for s in r["stories"]]
queried = lambda: [q["data_source_id"] for q in fake.queries]

# ---------------------------------------------------------------------------
print("--- the settings in .env ---")
check("'id1, id2,,id3 ,id1' -> three ids, spaces and repeats removed", ns._split_ids("id1, id2,,id3 ,id1") == ["id1", "id2", "id3"])
check("an empty setting gives no ids", ns._split_ids("") == [] and ns._split_ids(None) == [])
ns.DATA_SOURCE_IDS, ns.ADD_SOURCE_ID = ["id1", "id2", "id3"], None
check("the lists are used in .env order", ns.all_sources() == ["id1", "id2", "id3"])
check("the first list is the main one", ns.main_source_id() == "id1" and ns.list_number("id1") == 1 and ns.list_number("id3") == 3)
check("an unknown list has number 0", ns.list_number("nope") == 0)
check("no /add setting = /add writes to the first list", ns.add_source_id() == "id1")
ns.ADD_SOURCE_ID = "id3"
check("the /add setting picks the list", ns.add_source_id() == "id3")
ns.DATA_SOURCE_IDS = ["id1", "id2"]
check("an /add list that is not in the main setting is still used (last)", ns.all_sources() == ["id1", "id2", "id3"])
ns.DATA_SOURCE_IDS, ns.ADD_SOURCE_ID = ["id1", "id2", "id3"], None
check("all tags = union of the lists", ns.get_all_tags() == ["Sủng", "HE", "Cải trang"])
check("the default tags are the first list's", ns.get_valid_tags() == ["Sủng", "HE"])

# ---------------------------------------------------------------------------
print("--- search joins all lists ---")
fake.queries.clear()
result = ns.search_stories()
check("every list is queried once", queried() == ["id1", "id2", "id3"])
check("stories from all lists, list 1 first", names(result) == ["Truyện A1", "Dup", "Truyện B1", "Truyện C1"])
check("same link in two lists is counted once", result["total"] == 4)
a, b = ns.search_stories(seed=3), ns.search_stories(seed=3)
check("seed shuffle is repeatable across lists", names(a) == names(b))

print("--- tags that only some lists have ---")
fake.queries.clear()
result = ns.search_stories(tags=["cải trang"])
check("a tag only list 3 has finds list 3's stories", names(result) == ["Truyện C1"])
check("lists without that tag are not even asked", queried() == ["id3"])
fake.queries.clear()
ns.search_stories(tags=["he"])
check("a tag lists 1 and 2 have skips list 3", queried() == ["id1", "id2"])
error = raises(ns.search_stories, tags=["không có tag này"])
check("a tag no list has is a clear error listing all tags", "Cải trang" in error and "HE" in error)

print("--- one list failing is not hidden ---")
fake.broken = "id2"
try:
    ns.search_stories()
    failed = False
except RuntimeError:
    failed = True
check("the error reaches the caller (no silent 2-list answer)", failed)
fake.broken = None

print("--- /add duplicate check looks in every list ---")
fake.queries.clear()
dups = ns.find_duplicates(name="Dup", link="https://x.com/dup")
check("all three lists are checked", queried() == ["id1", "id2", "id3"])
check("duplicates from lists 1 and 2 are found, with the list number", [(d["id"], d["list"]) for d in dups] == [("dup1", 1), ("dup2", 2)])

print("--- one list only = same as before ---")
ns.DATA_SOURCE_IDS = ["id1"]
fake.queries.clear()
check("only the first list is searched", names(ns.search_stories()) == ["Truyện A1", "Dup"] and queried() == ["id1"])
ns.DATA_SOURCE_IDS = ["id1", "id2", "id3"]

# ---------------------------------------------------------------------------
print("--- /add writes to the 'add' list (list 3) ---")
ns.ADD_SOURCE_ID = "id3"
result = w.add_story("Truyện mới", "Tác giả", 50, ["Cải trang"], "https://x.com/new")
check("the new story is created in the add list", fake.pages.created[-1]["parent"]["data_source_id"] == "id3")
check("tags are checked against the add list (list 3 has 'Cải trang')",
      fake.pages.created[-1]["properties"]["Tags"]["multi_select"] == [{"id": "id3:Cải trang"}])
check("the result says which list and the automatic ID", result["list"] == 3 and result["code"] == "L3-90")
before = len(fake.pages.created)
raises(w.add_story, "X", "Y", 5, ["Tag không có"], "https://x.com/x")
check("an unknown tag writes nothing", len(fake.pages.created) == before)

print("--- the ID of a new story ---")
ID_COLUMN = {}      # what the fake says about the ID column of a list: {} = none
ID_ROWS = {}        # list id -> existing ID texts of that list
orig_retrieve, orig_query = fake.retrieve, fake.query


def retrieve_with_id(data_source_id):
    info = orig_retrieve(data_source_id)
    if data_source_id in ID_COLUMN:
        info["properties"]["ID"] = {"type": ID_COLUMN[data_source_id]}
    return info


def query_with_id(**kw):
    if kw["data_source_id"] in ID_ROWS:
        pages = [{"properties": {"ID": {"rich_text": [{"plain_text": t}] if t else []}}} for t in ID_ROWS[kw["data_source_id"]]]
        return {"results": pages, "has_more": False, "next_cursor": None}
    return orig_query(**kw)


fake.retrieve, fake.query = retrieve_with_id, query_with_id
ID_COLUMN["id3"] = "unique_id"
w.add_story("Tự động", "Tác giả", 10, ["Sủng"], "https://x.com/auto")
check("automatic ID column (list 3): the bot sends NO ID, Notion numbers it", "ID" not in fake.pages.created[-1]["properties"])

ns.ADD_SOURCE_ID = "id1"
ID_COLUMN["id1"] = "rich_text"
ID_ROWS["id1"] = ["L1-1", "L1-26", "L1-200", "L1-9", ""]
w.add_story("Văn bản", "Tác giả", 10, ["Sủng"], "https://x.com/text")
sent_id = fake.pages.created[-1]["properties"]["ID"]["rich_text"][0]["text"]["content"]
check("text ID column (list 1): the next number is counted (highest 200 -> L1-201)", sent_id == "L1-201")
ID_ROWS["id1"] = []
w.add_story("Rỗng", "Tác giả", 10, ["Sủng"], "https://x.com/empty")
check("text ID column with no earlier ID: nothing is guessed", "ID" not in fake.pages.created[-1]["properties"])
ID_COLUMN.clear(); ID_ROWS.clear()
w.add_story("Không ID", "Tác giả", 10, ["Sủng"], "https://x.com/none")
check("a list with no ID column: nothing is sent", "ID" not in fake.pages.created[-1]["properties"])
fake.retrieve, fake.query = orig_retrieve, orig_query
ns.ADD_SOURCE_ID = None

# ---------------------------------------------------------------------------
print("--- /edit finds books in every list ---")
fake.queries.clear()
found = ns.find_books("A")
check("find_books asks every list", queried() == ["id1", "id2", "id3"])
check("books come with their list, ID and page id", [(b["id"], b["list"], b["code"], b["source_id"]) for b in found["books"]][:2]
      == [("a1", 1, "L1-1", "id1"), ("dup1", 1, "L1-2", "id1")])
check("a book of list 3 can be found with its ID", ("c1", 3, "L3-91") in [(b["id"], b["list"], b["code"]) for b in found["books"]])
found = ns.find_books("A", limit=3)
check("more matches than the limit: only `limit` books, and has_more is True", len(found["books"]) == 3 and found["has_more"] is True)

print("--- /edit changes a story in ANY list ---")
for pid, label in (("a1", "list 1"), ("b1", "list 2"), ("c1", "list 3")):
    fake.pages.updates.clear()
    result = w.update_story(pid, "note", "ghi chú thử")
    check(f"a note can be changed in {label}", fake.pages.updates == [(pid, {"Note": {"rich_text": [{"type": "text", "text": {"content": "ghi chú thử"}}]}})])
result = w.update_story("c1", "note", "đã sửa")
check("the result says which list and ID the story has", result["list"] == 3 and result["code"] == "L3-91")
check("get_story reads a story of list 3", w.get_story("c1")["name"] == "Truyện C1")
fake.pages.updates.clear()
w.update_story("c1", "tags", ["Cải trang", "Sủng"])
check("tags of a list-3 story use LIST 3's tag ids",
      fake.pages.updates[-1][1]["Tags"]["multi_select"] == [{"id": "id3:Cải trang"}, {"id": "id3:Sủng"}])
check("'Cải trang' is refused for a list-1 story (that list has no such tag)",
      "không hợp lệ" in raises(w.update_story, "a1", "tags", ["Cải trang"]))
fake.pages.updates.clear()
w.update_story("a1", "tags", ["HE"])
check("tags of a list-1 story use LIST 1's tag ids", fake.pages.updates[-1][1]["Tags"]["multi_select"] == [{"id": "id1:HE"}])

print("--- /edit never touches a page of another table ---")
STRANGER = page("z1", "Của bảng khác", "https://x.com/z1", source="some-other-table")   # a table that is NOT one of ours
TABLES["other"] = {"tags": ["Sủng"], "pages": [STRANGER]}
fake.pages.updates.clear()
check("a page of a table that is not in .env is refused", "không thuộc các bảng" in raises(w.update_story, "z1", "note", "x"))
check("... reading it is refused too", raises(w.get_story, "z1") != "")
check("... and nothing was written", fake.pages.updates == [])
STRANGER["parent"]["data_source_id"] = "id2"
STRANGER["in_trash"] = True
check("a deleted (trashed) page is refused even in our list", "đã bị xóa" in raises(w.update_story, "z1", "note", "x"))
del TABLES["other"]

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
raise SystemExit(1 if failures else 0)
