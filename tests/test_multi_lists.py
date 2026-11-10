"""Tests for searching several Notion lists (list 1 + extra lists), WITHOUT real Notion.
It replaces Notion with three fake lists.
Run with:  python -m tests.test_multi_lists"""

from services import notion_search as ns
from services import notion_write as w

failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def page(pid: str, name: str, link: str, tags=("Sủng",)) -> dict:
    return {"id": pid, "url": f"https://notion.so/{pid}", "properties": {
        "Name": {"title": [{"plain_text": name}]},
        "Author": {"rich_text": [{"plain_text": "Tác giả"}]},
        "Chapters": {"number": 20},
        "Tags": {"multi_select": [{"name": t} for t in tags]},
        "Link": {"url": link},
    }}


# Three lists. List 3 is the only one with the tag "Cải trang". "Dup" is in lists 1 and 2.
TABLES = {
    "id1": {"tags": ["Sủng", "HE"], "pages": [page("a1", "Truyện A1", "https://x.com/a1"),
                                              page("dup1", "Dup", "https://x.com/dup")]},
    "id2": {"tags": ["Sủng", "HE"], "pages": [page("b1", "Truyện B1", "https://x.com/b1"),
                                              page("dup2", "Dup", "http://www.x.com/dup/")]},
    "id3": {"tags": ["Sủng", "Cải trang"], "pages": [page("c1", "Truyện C1", "https://x.com/c1", ["Cải trang"])]},
}


class FakePages:
    """Records every page that is created, and answers like Notion (with an automatic ID)."""
    def __init__(self):
        self.created = []

    def create(self, parent, properties):
        self.created.append({"parent": parent, "properties": properties})
        return {"id": "new-page", "url": "https://notion.so/new-page", "properties": {
            "ID": {"type": "unique_id", "unique_id": {"prefix": "L3", "number": 90}}}}


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
ns.NOTION_DATA_SOURCE_ID = "id1"
ns.NOTION_ADD_DATA_SOURCE_ID = None
ns.EXTRA_SOURCE_IDS = ["id2", "id3", "id2", "id1"]  # repeats are ignored
names = lambda r: [s["name"] for s in r["stories"]]

print("--- which lists are searched ---")
check("list 1 first, then the extras, no repeats", ns._search_sources() == ["id1", "id2", "id3"])
ns.EXTRA_SOURCE_IDS = ["id2", "id3"]
check("all tags = union of the lists", ns.get_all_tags() == ["Sủng", "HE", "Cải trang"])
check("list 1 tags stay list 1 tags", ns.get_valid_tags() == ["Sủng", "HE"])

print("--- search joins all lists ---")
result = ns.search_stories()
check("every list is queried once", [q["data_source_id"] for q in fake.queries] == ["id1", "id2", "id3"])
check("stories from all lists, list 1 first", names(result) == ["Truyện A1", "Dup", "Truyện B1", "Truyện C1"])
check("same link in two lists is counted once", result["total"] == 4)
a, b = ns.search_stories(seed=3), ns.search_stories(seed=3)
check("seed shuffle is repeatable across lists", names(a) == names(b))

print("--- tags that only some lists have ---")
fake.queries.clear()
result = ns.search_stories(tags=["cải trang"])
check("a tag only list 3 has finds list 3's stories", names(result) == ["Truyện C1"])
check("lists without that tag are not even asked", [q["data_source_id"] for q in fake.queries] == ["id3"])
fake.queries.clear()
ns.search_stories(tags=["he"])
check("a tag lists 1 and 2 have skips list 3", [q["data_source_id"] for q in fake.queries] == ["id1", "id2"])
try:
    ns.search_stories(tags=["không có tag này"])
    error = ""
except ValueError as e:
    error = str(e)
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
check("all three lists are checked", [q["data_source_id"] for q in fake.queries] == ["id1", "id2", "id3"])
check("duplicates from lists 1 and 2 are found, with the list number", [(d["id"], d["list"]) for d in dups] == [("dup1", 1), ("dup2", 2)])

print("--- /edit finder stays on list 1 ---")
fake.queries.clear()
ns.find_books("A")
check("find_books only asks list 1", [q["data_source_id"] for q in fake.queries] == ["id1"])

print("--- no extra lists = same as before ---")
ns.EXTRA_SOURCE_IDS = []
fake.queries.clear()
check("only list 1 is searched", names(ns.search_stories()) == ["Truyện A1", "Dup"] and len(fake.queries) == 1)

print("--- /add writes to the 'add' list (list 3), /edit tags stay on list 1 ---")
w._fetch_tag_ids.cache_clear()
w.WRITE_LOG_PATH = __import__("os").path.join(__import__("tempfile").mkdtemp(), "writes.log")  # never touch the real log
ns.EXTRA_SOURCE_IDS = ["id2", "id3"]
ns.NOTION_ADD_DATA_SOURCE_ID = "id3"
check("add_source_id is the list set for /add", ns.add_source_id() == "id3")
check("the add list is list number 3", ns.list_number("id3") == 3 and ns.list_number("id1") == 1)
result = w.add_story("Truyện mới", "Tác giả", 50, ["Cải trang"], "https://x.com/new")
check("the new story is created in the add list", fake.pages.created[-1]["parent"]["data_source_id"] == "id3")
check("tags are checked against the add list (list 3 has 'Cải trang')", result["name"] == "Truyện mới")
check("the result says which list and the automatic ID", result["list"] == 3 and result["code"] == "L3-90")
try:
    w.build_column_update("tags", ["Cải trang"])
    edit_error = ""
except ValueError as e:
    edit_error = str(e)
check("/edit tags use list 1's tags ('Cải trang' does not exist there)", "Cải trang" in edit_error and "không hợp lệ" in edit_error)
ids_before = len(fake.pages.created)
try:
    w.add_story("X", "Y", 5, ["Tag không có"], "https://x.com/x")
except ValueError:
    pass
check("an unknown tag writes nothing", len(fake.pages.created) == ids_before)
check("update_story still only accepts list 1 pages (id1)", w.notion_search.NOTION_DATA_SOURCE_ID == "id1")

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

ns.NOTION_ADD_DATA_SOURCE_ID = "id3"
ID_COLUMN["id3"] = "unique_id"
w.add_story("Tự động", "Tác giả", 10, ["Sủng"], "https://x.com/auto")
check("automatic ID column (list 3): the bot sends NO ID, Notion numbers it", "ID" not in fake.pages.created[-1]["properties"])

ns.NOTION_ADD_DATA_SOURCE_ID = "id1"
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
ns.NOTION_ADD_DATA_SOURCE_ID = "id3"

print("--- the add list is always searchable ---")
ns.EXTRA_SOURCE_IDS = []
check("an add list missing from the extra lists is still searched", ns._search_sources() == ["id1", "id3"])
ns.NOTION_ADD_DATA_SOURCE_ID = None
check("no add list set = list 1 (as before)", ns.add_source_id() == "id1" and ns._search_sources() == ["id1"])

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
raise SystemExit(1 if failures else 0)
