# CLAUDE.md — social-scout

Project context for Claude Code. Read this first when continuing work here.

## What this is

A **manual, on-demand research scraping toolkit** — no API keys, no paid
services, no credentials in the repo. Used two ways:
1. **Claude Code skill** (`SKILL.md` + `scripts/`).
2. **Claude Desktop MCP server** (`mcp_server.py`) — **34 tools across 11
   platforms**, the recommended path for non-technical teammates.

GitHub: `github.com/zaidrazavi/social-scout` (private). Push to continue.

## Architecture (the pattern — follow it)

- **`scripts/*.py`** — each is a **standalone CLI** that prints **JSON to
  stdout** (plain text for transcripts/web reads) and errors to stderr. They
  import peers directly (`import common`, `import browser`).
- **`scripts/common.py`** — shared utils every scraper uses: `emit()` (JSON
  out), `die()` (error + exit), `jitter_sleep()` (2–6s randomized), 
  `backoff_retry()` (exp backoff on 429/5xx), `clamp_cap()` (caps). 
  Caps philosophy: **default 30, hard max 100**.
- **`scripts/browser.py`** — the **"scout browser"**: a dedicated Chrome
  profile driven over CDP (port 9223). `launch`/`status`/`stop`. Login state
  via `logged_in(domain, cookie_names, value_ok)`. Logins persist in the
  profile at `%LOCALAPPDATA%\social-scout\chrome-profile` (machine-level).
- **`mcp_server.py`** — thin wrapper: runs each script as a **subprocess**
  (never imports them), `cwd=scripts/`, stdin=DEVNULL. Registers tools via the
  `tool(name, desc, schema, builder, gate, timeout)` helper. `gate=("x", bool)`
  ensures the scout browser is running (and optionally logged in) first.
  Uses **`mcp<2`** (low-level decorator API).
- **`scripts/doctor.py`** — health check. `--quick` = fast (<30s, concurrent,
  10s/check, friendly ✅/⚠️/❌ output) used by the MCP `health_check` tool;
  no flag = thorough technical diagnostics.

## Platforms & reliability (be honest about these)

- **Solid, no login:** YouTube (yt-dlp), Reddit (Arctic Shift→Pullpush→Reddit
  `.json`→**scout browser** fallback), web reading (Jina+local), Trustpilot
  (`__NEXT_DATA__` JSON), Amazon **product** (public `/dp/`).
- **Session-based (spare account in scout browser):** Instagram, TikTok,
  Amazon **reviews** (sign-in wall). Meta Ad Library + Google Shopping need the
  browser running but **no login**.
- **Optional-login upgrades (work degraded without it):** Pinterest Trends
  (`_auth` cookie → per-keyword, else public spotlight), Twitter/X
  (`auth_token` cookie → full tweets, else Nitter→logged-out x.com→web-search).
- **Best-effort:** Quora (web-search discovery + page read), Twitter/X without
  login. Google Shopping is browser-based and workable but Google rotates its
  layout — don't treat as authoritative.
- **Transcripts:** native captions → local `faster-whisper` (no key, model
  auto-downloads) → clear "no transcript" note.

## Rules when adding/changing a scraper

1. **Don't modify existing scripts** that aren't the one you're fixing.
2. Use `common.py` (`emit`/`die`/`jitter_sleep`/`backoff_retry`/`clamp_cap`).
3. For blocked sites: **direct fetch → scout-browser fallback** (optional —
   skip if browser not running, like `amazon.py`/`quora.py`/`reddit.py`).
4. Register the tool in `mcp_server.py`, add a `doctor.py` check + verdict,
   add a `SKILL.md` routing row + quirk, update `README.md`.
5. User-facing errors: **plain language, no HTTP codes or backend names.**
6. Browser logins are **optional** for Twitter/Pinterest (degraded mode),
   **required** for Instagram/TikTok. Never change that.

## How to run / test (Windows, Python 3.11)

```bash
pip install -r requirements.txt            # deps: yt-dlp, websocket-client,
                                           # faster-whisper, curl_cffi, mcp<2,
                                           # beautifulsoup4, lxml, pytrends
python scripts/browser.py launch           # start scout browser (one time)
python scripts/doctor.py --quick           # fast health check (<30s)
python scripts/doctor.py                   # full technical diagnostics
python scripts/<name>.py <subcommand> ...  # test any scraper standalone
```

MCP smoke test (34 tools, server starts clean):
```python
# minimal: stdio_client -> ClientSession -> initialize -> list_tools()
```

## Current state

- **34 tools, 11 platforms.** All committed & pushed (`main`).
- MCP config lives in `C:\Users\Zaid\.claude.json` (machine-level) and the
  skill copy in `%USERPROFILE%\.claude\skills\social-scout\`.
- Attribution: user prefers commits authored by themselves only — **no
  co-author trailer.** Commit/push only when asked.

## Deliberately out of scope

Scheduling/automation of any kind; shared accounts; site-wide crawling;
claims of "scrape anything perfectly." It's manual, on-demand, best-effort
where noted.
