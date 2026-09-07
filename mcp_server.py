#!/usr/bin/env python3
"""MCP server for the social-scout skill — lets Claude Desktop use it with no
terminal.

This is a thin WRAPPER over the existing scripts in scripts/. It does not
modify or import them into its own process; it runs each one as a subprocess
(the same way you'd run it by hand) with the working directory set to
scripts/, captures stdout/stderr, and returns the result to Claude Desktop
over stdio.

Why subprocess instead of import: the scripts print JSON to their own stdout.
This server's stdout is the MCP protocol channel, so anything the scripts
printed in-process would corrupt it. Running them as children keeps their
output on a separate pipe that we capture cleanly.

For the login-walled platforms (Instagram, TikTok) the server transparently
makes sure the "scout browser" is running and, where needed, logged in — via
`browser.py status` / `browser.py launch` — before running the real command,
so an end user never touches a terminal.

Launch check:  python mcp_server.py   (starts and waits for stdio input)
"""

import os
import re
import sys

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, "scripts")
# Required so the scripts' peer imports (import browser, import common, ...)
# resolve if anything in this process ever needs them; the scripts themselves
# also get scripts/ on sys.path[0] automatically when run as subprocesses.
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

PYTHON = sys.executable  # the same interpreter Claude Desktop launched us with

# Generous per-call ceilings. Transcripts download a video and may run local
# Whisper, so they get the most room; everything else is quick.
TIMEOUT_DEFAULT = 240
TIMEOUT_TRANSCRIPT = 1200
TIMEOUT_HEALTH = 360
TIMEOUT_BROWSER_LAUNCH = 90

INFO_PREFIXES = ("[throttle]", "[social-scout]")


# --------------------------------------------------------------------------
# subprocess plumbing
# --------------------------------------------------------------------------
def _run(argv, timeout):
    """Run a child process in scripts/, capturing output. Blocking; callers
    invoke it from a worker thread so the event loop stays responsive.

    stdin is DEVNULL on purpose: this server's own stdin is the MCP JSON-RPC
    pipe. If a child inherited it, it would sit holding that pipe (which never
    sends EOF) and never exit — so give every child a closed stdin instead."""
    import subprocess
    return subprocess.run(
        [PYTHON] + argv,
        cwd=SCRIPTS_DIR,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=os.environ.copy(),
    )


def _split_stderr(stderr):
    """Separate informational throttle/status lines (which the scripts print
    to stderr as normal operation) from genuine error output."""
    info, errs = [], []
    for line in (stderr or "").splitlines():
        (info if line.strip().startswith(INFO_PREFIXES) else errs).append(line)
    return "\n".join(info).strip(), "\n".join(errs).strip()


def run_script(argv, timeout=TIMEOUT_DEFAULT):
    """Run a wrapped script and format the result as text for Claude Desktop.
    Returns (text, is_error)."""
    try:
        proc = _run(argv, timeout)
    except Exception as e:  # timeout, spawn failure, etc.
        return f"ERROR: could not run {' '.join(argv)} — {type(e).__name__}: {e}", True

    info, errs = _split_stderr(proc.stderr)
    if proc.returncode == 0:
        out = proc.stdout.strip() or "(the command produced no output)"
        if info:
            out += f"\n\n---\n[status messages]\n{info}"
        return out, False

    # Non-zero exit: surface stderr as the error, keeping any throttle context.
    detail = errs or proc.stdout.strip() or "the command failed with no message"
    if info:
        detail += f"\n[status messages]\n{info}"
    return f"ERROR (exit {proc.returncode}): {detail}", True


# --------------------------------------------------------------------------
# scout browser gating (Instagram / TikTok)
# --------------------------------------------------------------------------
def _browser_status_text():
    """`browser.py status` output. Exits 1 when not running — that's fine, we
    only read the text."""
    try:
        proc = _run(["browser.py", "status"], timeout=30)
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        return f"__STATUS_ERROR__ {e}"


def _is_running(status_text):
    return "scout browser : RUNNING" in status_text


def _is_logged_in(status_text, platform):
    label = "Instagram" if platform == "instagram" else "TikTok"
    m = re.search(rf"{label}\s*:\s*(NOT LOGGED IN|LOGGED IN)", status_text)
    return bool(m) and m.group(1) == "LOGGED IN"


def ensure_browser(platform, require_login):
    """Make sure the scout browser is up (launching it if needed) and, when
    require_login is set, that `platform` has a live session. Returns None to
    proceed, or a user-facing message string to return instead of running."""
    status = _browser_status_text()
    if status.startswith("__STATUS_ERROR__"):
        return (f"Could not check the scout browser: {status}. Make sure "
                "Python and Google Chrome are installed.")

    if not _is_running(status):
        launched, is_err = run_script(["browser.py", "launch"], TIMEOUT_BROWSER_LAUNCH)
        if is_err:
            return ("Could not start the scout browser (the Chrome profile it "
                    f"uses for {platform}).\n{launched}")
        status = _browser_status_text()
        if not _is_running(status):
            return ("The scout browser did not come up. Close any leftover "
                    "scout Chrome windows and try again.")

    if require_login and not _is_logged_in(status, platform):
        nice = "Instagram" if platform == "instagram" else "TikTok"
        return (f"A Chrome window has opened on your screen. Please log into "
                f"your dedicated spare {nice} account in that window, then ask "
                f"me to try again. This is a one-time step — your login will "
                f"persist for future sessions.")
    return None


# --------------------------------------------------------------------------
# tool definitions
# --------------------------------------------------------------------------
def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


# Each entry: name -> (description, inputSchema, builder, browser_gate, timeout)
# builder(args) returns the argv AFTER the python executable, i.e.
# ["<script>.py", "<subcommand>", ...].
# browser_gate is None, or ("instagram"/"tiktok", require_login: bool).
def _b(*a):  # tiny helper to keep the table readable
    return a


TOOLS = {}


def tool(name, description, schema, builder, gate=None, timeout=TIMEOUT_DEFAULT):
    TOOLS[name] = {
        "description": description,
        "schema": schema,
        "builder": builder,
        "gate": gate,
        "timeout": timeout,
    }


# ---- YouTube (no login) ----
tool(
    "youtube_search",
    "Search YouTube for videos matching a query. Set sort_by_views=true for "
    "trending / most-viewed results on the topic. No login required.",
    _schema({"query": STR, "limit": {**INT, "default": 10},
             "sort_by_views": {**BOOL, "default": False}}, ["query"]),
    lambda a: ["youtube.py", "search", a["query"],
               "--limit", str(a.get("limit", 10))]
              + (["--sort-views"] if a.get("sort_by_views") else []),
)
tool(
    "youtube_video",
    "Get full metadata for one YouTube video: title, channel, view/like/"
    "comment counts, upload date, duration, description, tags. No login.",
    _schema({"url": STR}, ["url"]),
    lambda a: ["youtube.py", "video", a["url"]],
)
tool(
    "youtube_transcript",
    "Get the plain-text transcript/captions of a YouTube video. timestamps=true "
    "prefixes each line with [MM:SS]. Prefers human captions, falls back to "
    "auto-generated. No login.",
    _schema({"url": STR, "lang": {**STR, "default": "en"},
             "timestamps": {**BOOL, "default": True}}, ["url"]),
    lambda a: ["youtube.py", "transcript", a["url"], "--lang", a.get("lang", "en")]
              + (["--timestamps"] if a.get("timestamps", True) else []),
    timeout=TIMEOUT_TRANSCRIPT,
)
tool(
    "youtube_channel_videos",
    "List recent uploads from a YouTube channel or playlist URL. No login.",
    _schema({"url": STR, "limit": {**INT, "default": 15}}, ["url"]),
    lambda a: ["youtube.py", "videos", a["url"], "--limit", str(a.get("limit", 15))],
)

# ---- Instagram (scout browser + spare-account login) ----
tool(
    "instagram_post",
    "Get stats for one Instagram post or reel: caption, like/comment/view "
    "counts, author, date, duration. Uses your logged-in scout browser.",
    _schema({"url": STR}, ["url"]),
    lambda a: ["instagram.py", "post", a["url"]],
    gate=("instagram", True),
)
tool(
    "instagram_comments",
    "Get top comments on an Instagram post/reel (author, text, likes, date). "
    "max_comments default 30, capped at 100. Uses your logged-in scout browser.",
    _schema({"url": STR, "max_comments": {**INT, "default": 30}}, ["url"]),
    lambda a: ["instagram.py", "comments", a["url"],
               "--max-comments", str(a.get("max_comments", 30))],
    gate=("instagram", True),
)
tool(
    "instagram_transcript",
    "Download an Instagram reel and transcribe it (native captions, else local "
    "Whisper). Uses your logged-in scout browser.",
    _schema({"url": STR, "lang": {**STR, "default": "en"}}, ["url"]),
    lambda a: ["instagram.py", "transcript", a["url"], "--lang", a.get("lang", "en")],
    gate=("instagram", True),
    timeout=TIMEOUT_TRANSCRIPT,
)
tool(
    "instagram_hashtags",
    "Discover Instagram hashtags for a niche. Returns hashtags ranked by "
    "Instagram's own search with post counts. Uses your logged-in scout browser.",
    _schema({"niche": STR}, ["niche"]),
    lambda a: ["instagram.py", "hashtags", a["niche"]],
    gate=("instagram", True),
)

# ---- TikTok (scout browser; login for most, trending is public) ----
tool(
    "tiktok_video",
    "Get stats for one TikTok video: caption, like/comment/share/view counts, "
    "duration, hashtags, author, date, music. Uses your logged-in scout browser.",
    _schema({"url": STR}, ["url"]),
    lambda a: ["tiktok.py", "video", a["url"]],
    gate=("tiktok", True),
)
tool(
    "tiktok_comments",
    "Get top comments on a TikTok video (author, text, likes, date). "
    "max_comments default 30, capped at 100. Uses your logged-in scout browser.",
    _schema({"url": STR, "max_comments": {**INT, "default": 30}}, ["url"]),
    lambda a: ["tiktok.py", "comments", a["url"],
               "--max-comments", str(a.get("max_comments", 30))],
    gate=("tiktok", True),
)
tool(
    "tiktok_transcript",
    "Download a TikTok video and transcribe it (native captions, else local "
    "Whisper). Uses your logged-in scout browser.",
    _schema({"url": STR, "lang": {**STR, "default": "en"}}, ["url"]),
    lambda a: ["tiktok.py", "transcript", a["url"], "--lang", a.get("lang", "en")],
    gate=("tiktok", True),
    timeout=TIMEOUT_TRANSCRIPT,
)
tool(
    "tiktok_hashtags",
    "Get currently trending TikTok hashtags from the public Creative Center, "
    "optionally filtered by a niche keyword. days can be 7, 30, or 120. Opens "
    "the scout browser but needs no login.",
    _schema({"niche": STR, "days": {**INT, "enum": [7, 30, 120], "default": 7}}),
    lambda a: ["tiktok.py", "hashtags"] + ([a["niche"]] if a.get("niche") else [])
              + ["--days", str(a.get("days", 7))],
    gate=("tiktok", False),
)

# ---- Reddit (no login; archive chain -> Reddit .json fallback) ----
def _reddit_common(a):
    extra = []
    if a.get("after"):
        extra += ["--after", a["after"]]
    if a.get("before"):
        extra += ["--before", a["before"]]
    if a.get("min_score") is not None:
        extra += ["--min-score", str(a["min_score"])]
    return extra


tool(
    "reddit_search",
    "Search Reddit for posts matching a query. subreddit optional "
    "(comma-separated to scope the search). Dates as YYYY-MM-DD or 'Nd' (days "
    "ago). No login.",
    _schema({"query": STR, "subreddit": STR, "limit": {**INT, "default": 25},
             "after": STR, "before": STR, "min_score": INT}, ["query"]),
    lambda a: ["reddit.py", "search", a["query"], "--limit", str(a.get("limit", 25))]
              + (["--subreddit", a["subreddit"]] if a.get("subreddit") else [])
              + _reddit_common(a),
)
tool(
    "reddit_subreddit",
    "Get the newest posts from one subreddit (no keyword). Dates as YYYY-MM-DD "
    "or 'Nd'. No login.",
    _schema({"name": STR, "limit": {**INT, "default": 25},
             "after": STR, "before": STR, "min_score": INT}, ["name"]),
    lambda a: ["reddit.py", "subreddit", a["name"], "--limit", str(a.get("limit", 25))]
              + _reddit_common(a),
)
tool(
    "reddit_comments",
    "Get Reddit comments — either for a specific post (pass post: an id or "
    "URL) or by keyword (pass query, optionally scoped with subreddit). No login.",
    _schema({"post": STR, "query": STR, "subreddit": STR,
             "limit": {**INT, "default": 25}}),
    lambda a: ["reddit.py", "comments", "--limit", str(a.get("limit", 25))]
              + (["--post", a["post"]] if a.get("post") else [])
              + (["--query", a["query"]] if a.get("query") else [])
              + (["--subreddit", a["subreddit"]] if a.get("subreddit") else []),
)

# ---- Quora (best-effort, no login) ----
tool(
    "quora_search",
    "Research a topic on Quora (BEST-EFFORT): finds Quora question pages via "
    "web search and returns their visible question + answer text. Weaker and "
    "less reliable than the other platforms. No login.",
    _schema({"topic": STR, "limit": {**INT, "default": 3}}, ["topic"]),
    lambda a: ["quora.py", "search", a["topic"], "--limit", str(a.get("limit", 3))],
)

# ---- Web reading (no login) ----
tool(
    "web_read",
    "Read a single web page URL and return its clean, readable text (nav/ads "
    "stripped). Single-URL only — not a crawler. No login.",
    _schema({"url": STR, "max_chars": {**INT, "default": 20000}}, ["url"]),
    lambda a: ["webread.py", "read", a["url"], "--max-chars", str(a.get("max_chars", 20000))],
)

# ---- Amazon (product public; reviews need an Amazon login in scout browser) ----
# Both gate on the scout browser being RUNNING (Amazon bot-blocks scripted
# requests; a real Chrome gets through). No login is enforced by the gate —
# amazon.py itself detects Amazon's review sign-in wall and returns a clear
# "log into Amazon in the scout browser" message when needed.
tool(
    "amazon_reviews",
    "Pull Amazon product reviews. Filter by star rating (1-5 or all), sort by "
    "most helpful or most recent. Use stars=1 for pain points / complaints, "
    "stars=5 for what customers love. Needs an Amazon account logged into the "
    "scout browser (a spare is safest) — it'll tell you if you're not.",
    _schema({"url": STR,
             "stars": {**STR, "enum": ["1", "2", "3", "4", "5", "all"],
                       "default": "all"},
             "sort": {**STR, "enum": ["helpful", "recent"], "default": "helpful"},
             "pages": {**INT, "default": 3},
             "max_reviews": {**INT, "default": 30}}, ["url"]),
    lambda a: ["amazon.py", "reviews", a["url"],
               "--stars", str(a.get("stars", "all")),
               "--sort", str(a.get("sort", "helpful")),
               "--pages", str(a.get("pages", 3)),
               "--max-reviews", str(a.get("max_reviews", 30))],
    gate=("amazon", False),
    timeout=600,
)
tool(
    "amazon_product",
    "Get Amazon product metadata — title, price, overall rating, rating "
    "breakdown, and feature bullet points. Quick competitive intelligence "
    "without pulling full reviews. No login needed.",
    _schema({"url": STR}, ["url"]),
    lambda a: ["amazon.py", "product", a["url"]],
    gate=("amazon", False),
)

# ---- Meta Ad Library (public; needs the scout browser running, no login) ----
tool(
    "meta_ads_search",
    "Search Meta Ad Library for active Facebook/Instagram ads by keyword. Shows "
    "ad text, page name, how long the ad has been running, and media type. "
    "Longer-running ads are likely winners. Public — no Facebook login needed.",
    _schema({"query": STR, "country": {**STR, "default": "US"},
             "max_ads": {**INT, "default": 20}}, ["query"]),
    lambda a: ["meta_ads.py", "search", a["query"],
               "--country", str(a.get("country", "US")),
               "--max-ads", str(a.get("max_ads", 20))],
    gate=("meta", False),
    timeout=300,
)
tool(
    "meta_ads_page",
    "Get active ads from a specific Facebook/Instagram advertiser (pass a page "
    "name, a facebook.com URL, or a numeric page id). Use to analyze a "
    "competitor's current ad strategy. Public — no Facebook login needed.",
    _schema({"page": STR, "country": {**STR, "default": "US"},
             "max_ads": {**INT, "default": 20}}, ["page"]),
    lambda a: ["meta_ads.py", "page", a["page"],
               "--country", str(a.get("country", "US")),
               "--max-ads", str(a.get("max_ads", 20))],
    gate=("meta", False),
    timeout=300,
)

# ---- Google Trends (pytrends, direct HTTP — no browser gate) ----
tool(
    "google_trends_interest",
    "Get Google Trends interest over time for a keyword — weekly search "
    "interest scores (0-100, relative not absolute volume). No login.",
    _schema({"keyword": STR, "timeframe": {**STR, "default": "today 3-m"},
             "geo": {**STR, "default": ""}}, ["keyword"]),
    lambda a: ["google_trends.py", "interest", a["keyword"],
               "--timeframe", str(a.get("timeframe", "today 3-m")),
               "--geo", str(a.get("geo", ""))],
)
tool(
    "google_trends_compare",
    "Compare Google Trends interest for up to 5 keywords (comma-separated) side "
    "by side — shows which terms are trending higher. No login.",
    _schema({"keywords": STR, "timeframe": {**STR, "default": "today 3-m"},
             "geo": {**STR, "default": ""}}, ["keywords"]),
    lambda a: ["google_trends.py", "compare", a["keywords"],
               "--timeframe", str(a.get("timeframe", "today 3-m")),
               "--geo", str(a.get("geo", ""))],
)
tool(
    "google_trends_related",
    "Find related and rising search queries for a keyword on Google Trends. "
    "Rising queries surface emerging trends. No login.",
    _schema({"keyword": STR, "geo": {**STR, "default": ""}}, ["keyword"]),
    lambda a: ["google_trends.py", "related", a["keyword"],
               "--geo", str(a.get("geo", ""))],
)
tool(
    "google_trends_by_region",
    "See where a keyword is most searched geographically (COUNTRY / REGION / "
    "CITY). Useful for targeting decisions. No login.",
    _schema({"keyword": STR,
             "resolution": {**STR, "enum": ["COUNTRY", "REGION", "CITY"],
                            "default": "COUNTRY"},
             "geo": {**STR, "default": ""}}, ["keyword"]),
    lambda a: ["google_trends.py", "by_region", a["keyword"],
               "--resolution", str(a.get("resolution", "COUNTRY")),
               "--geo", str(a.get("geo", ""))],
)

# ---- Health check ----
tool(
    "health_check",
    "Run a full diagnostic check on all social-scout backends — YouTube, "
    "Reddit, Instagram, TikTok, Quora, web reader — and report READY / NOT "
    "READY / BEST-EFFORT status for each.",
    _schema({}),
    lambda a: ["doctor.py"],
    timeout=TIMEOUT_HEALTH,
)


# --------------------------------------------------------------------------
# MCP server wiring
# --------------------------------------------------------------------------
server = Server("social-scout")


@server.list_tools()
async def list_tools():
    return [
        types.Tool(name=name, description=t["description"], inputSchema=t["schema"])
        for name, t in TOOLS.items()
    ]


@server.call_tool()
async def call_tool(name, arguments):
    arguments = arguments or {}
    spec = TOOLS.get(name)
    if spec is None:
        return [types.TextContent(type="text", text=f"ERROR: unknown tool '{name}'")]

    # Gate login-walled platforms behind the scout browser.
    gate = spec["gate"]
    if gate is not None:
        platform, require_login = gate
        message = await anyio.to_thread.run_sync(
            ensure_browser, platform, require_login)
        if message is not None:
            return [types.TextContent(type="text", text=message)]

    argv = spec["builder"](arguments)
    text, _is_error = await anyio.to_thread.run_sync(
        run_script, argv, spec["timeout"])
    return [types.TextContent(type="text", text=text)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
