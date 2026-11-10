"""Turns a search result into the text the user reads. Plain Python: no Gemini, no Notion,
no Telegram. (Gemini only picks the search conditions; this file writes the answer.)"""

# Telegram refuses messages longer than 4096 characters; stay a little under
MAX_LENGTH = 3900

# How the search conditions are shown to the user
READING_STATUS_TEXT = {
    "not started": "chưa đọc",
    "in progress": "đang đọc",
    "done": "đã đọc xong",
    "beginning + end": "chỉ đọc đầu và cuối",
    "drop": "đã bỏ (không đọc nữa)",
}
STATUS_TEXT = {
    "full": "truyện đã hoàn",
    "in progress": "truyện đang ra",
    "drop": "truyện tác giả bỏ dở",
}


def describe_filters(filters: dict) -> str:
    """The search conditions in one short phrase, e.g. 'tags Sủng + HE, chưa đọc, tối đa 99 chương'."""
    parts = []
    tags = [t.strip() for t in (filters.get("tags") or []) if t and t.strip()]
    if tags:
        parts.append(("tag " if len(tags) == 1 else "tags ") + " + ".join(tags))
    reading = filters.get("reading_status")
    if reading:
        parts.append(READING_STATUS_TEXT.get(reading.strip().casefold(), f"tiến độ đọc: {reading}"))
    status = filters.get("status")
    if status:
        parts.append(STATUS_TEXT.get(status.strip().casefold(), f"tình trạng: {status}"))
    if filters.get("max_chapters") is not None:
        parts.append(f"tối đa {filters['max_chapters']} chương")
    if filters.get("author"):
        parts.append(f"tác giả {filters['author'].strip()}")
    return ", ".join(parts) if parts else "không có điều kiện nào"


def _story_lines(number: int, story: dict, with_tags: bool) -> str:
    """One story: '1. Name - Author - 56 chương', then tags, then the link."""
    head = f"{number}. {story['name']}"
    if story.get("author"):
        head += f" - {story['author']}"
    chapters = story.get("chapters")
    head += f" - {chapters:g} chương" if isinstance(chapters, (int, float)) else " - chưa rõ số chương"
    lines = [head]
    if with_tags and story.get("tags"):
        lines.append("   Tags: " + ", ".join(story["tags"]))
    if story.get("link"):
        lines.append("   " + story["link"])
    return "\n".join(lines)


def render_result(result: dict, filters: dict, continued: bool = False) -> str:
    """The full answer for one search result (the dict that search_stories returns)."""
    conditions = describe_filters(filters)
    total, skip, shown = result["total"], result["skip"], result["shown"]

    if total == 0:
        return (f"Không tìm thấy truyện nào khớp: {conditions}.\n"
                "Bạn thử bỏ bớt một điều kiện (một tag, tiến độ đọc hoặc giới hạn số chương) nhé.")
    if shown == 0:
        # "xem thêm" after the last batch
        return f"Đã hết rồi: bạn đã xem hết {total} truyện của lần tìm này ({conditions})."

    if continued:
        header = f"Tiếp theo, truyện {skip + 1}-{skip + shown} trong {total} truyện ({conditions}):"
    elif total > shown:
        header = f"Tìm thấy {total} truyện ({conditions}). Gợi ý ngẫu nhiên {shown} truyện:"
    else:
        header = f"Tìm thấy {total} truyện ({conditions}):"

    footer = ""
    if result["remaining"] > 0:
        footer = f'\nCòn {result["remaining"]} truyện nữa, nhắn "xem thêm" để xem tiếp.'

    def build(with_tags: bool) -> str:
        """Write the whole answer; with_tags=False leaves out the tags lines to make it shorter."""
        stories = [_story_lines(skip + i, s, with_tags) for i, s in enumerate(result["stories"], start=1)]
        return header + "\n\n" + "\n".join(stories) + "\n" + footer

    text = build(with_tags=True)
    if len(text) > MAX_LENGTH:  # very long links: drop the tags lines so nothing is cut off
        text = build(with_tags=False)
    return text.rstrip()


def history_note(result: dict, filters: dict, continued: bool = False) -> str:
    """One short line about what was shown. It goes into the conversation memory instead of the
    whole list, so Gemini knows what happened without paying for the stories on every question."""
    if result["total"] == 0:
        return f"Không có truyện nào khớp ({describe_filters(filters)})."
    if result["shown"] == 0:
        return f"Đã hết, không còn truyện nào nữa ({describe_filters(filters)})."
    seen = result["skip"] + result["shown"]
    return (f"Đã hiển thị truyện {result['skip'] + 1}-{seen} trong {result['total']} truyện "
            f"({describe_filters(filters)}). Còn {result['remaining']} truyện nữa.")
