# Telegram bot for searching novels in Notion

The bot takes natural-language questions on Telegram, uses Gemini to understand the question (it picks the filters),
searches the Notion novel table, and then the program itself prints the story list (Gemini is asked only
once per question to save tokens). Besides searching, the bot has the commands `/add` (add a new story) and
`/edit` (edit a story) that write to Notion, and it only writes after you press **Confirm**. The bot never
deletes stories. `/ping` replies "pong".
The bot only responds to one user (the ID in `TELEGRAM_USER_ID`).

## First-time setup

You need Python 3.11 or newer (the project runs on Python 3.13). Check with `python3 --version`.

1. Create a virtual environment (venv) and install the libraries, inside the project folder:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create the `.env` file from the example file and make it readable only by you:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

3. Open `.env` and fill in your own values:
   - `BOT_TOKEN`: the token from @BotFather on Telegram.
   - `TELEGRAM_USER_ID`: your numeric ID (get it from the @userinfobot bot).
   - `NOTION_TOKEN`: the token of your Notion integration (notion.so/profile/integrations). Remember to share the novel table with the integration.
   - `NOTION_DATA_SOURCE_ID`: the data source ID of the main novel table (list 1). `/edit` only works on this list.
   - `NOTION_EXTRA_DATA_SOURCE_IDS` (optional): the data source IDs of your other lists (list 2, list 3), separated by commas, for example `id2,id3`. The search and the `/add` duplicate check read them too. Share these tables with the integration as well. Leave empty to search list 1 only.
   - `NOTION_ADD_DATA_SOURCE_ID` (optional): the data source ID of the list where `/add` puts new stories (for example list 3). Leave empty to add to list 1. `/edit` only works on list 1.
   - `GEMINI_API_KEY`: the API key from Google AI Studio (aistudio.google.com).
   - `GEMINI_MODEL` (optional): the Gemini model name. Leave empty to use the default `gemini-3.6-flash`.
   - `LOG_LEVEL` (optional): how detailed the log is (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Leave empty to use `INFO`.

   Do not share the `.env` file with anyone and do not push it to GitHub.

## Activate the virtual environment (venv)

After the first setup, each time you open a new terminal you only need to run this command in the project folder:

```bash
source .venv/bin/activate
```

When it works, the start of the command line shows `(.venv)`.
To leave the venv: `deactivate`.

## Run the bot

```bash
python bot.py
```

Stop the bot with `Ctrl+C`. The log is written to the file `logs/bot.log` (not pushed to GitHub), and each line looks like
`time LEVEL [question id] message`. The detail level is set with `LOG_LEVEL` in `.env`
(`INFO` by default, `DEBUG` for more detail). Tokens and keys are always hidden as `***`.
For each question the log records, in order: the incoming question, the time of the Gemini call, the filters Gemini chose,
the Notion result and time, the token usage, "Reply sent" and the total time. Example token line:
`usage: model=... prompt=871 output=33 function_calls=1 model_requests=1 total=904`
(`model_requests` is the number of Gemini calls, useful for tracking the free limit of 15 calls per minute).
See today's summary with `python -m logtools.summary` (add a date like `2026-11-09` for another day). To preview the log lines without a network, run `python -m tests.demo_log`.

The bot remembers the last 5 questions in each chat (lost when the bot stops, and it remembers only the conditions that were searched, not story names),
so you can ask follow-ups such as "only completed stories".

## Add a new story: /add

Type `/add` in Telegram and the bot asks, one by one: name, link, author, number of chapters (1 or more),
tags (tap buttons to choose) and status. At the end the bot shows a summary, and when you press **Confirm**
the new story is added to Notion. Type `/cancel` at any time to stop (nothing has been written yet).

Defaults: Status = Full, Reading Status = Not started, Record Audio = Not started,
Note and Date are left empty. If the story is already in any of your lists (same name or link), the bot warns you.

The new story goes into the list set by `NOTION_ADD_DATA_SOURCE_ID` in `.env` (for example list 3). The tag buttons show the tags of that list, and the bot tells you the automatic ID Notion gave the story (for example `L3-90`). `/edit` still only edits list 1.

Note: to add stories, the Notion integration must have the **Insert content** capability turned on.

## Edit a story: /edit

Type `/edit`, enter part of a story name or an author name, then pick the story from the list.
The bot shows the current values and asks which column you want to edit (name, author, chapters, tags, link,
Status, Reading Status, Record Audio, note, date). You enter the new value, the bot shows
"Before / After" and only writes to Notion when you press **Confirm**. After each edit, the bot asks
"Which column next?" until you press **Done**. Only the note and the date can be left empty.
Type `/cancel` to stop: changes you already confirmed are kept.

The bot never deletes stories. To drop a story, change its Reading Status to Drop.

Note: to edit stories, the Notion integration must have the **Update content** capability turned on.
