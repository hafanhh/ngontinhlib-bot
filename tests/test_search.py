"""Phase 1 test: run a few searches in the terminal and compare with Notion by hand.
Run with:  python -m tests.test_search"""

from services.notion_search import search_stories

# Each test = a name + the filters to use
TESTS = [
    ("Sủng + HE, chưa đọc, dưới 100 chương",
     dict(tags=["Sủng", "HE"], reading_status="Not started", max_chapters=100)),
    ("Cổ đại + Cung đấu, đã hoàn",
     dict(tags=["cổ đại", "cung đấu"], status="Full")),
    ("Ngọt + Nhẹ nhàng, đã đọc xong",
     dict(tags=["Ngọt", "Nhẹ nhàng"], reading_status="Done")),
]

for title, filters in TESTS:
    print(f"\n=== {title} ===")
    print(f"Bộ lọc: {filters}")
    result = search_stories(**filters)
    stories = result["stories"]
    if not stories:
        print("(không có kết quả)")
    for i, s in enumerate(stories, 1):
        print(f"{i}. {s['name']} | {s['author']} | {s['chapters']} chương")
        print(f"   Tags: {', '.join(s['tags'])}")
        print(f"   {s['link']}")
    print(f"-> tìm thấy {result['total']} truyện, hiển thị {result['shown']} (tối đa 10)")
