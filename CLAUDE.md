# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A personal Telegram chatbot that will search the owner's Notion database of novels. The owner is a coding beginner, so explain steps simply, keep short comments in code, and write user-facing docs (README.md) in Vietnamese.

The roadmap is the owner's plan document `Kế hoạch chi tiết Chatbot Telegram tìm truyện (Notion + Gemini).docx` (phases 0-5; finish one phase's "done when" check before the next). Phases 0-2 are done and verified live (sample questions give 0 / 2 / 1 / 7 stories). **Phase 3** (per-`chat_id` memory, tag synonyms, error handling and logging) is implemented; the owner still has to set the API spending limit and test on Telegram. **Phase 4** (add/update stories) is in progress: the data layer (`services/notion_write.py`, plus `find_books` / `find_duplicates` in `services/notion_search.py`) is done and tested only against a fake Notion. `/add` (`flows/add_flow.py`) and `/edit` (`flows/edit_flow.py`) are built and tested offline only; the owner now has to enable the integration's write capabilities and does a real test (4.6-4.7). See "Notion write" below. The search now picks randomly, defaults to unread books and supports "xem thêm" paging (see "Gemini" and "Notion search" below); the owner's main purpose for the bot is finding a NEW book to read.

- LLM provider is the **Gemini API**, not Claude/Anthropic. Do not install `anthropic`; the SDK is `google-genai`. Model comes from `GEMINI_MODEL` in `.env`, default `gemini-3.6-flash` (`gemini-2.5-flash` is deprecated; check the Google model list before changing).
- The Notion integration is **read-only** until Phase 4; no delete feature ever (use Reading Status = Drop instead).
- New secrets go in `.env.example` as empty keys (e.g. `GEMINI_API_KEY=`).

## Commands

```bash
source .venv/bin/activate        # activate the venv (Python 3.13)
pip install -r requirements.txt  # rebuild the venv on a new machine
python bot.py                    # run the bot (long polling, stop with Ctrl+C)
python -m tests.test_search      # Phase 1: run 3 sample Notion searches (real Notion, read-only)
python -m tests.test_gemini      # Phase 2: ask the 4 sample questions via real Gemini (needs all keys)
python -m tests.test_gemini "câu hỏi"  # ask one custom question
python -m tests.test_write       # offline tests (no network except a read-only tag lookup):
python -m tests.test_add_flow    #   also test_edit_flow, test_paging, test_errors, test_usage (all offline/local; test_errors only uses servers on this computer)
# Run tests from the project root with `python -m tests.<name>`; `python tests/<name>.py` fails to import `services`.
pip freeze > requirements.txt    # refresh pins after installing a package
```

There are no tests or linter configured yet.

## Architecture

## Layout

```
bot.py            entry point (`python bot.py`): config check, logging, error messages, handler wiring
services/         no Telegram code: notion_search.py (read), notion_write.py (the ONLY writer), gemini_agent.py
flows/            Telegram conversations: __init__.py (joins /add + /edit), add_flow.py, edit_flow.py
tests/            test scripts, run with `python -m tests.<name>`
logs/             bot.log (errors) and writes.log (every Notion write); git-ignored, created automatically
```

Import rule: `bot.py` → `flows` and `services`; `flows` → `services`; `services` never imports `flows` or `bot`. Imports are absolute (`from services import notion_search`). `load_dotenv()` finds the root `.env` from inside `services/` by walking up.

Flow: Telegram message → `bot.py` → `services/gemini_agent.answer_question` → Gemini picks filters by calling the `find_stories` tool → `services/notion_search.search_stories` queries Notion → result goes back to Gemini, which writes the Vietnamese answer. Gemini never touches Notion directly; our code runs the query.

### Bot (`bot.py`)

Built on `python-telegram-bot` (v22, async, long polling via `app.run_polling()`).

- **Config** is read from `.env` with `python-dotenv`. The script exits with a message at startup if `BOT_TOKEN`, `TELEGRAM_USER_ID` (must be numeric), `NOTION_TOKEN`, `NOTION_DATA_SOURCE_ID` or `GEMINI_API_KEY` is missing.
- **Single-user access control** is done with a `filters.User(user_id=ALLOWED_USER_ID)` filter attached to every handler (`only_me`). Messages from anyone else match no handler and are silently ignored. Any new handler must include this filter, or it will respond to strangers.
- Free text (`only_me & filters.TEXT & ~filters.COMMAND`) goes to `answer_question(chat_id, text)` inside `asyncio.to_thread`, because the Gemini and Notion SDK calls used are synchronous.
- Logging goes to the terminal and a rotating `logs/bot.log` (git-ignored). The `httpx`, `httpcore`, `httpx2` and `httpcore2` loggers are set to WARNING because their INFO logs print every request URL (Telegram URLs contain the bot token; Gemini's library uses `httpx2`). Keep it that way. Do not log the text of a Telegram `NetworkError` (it can contain the token); `on_error` logs only its type.
- google-genai uses the `httpx2` library, whose errors do NOT inherit from `httpx`'s. `friendly_error()` therefore checks both (`NETWORK_ERRORS` / `TIMEOUT_ERRORS`). It names the real cause: Notion `rate_limited`/429, 5xx and permission/sharing errors get different messages (never blame the token for a rate limit); Gemini 429/401/403/404/5xx; timeouts; network. In `/add` and `/edit`, only `restricted_resource`/`unauthorized` send the owner to the "Insert/Update content" permission; any other Notion error uses `friendly_error` plus "check Notion before retrying". `on_error` is the app-wide error handler (one short log line instead of a traceback).

### Gemini (`services/gemini_agent.py`)

Uses the SDK's automatic function calling through `client.chats.create(...)`: `find_stories` is passed as a Python callable in `tools=`, and the SDK runs it and feeds the result back. **Token usage is logged once per question**, e.g. `usage: requests=2 prompt=3203 output=163 total=3366 model=...` (`thinking=`/`cached=` appear only when non-zero). `response.usage_metadata` is NOT used: with automatic function calling the library returns only the LAST request's usage (about half the real cost), so `_count_usage` is an httpx2 response hook, attached through `HttpOptions(client_args={"event_hooks": ...})` in `_http_options()`, that adds up the `usageMetadata` of every request into `_usage`, which `answer_question` resets before and logs after each question (also if it fails half-way). `requests=` is also how many requests of the free tier's 15-per-minute limit the question used. `python -m tests.test_usage` checks it against a fake Gemini server. The Gemini client has a 60 s timeout (`GEMINI_TIMEOUT_MS`): the library's default is no timeout, and one hung request would hold `_lock` forever and freeze every later question. Conversation memory is one chat object per Telegram `chat_id` in the `_chats` dict (in memory, lost on restart), trimmed to the last `MAX_EXCHANGES` (10) user questions; a single lock serializes calls. The Gemini free tier allows 15 requests/minute per model and each question costs 2-3 requests, so rapid-fire tests hit HTTP 429. Its `__doc__` is rebuilt in `_config()` to include the live tag list from Notion, which is how Gemini learns the valid tags, so edit that docstring (not the signature) to change what Gemini is told. `SYSTEM_PROMPT` (Vietnamese) holds the answer rules: plain text only, state the conditions used, report `total` vs `shown`, only loosen a failed search if it says which condition was dropped. `find_stories` turns `ValueError` into `{"error": ...}` so Gemini can explain a bad tag instead of crashing.

**Finding new books (random pick, default unread, "xem thêm").** Each NEW search gets a random seed and `search_stories` shuffles all matches with it, so repeated questions give different suggestions instead of always Notion's first 10. `_search_state[chat_id]` (in memory, lost on restart) stores that search's filters, seed and how many were shown; when Gemini calls `find_stories(next_page=True)` the stored filters and seed are reused (Gemini's repeated filters are ignored) and the next 10 come back, so "xem thêm" never repeats within a search. `_current_chat_id` is set in `answer_question` under the lock so the tool knows which chat is asking. `SYSTEM_PROMPT` tells Gemini to default to `reading_status="Not started"` for suggestion requests (and say so), but NOT for author/title lookups or when the user mentions reading progress. Calling it with `next_page=True` when there is no stored search (e.g. right after a restart) returns an error telling Gemini to ask the user to search again, instead of silently running a new search. The owner declined a "never repeat across different questions" memory: a new question may show books suggested earlier. `python -m tests.test_paging` checks this offline (30 checks).

### Notion search (`services/notion_search.py`)

`search_stories(tags, reading_status, status, max_chapters, author)` is the only place that talks to Notion; it is read-only and returns `{"total", "skip", "shown", "remaining", "stories"}` with at most 10 stories (it pages through all matches to count the total and to shuffle them; `seed=None` keeps Notion's order, a seed gives a repeatable shuffle, `skip` selects the batch). `notion-client` 3.x queries via `data_sources.query` with `NOTION_DATA_SOURCE_ID` (not the database ID). Table "List truyện 1" gotchas:

- The column is named `"Reading Status "` **with a trailing space**; the tag `" Cổ Đại"` has a leading space. Input is matched case-insensitively against real option names.
- `Status` (Full / In progress / Drop) is the story's own completion state ("đã hoàn" = Full); `Reading Status` (Not started / In progress / Done / Beginning + End / Drop) is the owner's reading progress ("chưa đọc" = Not started).
- `Trùng sinh` and `Trọng sinh` are near-duplicate tags; `TAG_SYNONYMS` in `services/notion_search.py` makes a search for either match both (OR inside the group, still AND with other tags; add further pairs there). Synonyms are matched against Notion's real tag names; one that was renamed or deleted is skipped with a warning in the log instead of breaking the search. `_client()` is cached (one Notion client reused), and the pagination loop also stops if Notion says `has_more` but sends no `next_cursor`.
- `max_chapters` is inclusive; multiple tags are AND-ed (owner chose strict AND; Notion's own UI "Contains" with several tags is OR, so compare by hand with one filter row per tag).
- Only 1 of ~199 stories has the `HE` tag; the owner chose to keep `HE` a strict filter and tag their stories in Notion rather than loosen it in code.
- 3 stories have no chapter count and never match a `max_chapters` search.

### Notion write (`services/notion_write.py`)

The only file that writes to Notion. Rules that must not be weakened:

- **Gemini never writes.** Writing happens only through the bot's guided `/add` and `/edit` conversations (the bot asks one column at a time; no free-text parsing by an LLM). Only a confirm button press from `ALLOWED_USER_ID` calls `add_story` / `update_story`. Do not give Gemini any write tool: the SDK runs any tool it is given immediately, so a prompt rule is not a safeguard.
- `/add` (`flows/add_flow.py`) and `/edit` (`flows/edit_flow.py`) are guided conversations, joined into ONE `ConversationHandler` by `flows/__init__.py` and registered in `bot.py` **before** the free-text Gemini handler. One handler (not two) so the bot is always in exactly one step of one flow: with two handlers, typing `/add` during `/edit` left both open and the next answer went to the wrong one. Their state numbers must not clash (`/add` uses 0-7, `/edit` 100-106), and each `start` clears the other flow's `user_data`. `/cancel` picks the right message by checking which flow's data exists. Every button step also has a stray-text handler that says "use the buttons", otherwise typed text would fall through to Gemini.
- `/add` steps: name → link → duplicate warning (add anyway / cancel) → author → chapters → tags (tap-to-toggle buttons) → status → summary with Xác nhận / Hủy. Answers are validated by calling `notion_write.build_column_update` / `parse_chapters`, so the rules live in one place.
- `/edit` steps: type part of a name or author (`find_books`, up to 10 buttons; typing again searches again) → the story's current values and a column list → new value (text, tag buttons, or a list of values; only note and date offer "clear") → "Trước/Sau" with Xác nhận / Không sửa → written as ONE column → "Sửa cột nào nữa?" until Xong. A value equal to the current one is skipped. The confirm button carries a one-time code (`edit:confirm:<nonce>`) that is used up before the write, so an old or double-pressed button can never confirm or repeat a change. `/add` has the same one-time confirm code (`add:confirm:<nonce>`), so after restarting `/add` an old summary's button is refused.
- Tests: `python -m tests.test_add_flow` (48 checks), `python -m tests.test_edit_flow` (61, including `/add`+`/edit` switching) and `python -m tests.test_write` drive the real handlers with a fake Telegram connection and a fake Notion: no real writes. None of it has been tried against real Telegram or a real Notion write yet.
- No delete, archive or trash code, ever; to drop a story set Reading Status = Drop. Notion has no separate delete permission, so this is enforced here, and `tests/test_write.py` greps for it.
- `update_story(page_id, column, value)` changes ONE column per call, only keys in `EDITABLE_COLUMNS` (all columns including Date, by the owner's choice). Only `note` and `date` may be cleared; every other column must keep a value. Note is replaced, not appended. It refuses pages outside the novel table or in the trash. `get_story` reads current values for display; `parse_chapters` / `parse_date` (accepts dd/mm/yyyy) convert typed text.
- `add_story`: name, author, chapters (at least 1), tags and link are all **required** (a missing one is rejected, and all missing items are listed together). Defaults: Status = Full, Reading Status = Not started, Record Audio = Not started; `Note` and `Date` are never sent, so they stay empty. Unknown tags are rejected; tags are written by option id (the tag `" Cổ Đại"` has a leading space).
- Every write is appended to `logs/writes.log` (git-ignored) with before/after values.
- `python -m tests.test_write` checks all of this against a fake Notion client, with no real writes. Real writes need the integration's "Update content" and "Insert content" capabilities (step 4.6) and have not been tested.

## Secrets

`.env` is git-ignored and must never be created or filled by Claude. The user copies `.env.example` to `.env` and enters their own values. Never ask them to paste real tokens or keys into chat.
