# NotebookLMBot

A Discord bot that lets everyone on the server ask questions of shared
**Google NotebookLM** notebooks, add sources to them and build up notes
together, without leaving Discord.

Link a channel to a notebook, then anyone in that channel can type
`/ask what are the key points of chapter 3?` (or just `@NotebookLMBot …`).
The answer is posted in the channel with numbered citations, so the whole
group sees it and can follow up.

> [!WARNING]
> **There is no official NotebookLM API for asking questions.** Google's
> NotebookLM Enterprise API (Google Cloud, alpha) can only manage notebooks
> and sources. It has no query or chat endpoint. This bot uses
> [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py), an
> **unofficial** library that talks to NotebookLM the way the website does,
> logged in as a normal Google account. That means:
>
> - **It can break without warning** when Google changes NotebookLM. If the
>   bot suddenly stops answering, first update the library (see
>   [Updating](#updating)).
> - **Use a dedicated Google account for the bot**, not your personal one.
>   Automated use may go against Google's terms, and the bot's login cookies
>   give full access to that account.
> - **NotebookLM has usage limits** (questions per day, notebooks and sources
>   per account). They apply to the bot's account, and everyone on the server
>   shares them.
>
> Google renamed NotebookLM to "Gemini Notebook" in July 2026. The library and
> this README still say NotebookLM.

## How collaboration works

| Idea | What it means |
|---|---|
| **A channel is linked to one notebook** | `/notebook link` in `#biology` makes every question there go to the Biology notebook. Different channels can use different notebooks. |
| **Threads follow their channel** | A thread uses its parent channel's notebook, unless a manager links the thread to a different one. |
| **Each channel and thread is its own conversation** | Follow-up questions remember what was asked before *in that channel or thread*. To explore something on the side without mixing it into the main conversation, open a thread. `/reset` starts a new conversation. |
| **Answers are public** | Everyone sees who asked what and what the answer was. The **📚 Sources** button shows the cited passages, and only the person who clicks it sees them. |
| **Notes are shared** | **📝 Save as note** saves an answer to the notebook's notes, where everyone can see it in NotebookLM. |
| **Humans can use NotebookLM too** | Notebooks belong to (or are shared with) the bot's Google account. Use `/notebook share` to give people access, so they can also open the same notebook in their browser. |
| **Managers control content** | Anyone can ask questions. Only people with **Manage Server** or the role set as `MANAGER_ROLE_ID` can link notebooks and add or remove sources. |

## Commands

### Asking

| Command | Who | What it does |
|---|---|---|
| `/ask <question> [new_conversation]` | anyone | Ask the linked notebook. Set `new_conversation` to True to ignore earlier questions. |
| `@NotebookLMBot <question>` | anyone | Same as `/ask` |
| `/reset` | anyone | Start a new conversation in this channel or thread |

Each person has to wait a few seconds between questions (`ASK_COOLDOWN`).
NotebookLM usually takes 5–30 seconds to answer.

### Notebooks

| Command | Who | What it does |
|---|---|---|
| `/notebook list` | anyone | Notebooks the bot's Google account can see |
| `/notebook info` | anyone | Which notebook this channel uses, how many sources it has, and a link to open it |
| `/notebook link <notebook>` | manager | Link this channel (or thread) to a notebook. Names autocomplete. |
| `/notebook create <title>` | manager | Create a new, empty notebook and link it here |
| `/notebook unlink` | manager | Remove the link. The notebook itself isn't touched. |
| `/notebook share <email> [editor/viewer]` | manager | Give someone access to the notebook in NotebookLM |

### Sources

| Command | Who | What it does |
|---|---|---|
| `/source list` | anyone | Show the notebook's sources |
| `/source add-url <url>` | manager | Add a web page or YouTube video |
| `/source add-file <file>` | manager | Add an attached PDF, document, text or audio file (up to 100 MB) |
| `/source add-text <title>` | manager | Opens a form to paste in up to 4000 characters of text |
| `/source remove <source>` | manager | Remove a source. Names autocomplete. |

Adding a source waits until NotebookLM has finished processing it, which can
take a minute for long PDFs or videos.

### Studio

Make and share the things in NotebookLM's Studio panel (Audio and Video
Overviews, slide decks, infographics, reports, quizzes, flashcards, mind maps,
data tables, and files NotebookLM's chat saved there) as files in Discord.

| Command | Who | What it does |
|---|---|---|
| `/studio list` | anyone | Show the notebook's studio items (only you see the list). Pick one from the dropdown to post it in the channel. |
| `/studio create <kind> [instructions]` | manager | Have NotebookLM make a new item from all the notebook's sources, then post it in the channel when it's ready. `instructions` can say what to focus on, who it's for, or the tone. |
| `/studio download <item> [format] [private]` | anyone | Post a studio item as a file. Names autocomplete. `format`: `pdf`/`pptx` for slide decks, `markdown`/`html`/`json` for quizzes and flashcards. Set `private` to True to get the file only for yourself. |

| Item | File |
|---|---|
| Audio Overview | `.m4a` |
| Video Overview | `.mp4` |
| Slide deck | `.pdf` (or `.pptx`) |
| Infographic | `.png` |
| Report | `.md` |
| Quiz, Flashcards | `.md` (or `.html`, `.json`) |
| Mind map | `.json` |
| Data table | `.csv` |
| File (saved to Studio by NotebookLM's chat, e.g. a summary) | as saved (e.g. `.md`) |

Discord limits uploads to 10 MB on most servers (more with Nitro boosts).
Video Overviews and long Audio Overviews are often bigger than that. In that
case the bot posts a link to the notebook instead. Each person has to wait 15
seconds between downloads.

About `/studio create`:

- Generating takes from under a minute (mind maps) to 30 minutes (Video
  Overviews). You can keep using the bot meanwhile.
- If the item is ready within about 12 minutes, the bot posts the file as a
  reply to the command. Otherwise it posts "your item is ready" and pings
  whoever asked, with a **📥 Post file** button. (Discord only lets a bot
  reply to a slash command for 15 minutes. The button avoids needing the
  **Attach Files** permission.)
- If the bot restarts while something is generating, it stops waiting, but
  NotebookLM keeps going. The item shows up in `/studio list` when done.
- Google limits how many items an account can make per day, especially Audio
  and Video Overviews. When the bot's account hits a limit, the bot says so.
  That's why only managers can create items.

## Setup

These steps are for Ubuntu/Debian. You only need to do them once.

### 1. Create the bot on Discord

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open **Bot** in the sidebar, click **Reset Token** and copy the token. You
   need it in step 5. Keep it secret: anyone with it can control the bot.
   You do **not** need the Message Content intent: Discord always lets bots
   read messages that mention them.
3. Open **OAuth2 → URL Generator**:
   - Under **Scopes**, tick `bot` **and** `applications.commands`.
   - Under **Bot Permissions**, tick **View Channels**, **Send Messages**,
     **Send Messages in Threads**, **Embed Links**, **Read Message History**
     and **Add Reactions**.
   - Open the URL at the bottom and add the bot to the server.
4. Copy your server ID: in Discord, open Settings → Advanced and turn on
   **Developer Mode**. Then right-click the server icon and choose
   **Copy Server ID**. To limit managing to a role, right-click the role in
   Server Settings → Roles and choose **Copy Role ID**.

### 2. Make a Google account for the bot

Create a new Google account (e.g. `ourserver.notebookbot@gmail.com`). Open
<https://notebooklm.google.com> with it once and accept the terms.

To use notebooks you already have, open each one in NotebookLM, click
**Share** and add the bot's email as an **Editor**. They will then show up in
`/notebook list`.

### 3. Install the requirements

```bash
sudo apt install python3-venv
cd ~/Documents/Code/NotebookLMBot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If `python3-venv` doesn't work, install the package that matches your Python
version, e.g. `python3.14-venv` (check yours with `python3 --version`). You
need Python 3.10 or newer.

### 4. Log the bot in to NotebookLM

```bash
.venv/bin/notebooklm login
```

A browser window opens (the first time, it downloads Chromium, about 170 MB).
Sign in with **the bot's Google account**. The window closes by itself once
you're logged in. Check that it worked:

```bash
.venv/bin/notebooklm list
```

The login is saved in `~/.notebooklm/`. Other ways to log in:

| Situation | Command |
|---|---|
| The bundled Chromium crashes | `.venv/bin/notebooklm login --browser chrome` |
| You're already signed in to the bot account in Chrome or Firefox | `.venv/bin/notebooklm login --browser-cookies chrome` |
| A server with no screen (VPS, Raspberry Pi) | `.venv/bin/notebooklm login --master-token --account <bot email>`, or log in on your own computer and copy `~/.notebooklm/` over |
| Several Google accounts | `notebooklm profile …`, then set `NOTEBOOKLM_PROFILE` in `.env` |

If `notebooklm login` complains that Playwright's browser is missing, run
`.venv/bin/playwright install chromium`.

### 5. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Setting | What to put there |
|---|---|
| `DISCORD_TOKEN` | The bot token from step 1 (required) |
| `GUILD_ID` | Your server ID. Recommended: without it, commands take up to an hour to show up. |
| `MANAGER_ROLE_ID` | Optional: the role allowed to link notebooks and change sources. People with Manage Server can always do this. |
| `NOTEBOOKLM_PROFILE` | Optional: only needed if you made extra `notebooklm` profiles |
| `ASK_COOLDOWN` | Seconds between questions per person (default 10) |
| `DATA_DIR` | Where channel links are saved (default `data/`) |

`.env` is ignored by git, so your keys won't be committed.

## Running the bot

```bash
cd ~/Documents/Code/NotebookLMBot
.venv/bin/python bot.py
```

When it's working you'll see `Connected to NotebookLM as …`,
`Synced … commands` and `Logged in as …`. Ctrl+C stops it.

Then in Discord, try it out:

1. `/notebook list`, to check that the bot can see your notebooks
2. `/notebook link`, in the channel you want to use
3. `/ask`, then a follow-up `/ask` that refers back to the first answer
4. Click **📚 Sources** under the answer

### Keeping it running in the background (optional)

Create `~/.config/systemd/user/notebookbot.service`:

```ini
[Unit]
Description=Discord NotebookLM bot
After=network-online.target

[Service]
WorkingDirectory=%h/Documents/Code/NotebookLMBot
ExecStart=%h/Documents/Code/NotebookLMBot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now notebookbot
loginctl enable-linger "$USER"   # keep it running while you're logged out
```

| Task | Command |
|---|---|
| See the log | `journalctl --user -u notebookbot -f` |
| Restart after changes | `systemctl --user restart notebookbot` |
| Stop | `systemctl --user stop notebookbot` |

### Updating

`notebooklm-py` gets frequent fixes when Google changes NotebookLM:

```bash
.venv/bin/pip install -U -r requirements.txt
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `Couldn't log in to NotebookLM` / `Storage file not found` at startup | Run `.venv/bin/notebooklm login` (step 4). |
| "The bot's Google login has expired" | Google cookies expire every few weeks. Run `.venv/bin/notebooklm login` again and restart the bot. Use `--master-token` for a longer-lasting login. |
| "NotebookLM is rate-limiting the bot" | The bot's account hit a limit. Wait, or raise `ASK_COOLDOWN`. |
| A notebook is missing from `/notebook list` | Share it with the bot's Google account (step 2). |
| "That notebook doesn't exist any more…" | It was deleted or unshared. Re-run `/notebook link`. |
| The bot worked yesterday and now every question fails | Google probably changed something. Update (see [Updating](#updating)) and check the [notebooklm-py issues](https://github.com/teng-lin/notebooklm-py/issues). |
| Slash commands don't show up | Set `GUILD_ID`, invite the bot with the `applications.commands` scope, then restart Discord (Ctrl+R). |
| `@mention` does nothing | The channel needs a link, and the bot needs permission to see and send messages there. |

`.venv/bin/notebooklm doctor` checks the NotebookLM login on its own,
without the bot.

## How it's built

```
NotebookLMBot/
├── bot.py               # Entry point: logs in to NotebookLM, loads the cog, syncs slash commands
├── requirements.txt     # discord.py, notebooklm-py[browser], python-dotenv
├── .env.example         # Copy to .env
└── notebookbot/
    ├── config.py        # Reads .env
    ├── nlm.py           # The ONLY file that talks to NotebookLM (wraps notebooklm-py) + friendly error messages
    ├── store.py         # Saves channel → notebook links and conversation IDs to data/links.json
    ├── cog.py           # Slash commands, @mention handler, permission checks
    └── ui.py            # Answer embeds, Sources / Save as note buttons, the add-text form, studio dropdown
```

- **One NotebookLM client** is opened at startup and shared by every command.
- **Questions in the same channel or thread run one at a time**, so
  follow-ups stay in order. Different channels run in parallel.
- **Conversation IDs** are saved per channel or thread together with the
  notebook ID, so they survive restarts and reset automatically when a
  channel is linked to a different notebook. If a saved conversation was
  deleted in NotebookLM, the bot starts a new one by itself.
- **Long answers** are split over several messages, because Discord caps an
  embed at 4096 characters.
- The buttons under an answer work for an hour (and stop working if the bot
  restarts). After that the answer stays, but the buttons are removed.

### Where things are stored

| What | Where | Secret? |
|---|---|---|
| Discord token and settings | `.env` | **Yes** |
| Google login cookies | `~/.notebooklm/` | **Yes**: full access to the bot's Google account |
| Channel links and conversation IDs | `data/links.json` | No |

Never commit or share `.env` or `~/.notebooklm/`. Both are outside git
(`.env` and `data/` are in `.gitignore`).

### Switching to an official API later

All NotebookLM calls go through `NotebookService` in `notebookbot/nlm.py`. If
Google adds a query endpoint to the NotebookLM Enterprise API (or the Gemini
API gains notebook access), only that class has to be rewritten. The commands,
storage and UI stay the same.

## Links

- notebooklm-py: <https://github.com/teng-lin/notebooklm-py>
- NotebookLM Enterprise API (official, no chat endpoint yet):
  <https://cloud.google.com/agentspace/notebooklm-enterprise/docs/api-notebooks>
- discord.py docs: <https://discordpy.readthedocs.io/>
