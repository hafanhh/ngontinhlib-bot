"""Tests for the random pick and 'xem thêm' paging, WITHOUT Notion and WITHOUT Gemini.
It replaces Notion with a fake table of 25 stories.
Run with:  python -m tests.test_paging"""

from services import notion_search
from services import gemini_agent as g

notion_search.EXTRA_SOURCE_IDS = []  # these tests are about ONE list; test_multi_lists covers several
notion_search.NOTION_ADD_DATA_SOURCE_ID = None  # and /add writes to list 1 here

failures = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def make_page(number: int) -> dict:
    return {"id": f"p{number}", "url": f"https://notion.so/p{number}", "properties": {
        "Name": {"title": [{"plain_text": f"Truyện {number:02d}"}]},
        "Author": {"rich_text": [{"plain_text": "Tác giả"}]},
        "Chapters": {"number": 10 + number},
        "Tags": {"multi_select": [{"name": "Sủng"}]},
        "Link": {"url": f"https://example.com/{number}"},
    }}


class FakeTable:
    """Pretends to be Notion's table query. Serves 25 stories in pages of 100."""

    def __init__(self, count=25):
        self.pages = [make_page(i) for i in range(1, count + 1)]
        self.queries = []

    @property
    def data_sources(self):
        return self

    def query(self, **kw):
        self.queries.append(kw)
        return {"results": self.pages, "has_more": False, "next_cursor": None}


table = FakeTable()
notion_search._client = lambda: table
names = lambda result: [s["name"] for s in result["stories"]]
ALL = [f"Truyện {i:02d}" for i in range(1, 26)]

print("--- search_stories: order, skip and seed ---")
plain = notion_search.search_stories()
check("no seed keeps Notion's order", names(plain) == ALL[:10])
check("reports total, skip, shown, remaining", (plain["total"], plain["skip"], plain["shown"], plain["remaining"]) == (25, 0, 10, 15))
a = notion_search.search_stories(seed=1)
b = notion_search.search_stories(seed=1)
c = notion_search.search_stories(seed=2)
check("same seed gives the same order", names(a) == names(b))
check("a different seed gives a different order", names(a) != names(c))
check("a shuffled batch is not just Notion's first 10", names(a) != ALL[:10])
batches = [notion_search.search_stories(seed=7, skip=skip) for skip in (0, 10, 20)]
seen = [n for batch in batches for n in names(batch)]
check("batches are 10, 10 and 5 stories", [b["shown"] for b in batches] == [10, 10, 5])
check("remaining counts down 15, 5, 0", [b["remaining"] for b in batches] == [15, 5, 0])
check("paging through a seed shows every story exactly once", sorted(seen) == ALL and len(seen) == len(set(seen)))
past_end = notion_search.search_stories(seed=7, skip=30)
check("skipping past the end gives nothing", past_end["stories"] == [] and past_end["remaining"] == 0 and past_end["total"] == 25)

print("--- find_stories (the tool Gemini calls): a search state per chat ---")
g._search_state.clear()
g._current_chat_id = 1
first = g.find_stories(author="x")
check("a new search starts at the first batch, not continued", first["skip"] == 0 and first["continued"] is False)
check("it is shuffled, not Notion's first 10", names(first) != ALL[:10])
second = g.find_stories(next_page=True)
check("'xem thêm' continues the same search", second["continued"] is True and second["skip"] == 10)
check("'xem thêm' shows different stories (no repeats)", not set(names(first)) & set(names(second)))
third = g.find_stories(next_page=True)
check("third batch has the last 5", third["shown"] == 5 and third["remaining"] == 0)
all_three = names(first) + names(second) + names(third)
check("all 25 stories shown once across the 3 batches", sorted(all_three) == ALL)
fourth = g.find_stories(next_page=True)
check("after the end, 'xem thêm' gives nothing (and says remaining 0)", fourth["stories"] == [] and fourth["remaining"] == 0)

print("--- conditions are reused for 'xem thêm' ---")
g._search_state.clear()
table.queries.clear()
g.find_stories(author="Lựu", status="Full")
g.find_stories(author="KHÁC HẲN", next_page=True)   # Gemini repeated different filters by mistake
used_filters = [q.get("filter") for q in table.queries]
check("'xem thêm' queries Notion with the ORIGINAL conditions, ignoring new ones",
      used_filters[0] == used_filters[1])

print("--- a new question reshuffles ---")
g._search_state.clear()
seeds = []
for _ in range(2):
    g.find_stories()
    seeds.append(g._search_state[1]["seed"])
check("each new search gets a new shuffle", seeds[0] != seeds[1])
g.find_stories(next_page=True)
check("'xem thêm' keeps the shuffle of the last search", g._search_state[1]["seed"] == seeds[1])
g.find_stories(tags=None, author="đổi điều kiện")   # a changed question, next_page left off
check("a changed question starts again from the first batch", g._search_state[1]["skip"] == 10)

print("--- chats do not mix ---")
g._search_state.clear()
g._current_chat_id = 1
g.find_stories()
g._current_chat_id = 2
other = g.find_stories(next_page=True)
check("'xem thêm' in a chat with no earlier search is an error, not a silent new search",
      "error" in other and "stories" not in other and 2 not in g._search_state)
g._current_chat_id = 1
mine = g.find_stories(next_page=True)
check("chat 1 still continues its own search", mine["continued"] is True and mine["skip"] == 10)

print("--- 'xem thêm' right after a restart ---")
g._search_state.clear()
g._current_chat_id = 5
queries_before = len(table.queries)
restart = g.find_stories(next_page=True)
check("no earlier search: tells Gemini to ask the user to search again", "hỏi lại" in restart.get("error", ""))
check("... and does not query Notion at all", len(table.queries) == queries_before)

print("--- a Notion that says has_more but gives no cursor cannot loop forever ---")
class StuckTable(FakeTable):
    def query(self, **kw):
        self.queries.append(kw)
        if len(self.queries) > 5:
            raise AssertionError("looped more than 5 times")
        return {"results": self.pages, "has_more": True, "next_cursor": None}
stuck = StuckTable(3)
notion_search._client = lambda: stuck
result = notion_search.search_stories()
check("stops after one query when there is no next_cursor", len(stuck.queries) == 1 and result["total"] == 3)
notion_search._client = lambda: table

print("--- tag synonyms are checked against the real tag list ---")
import logging
logged = []
class Grab(logging.Handler):
    def emit(self, record): logged.append(record.getMessage())
logging.getLogger("services.notion_search").addHandler(Grab())
real_tags = notion_search.get_valid_tags
notion_search.get_valid_tags = lambda source_id=None: ["Sủng", "Trùng sinh", "Trọng sinh"]
both = notion_search.build_filter(tags=["trùng sinh"])["and"][0]
check("both real names are searched (OR) when both exist", [c["multi_select"]["contains"] for c in both["or"]] == ["Trùng sinh", "Trọng sinh"])
check("no warning when everything exists", logged == [])
notion_search.get_valid_tags = lambda source_id=None: ["Sủng", "Trùng sinh"]          # 'Trọng sinh' was renamed or deleted in Notion
one = notion_search.build_filter(tags=["Trùng sinh"])["and"][0]
check("a missing synonym is skipped, the search still works", one == {"property": "Tags", "multi_select": {"contains": "Trùng sinh"}})
check("a missing synonym is reported in the log", any("Trọng sinh" in m and "TAG_SYNONYMS" in m for m in logged))
notion_search.get_valid_tags = real_tags

print("--- errors still reach Gemini as a message ---")
g._search_state.clear()
check("an unknown status becomes an error result, not a crash", "error" in g.find_stories(status="Maybe"))

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
raise SystemExit(1 if failures else 0)
