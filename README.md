# Social Scout

A Claude Code skill for **manual, on-demand research scraping** across six
surfaces — no API keys, no paid services, no credentials in this repo:

| Platform | What you get | Auth needed | Reliability |
|---|---|---|---|
| YouTube | search, video stats, transcripts, channel uploads | none | solid (yt-dlp) |
| Reddit | keyword search, subreddit feeds, comments | none | solid (Arctic Shift → Pullpush archives, with Reddit's own `.json` as a lightweight last-resort fallback) |
| Instagram | reel/post caption, like/comment/view counts, top comments, video + transcript, hashtag discovery | **your own spare account** in the scout browser | good, session-based |
| TikTok | video caption, like/comment/share/view counts, hashtags, top comments, video + transcript, trending hashtags | **your own spare account** in the scout browser (trending needs no login) | good, session-based |
| Quora | question + visible answer text on a topic | none | ⚠️ **best-effort only** |
| Any webpage | clean readable text from a single URL | none | good (Jina Reader + local fallback) |

## Design principles (read before using)

**Manual trigger only.** Nothing here schedules, polls, or runs in the
background — there is deliberately no cron, daemon, or watcher code. A scrape
happens only when a person explicitly asks Claude for one ("scrape this reel",
"what's trending on TikTok for skincare"). This keeps request volume tiny and
human-shaped, which is most of what keeps accounts safe.

**One person, one spare account, one machine.** Instagram and TikTok scraping
reuses a real logged-in Chrome session on *your* laptop — the skill never
sees, stores, or transmits passwords, and there is nothing account-related in
this repo. Accounts must be **dedicated spares** (never personal, never
shared): scraping violates these platforms' terms of service, and the built-in
throttling *reduces* ban risk but cannot eliminate it. Assume the spare
account is expendable.

**Quora and web reading are different in kind.** They're plain
fetch-and-read with no session and no robust backend. Web reading is
single-URL text extraction — *not* a crawler, not site-wide, not "search the
whole internet". Quora is web-search + page-reading and is explicitly
**best-effort**: expect partial answers and occasional failures; never expect
Reddit-level reliability from it.

# 🖥️ Claude Desktop App Setup (MCP Server)

> This is the **complete, non-technical setup guide** and the single source of
> truth for getting Social Scout working in the Claude Desktop App. If you're a
> power user who wants the terminal / Claude Code path instead, skip down to
> [Install (Claude Code / terminal)](#install-claude-code--terminal).

## What this does

Once this is set up, you can use Instagram, TikTok, YouTube, Reddit, Quora, and
any-webpage scraping **right inside the Claude Desktop App — just by talking to
Claude normally.** No terminal, no code, no technical knowledge needed after
the one-time setup below. You type "what's trending on TikTok for skincare" and
Claude does the rest.

## Prerequisites

| You need | Notes |
|---|---|
| **Python 3.11** | ⚠️ Use **3.11 specifically** — not 3.12 or 3.13 (they have compatibility issues with this tool). Windows: [python-3.11.9-amd64.exe](https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe) · Mac: [python-3.11.9-macos11.pkg](https://www.python.org/ftp/python/3.11.9/python-3.11.9-macos11.pkg) |
| **Google Chrome** | Only needed for Instagram/TikTok. |
| **Claude Desktop App** | Download from [claude.ai/download](https://claude.ai/download). |

---

## Step-by-step setup

### Step 1 — Install Python 3.11

**Windows**
1. Download the `.exe` above and double-click it.
2. ⚠️ **On the very first screen, tick the box "Add python.exe to PATH".** This
   is the most-missed step — don't skip it.
3. Click **Install Now** and wait for "Setup was successful".

**Mac**
1. Download the `.pkg` above and double-click it.
2. Click **Continue → Continue → Agree → Install**, then enter your password.

**Verify it worked** (open Command Prompt on Windows / Terminal on Mac):

```
# Windows
python --version

# Mac
python3 --version
```

You should see `Python 3.11.9`.

⚠️ **If a different version shows up (3.12, 3.13, etc.):** you have more than
one Python installed. Find the 3.11 one and note its full path for Step 4:

```
# Windows — look for the Python311 line
where python
# typical path: C:\Users\USERNAME\AppData\Local\Programs\Python\Python311\python.exe

# Mac
which python3.11
# typical path: /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
```

### Step 2 — Install Social Scout

**A) Unzip the folder** to a permanent location (not Downloads):

- Windows: `C:\Users\USERNAME\Documents\social-scout`
- Mac: `/Users/USERNAME/Documents/social-scout`

**B) Install the dependencies:**

- **Windows:** double-click **`install.bat`** inside the folder.
- **Mac:** open Terminal and run:
  ```
  cd /Users/USERNAME/Documents/social-scout
  chmod +x install.sh
  ./install.sh
  ```

⚠️ **If you get a "pip not found" / "No module named pip" error:**

```
# Windows
python -m ensurepip --upgrade

# Mac
python3.11 -m ensurepip --upgrade
```

Then run the installer again.

### Step 3 — Quick verify the server runs

From inside the social-scout folder:

```
# Windows
python mcp_server.py

# Mac
python3.11 mcp_server.py
```

✅ **Expected:** the screen goes blank and the cursor just blinks. That means
the server started correctly. Press **Ctrl+C** to stop it.

⚠️ **If you see `ModuleNotFoundError`:** the dependencies landed on the wrong
Python. Install them explicitly:

```
# Windows
python -m pip install "mcp<2" --break-system-packages
python -m pip install -r requirements.txt --break-system-packages

# Mac
python3.11 -m pip install "mcp<2" --break-system-packages
python3.11 -m pip install -r requirements.txt --break-system-packages
```

### Step 4 — Connect it to Claude Desktop

1. Open **Claude Desktop → Settings → Developer → Edit Config**.
2. ⚠️ If VS Code opens in **"Restricted Mode"**, click **"Trust this window"** at
   the top first — otherwise you can't save.
3. Add this block right after the first `{`, **before** `"preferences"`:

**Windows:**
```json
"mcpServers": {
  "social-scout": {
    "command": "C:\\Users\\USERNAME\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
    "args": ["C:\\Users\\USERNAME\\Documents\\social-scout\\mcp_server.py"]
  }
},
```

**Mac:**
```json
"mcpServers": {
  "social-scout": {
    "command": "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11",
    "args": ["/Users/USERNAME/Documents/social-scout/mcp_server.py"]
  }
},
```

⚠️ **Critical details:**
- Replace **USERNAME** with your real username (run `whoami` in CMD/Terminal if
  unsure).
- **Both** paths must be right — the `command` (your Python 3.11) **and** the
  `args` (the `mcp_server.py` in your folder).
- Windows paths use **double backslashes** `\\`.
- Keep the **trailing comma** after the closing `}`.

4. **Save** the file (`Ctrl+S` / `Cmd+S`).

### Step 5 — Fully restart Claude Desktop

⚠️ Closing the window is **not** enough — Claude keeps running in the
background and won't reload the config. You must fully quit:

**Windows**
1. Close the Claude window (X).
2. Click the **▲ up-arrow** in the system tray (bottom-right), right-click the
   **Claude** icon → **Quit**.
3. Reopen Claude Desktop.

**Mac**
1. Menu bar → **Claude → Quit Claude** (or **Cmd+Q**).
2. Reopen Claude Desktop.

### Step 6 — Confirm it's running

1. Open **Settings → Developer** in Claude Desktop.
2. **social-scout** should show a green/blue **"Running"** badge.
3. ⚠️ If it says **"Failed"** or **"Server disconnected"**, the Python path in
   your config is wrong. Re-check it with `where python` (Windows) /
   `which python3.11` (Mac), fix the config, and fully restart again.

### Step 7 — Test it

Type this into Claude Desktop:

> Run the social scout health check

**YouTube, Reddit, Web reading, and Quora should show READY.** (Instagram and
TikTok will show as needing login until you do Step 8.)

### Step 8 — Instagram / TikTok first-time login

Type into Claude Desktop:

> Search Instagram for hashtags about skincare

Here's what happens:
1. A **Chrome window opens by itself** on your screen.
2. Claude tells you to log in.
3. ⚠️ **Log into your DEDICATED SPARE Instagram account** in that Chrome window
   — **never your personal account.**
4. Go back to Claude Desktop and send the same command again.
5. Results appear! 🎉

Same flow for TikTok — log into your spare TikTok account in that same Chrome
window when asked.

⚠️ **The rules that keep you safe:**
- **Spare accounts only.** Scraping can get accounts banned — assume the spare
  is expendable. Never use your personal or business account.
- **Login is one-time.** Chrome remembers it on your machine.
- **Sessions can expire** every 2–3 months — if that happens, just log in again
  (takes ~2 minutes).

---

## Daily usage — just talk to Claude

Once set up, type things like:

- "Search Instagram for hashtags about handmade soap"
- "Get stats and top comments for this reel [url]"
- "What's trending on TikTok for skincare"
- "Transcribe this TikTok [url]"
- "Search YouTube for soap making tutorials"
- "What is Reddit saying about natural skincare"
- "What is Quora saying about soap business tips"
- "Read this webpage for me: [url]"
- "Run the social scout health check"

---

## Troubleshooting (Desktop App)

| Problem | Fix |
|---|---|
| **"Server disconnected"** in Claude Desktop | Python path in the config is wrong. Run `where python` (Win) / `which python3.11` (Mac), put the correct full path in the config, then fully quit + reopen Desktop. |
| **`ModuleNotFoundError: No module named 'anyio'`** (or `'mcp'`) | Dependencies aren't on the Python that Claude is using. Re-run `install.bat` / `install.sh`, or manually: `python -m pip install "mcp<2" -r requirements.txt --break-system-packages` (use `python3.11` on Mac). |
| **"pip not found" / "No module named pip"** | Run `python -m ensurepip --upgrade` (Win) / `python3.11 -m ensurepip --upgrade` (Mac), then retry the installer. |
| **VS Code opens in "Restricted Mode"** when editing the config | Click **"Trust this window"** at the top, then edit and save. |
| **Still shows "Failed" after editing config** | You didn't fully quit. Check the **system tray** (Windows) or use **Cmd+Q** (Mac), then reopen. |
| **Multiple Python versions installed** | Use the **full path** to Python 3.11 as the `command` in the config instead of just `"python"`. |
| **Instagram/TikTok "scout browser not running"** | The MCP normally opens Chrome automatically. If it doesn't, run `python scripts/browser.py launch` from the social-scout folder. |
| **Instagram "not logged in"** | Log into your spare account in the Chrome window that opened. One-time per machine. |
| **Quora returns nothing** | Normal — Quora is best-effort. Try again or broaden the topic. |
| **YouTube "Sign in to confirm you're not a bot"** | Run `pip install -U yt-dlp` in CMD/Terminal. |
| **"Python was not found"** | Python isn't installed or isn't on PATH. Reinstall Python 3.11 with **"Add python.exe to PATH"** ticked. |

---

## Team setup via Zoom call — checklist

For whoever is running the onboarding call, tick these off:

- [ ] Member is screen sharing
- [ ] Python 3.11 installed, version verified (`python --version` → 3.11.9)
- [ ] Folder unzipped to the correct location
- [ ] `install.bat` / `install.sh` ran successfully
- [ ] `mcp_server.py` test passed (blank blinking screen)
- [ ] Config JSON pasted with **both** paths correct
- [ ] VS Code in trusted mode (if it prompted)
- [ ] Claude Desktop **fully quit** and reopened
- [ ] Settings → Developer shows **"Running"**
- [ ] Health check returns YouTube / Reddit READY
- [ ] Instagram command → Chrome opened → spare account logged in
- [ ] Instagram command retried → results returned
- [ ] TikTok logged in (if the member needs it)

---

## How it works (quick architecture note)

The MCP server runs **locally on your own machine** — Claude Desktop launches
`mcp_server.py` as a subprocess. It's a thin wrapper around the existing Python
scripts: when you ask Claude about Instagram, Claude calls the MCP tool, which
runs the script **on your laptop** (not in the cloud). That's why a Chrome
window can open on your screen and why your logins persist between sessions.
YouTube, Reddit, Quora, and web reading don't need Chrome or any login — they
work through public sources.

---

The terminal / Claude Code path below is still fully supported for power users.

## Install (Claude Code / terminal)

1. Prerequisites: **Python 3.8+**, **Google Chrome**, then:
   ```
   pip install -r requirements.txt
   ```
2. Copy this whole folder into your Claude Code skills directory:
   - Windows: `%USERPROFILE%\.claude\skills\social-scout\`
   - macOS/Linux: `~/.claude/skills/social-scout/`
3. Verify: start a new Claude Code session and ask *"run the social scout
   health check"* (or run `python scripts/doctor.py`). YouTube, Reddit, and
   web reading should be READY immediately, no further setup.

### One-time setup for Instagram / TikTok (per machine)

Chrome (since v136) refuses automation on your normal everyday profile, so
the skill manages a separate **scout browser** profile stored locally on your
machine:

```
python scripts/browser.py launch
```

In the Chrome window that opens, log into your **dedicated spare** Instagram
and/or TikTok account — once. The session persists in that profile like any
normal browser. Re-run `python scripts/browser.py status` (or the doctor) to
confirm `LOGGED IN`. Every Instagram/TikTok command checks this first and
stops with instructions if you're not logged in — it never silently proceeds.

### Transcripts (video → text)

For Instagram/TikTok videos, the skill first tries the platform's own
captions. When a video has none, it falls back to **local Whisper**
transcription via the `faster-whisper` package — which runs entirely on your
own machine with **no API key and no account, ever**.

There is no separate setup: the same `pip install -r requirements.txt` the
whole team already runs installs it, and the speech model (default `base`,
~150 MB) **downloads itself automatically the first time a transcript is
actually needed**. Every run after that reuses the cached model offline.

Because it runs on your device, it's slower than a cloud service —
especially without a GPU — but it's completely free. On a normal laptop a
short reel transcribes in a few seconds. To trade speed for accuracy, set
`SOCIAL_SCOUT_WHISPER_MODEL` (`tiny` fastest → `base` default → `small` /
`medium` more accurate). If neither captions nor Whisper produce text, you
still get the downloaded video plus a clear "no transcript possible" note —
never a silent empty result.

### Optional extras

- `JINA_API_KEY` env var — raises the web reader's free rate limit
  (~20 pages/min without it). Not required.

## Example asks (natural language → what Claude runs)

- "Get the stats and top comments for this reel <url>" → `instagram.py post` + `comments`
- "Transcribe this TikTok" → `tiktok.py transcript <url>`
- "What hashtags are trending on TikTok for fitness?" → `tiktok.py hashtags fitness`
- "Find Instagram hashtags for the skincare niche" → `instagram.py hashtags skincare`
- "What is Quora saying about morning routines?" → `quora.py search "morning routines"`
- "Read this page for me <url>" → `webread.py read <url>`
- "Find trending YouTube videos about X" / "search Reddit for Y" → unchanged v1 commands
- "Is the scraper healthy?" → `doctor.py`

## Rate limiting & anti-ban design

Baked into every Instagram/TikTok request path (`scripts/common.py`):

- **Randomized delays** (2–6 s jitter, never fixed intervals) between
  successive requests, announced as `[throttle] waiting Ns` so you always see
  when and why the tool is pacing itself.
- **Exponential backoff** (≈10 s → 20 s → 40 s, jittered) on any rate-limit or
  error response before giving up with a specific error.
- **Conservative caps**: 30 comments per post by default (2–3 paginated
  requests — the request pattern of a human briefly scrolling), hard ceiling
  of 100 regardless of flags; per-invocation post caps likewise. Override per
  call (`--max-comments`) or per machine (`SOCIAL_SCOUT_MAX_COMMENTS`), but
  the ceilings stand.

These measures shrink the risk; they do not remove it. That is why spare
accounts are mandatory, not optional.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Scout browser is not running" | `python scripts/browser.py launch` |
| "No active Instagram/TikTok login" | log into your spare account in the scout Chrome window (one-time per machine) |
| IG/TikTok command hits a captcha / verification wall | open the scout window, complete the check by hand, browse a minute like a human, retry |
| YouTube commands failing | `pip install -U yt-dlp` (extractor breakage is almost always fixed upstream), then rerun the doctor |
| Reddit global search fails | Pullpush (the archive that searches all of Reddit) is down; the code then tries Reddit's own `.json`, but that's a lightweight fallback Reddit may block — retry with specific `--subreddit` names (which also use Arctic Shift) |
| "Reddit is blocking this network" / all Reddit sources failed | both archives were down AND Reddit's `.json` last-resort tier was rate-limited/blocked (common on datacenter/VPN IPs). Wait and retry, or run from a residential connection; the `.json` tier is intentionally not for bulk use |
| Quora returns nothing | expected occasionally — it's best-effort web search; retry later or broaden the topic |
| Web reader slow or throttled | free Jina tier is ~20 req/min; set `JINA_API_KEY`, or pass `--local` for direct fetching |
| "No transcript possible" | video has no native captions and local Whisper found no speech (music-only/silent), or `faster-whisper` isn't installed (`pip install -r requirements.txt`) |
| First transcript is slow | one-time Whisper model download; later runs reuse the cache. Set `SOCIAL_SCOUT_WHISPER_MODEL=tiny` for more speed |
| Doctor shows FAIL on something that worked yesterday | platforms change; run it twice (some backends flap), then open an issue with the doctor output |

## Repo hygiene

Nothing personal ever belongs in this repo: no cookies, tokens, downloaded
videos, or browser profiles. The scout Chrome profile lives outside the repo
(`%LOCALAPPDATA%\social-scout\` / `~/.social-scout/`), temp cookie files are
created in the OS temp dir and deleted after use, and `downloads/` is
gitignored. Keep it that way in PRs.

## Out of scope, permanently

- Scheduled/automated runs of any kind.
- Shared accounts or centralized session storage.
- Site-wide crawling or "scrape any site perfectly" claims.
