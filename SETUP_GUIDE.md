# social-scout — Claude Desktop Setup Guide

A plain-English guide for the team. No coding required.

## What this does

It lets you research YouTube, Reddit, Instagram, TikTok, Quora, and any web
page **just by asking Claude in the Claude Desktop App** — "get the stats and
top comments on this reel", "what's trending on TikTok for skincare", "what is
Reddit saying about X". Claude does the scraping for you; you never touch a
terminal.

## Prerequisites (install these first if you don't have them)

- **Python 3.10 or newer** — https://www.python.org/downloads/
  (On Windows, tick **"Add python.exe to PATH"** on the first install screen.)
- **Google Chrome** — needed only for Instagram and TikTok.
- **Claude Desktop App** — https://claude.ai/download

## Step 1 — Download and unzip

Download the `social-scout` folder (or the `.zip`) and unzip it somewhere
permanent you'll remember, e.g. your Documents folder. Don't run it from
inside the Downloads folder or a temporary location — Claude Desktop needs a
stable path to it.

## Step 2 — Run the installer

- **Windows:** double-click **`install.bat`**.
- **Mac / Linux:** open Terminal in the folder and run `bash install.sh`.

It installs the small set of Python packages the tool needs and prints
"Install complete!" when done. If it reports Python is missing, install Python
(above) and run it again.

## Step 3 — Connect it to Claude Desktop

1. Open the **Claude Desktop App**.
2. Go to **Settings → Developer → Edit Config** (this opens a file called
   `claude_desktop_config.json`).
3. Paste the block below **inside** the file. If the file already has other
   `mcpServers` entries, add `"social-scout"` alongside them rather than
   duplicating the outer braces.

   **Windows** (note the **double** backslashes, and replace the path with
   where you actually unzipped the folder):

   ```json
   {
     "mcpServers": {
       "social-scout": {
         "command": "python",
         "args": ["C:\\Users\\YOURNAME\\Documents\\social-scout\\mcp_server.py"]
       }
     }
   }
   ```

   **Mac / Linux** (use your real home path):

   ```json
   {
     "mcpServers": {
       "social-scout": {
         "command": "python3",
         "args": ["/Users/YOURNAME/Documents/social-scout/mcp_server.py"]
       }
     }
   }
   ```

   > Tip for finding the path: it's the folder that contains `mcp_server.py`.
   > The installer printed this exact path at the end — you can copy it from
   > there. On Windows, replace every single `\` with `\\` in the JSON.

4. **Save** the file and **fully quit and reopen** Claude Desktop (not just
   close the window — quit it completely so it reloads the config).

You'll know it worked when you see a tools/plug icon in Claude Desktop and
`social-scout` listed under it.

## Step 4 — First use

Just type naturally, for example:

- "Search YouTube for the best standing desks and sort by views"
- "What is Reddit saying about creatine? Check r/fitness"
- "Read this page for me: <paste a URL>"
- "Get the stats and top comments on this Instagram reel: <paste a link>"
- "What hashtags are trending on TikTok for fitness right now?"

For **Instagram and TikTok** the first time: a Chrome window will open on its
own. **Log into your dedicated spare Instagram/TikTok account** in that window
(never your personal account), then ask Claude to try again. You only do this
once per computer — the login is remembered afterwards.

To check everything is healthy, ask: **"run the social-scout health check"**.

## Troubleshooting

**"social-scout" doesn't appear in Claude Desktop.**
The config file almost always has a small JSON typo, or Claude wasn't fully
restarted. Re-check that every `\` in a Windows path is doubled (`\\`), that
commas and braces match, and then **completely quit** Claude Desktop and
reopen it.

**Instagram/TikTok says it needs me to log in every time.**
Make sure you logged into the account **in the Chrome window that opened by
itself** (the "scout browser"), not your normal everyday Chrome. That
dedicated window keeps its own login. If it still forgets, run the health
check and confirm it reports the scout browser is running.

**A tool says Python or a package is missing.**
Run the installer again (`install.bat` / `install.sh`). If it still fails,
open a terminal in the folder and run
`python -m pip install -r requirements.txt --break-system-packages`
(use `python3` on Mac), then fully restart Claude Desktop.
