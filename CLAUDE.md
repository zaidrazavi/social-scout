# CLAUDE.md — social-scout

Project context for Claude Code. Read this first when continuing work here.

## What this is

A **manual, on-demand research scraping toolkit** — no API keys, no paid
services, no credentials in the repo. Used two ways:
1. **Claude Code skill** (`SKILL.md` + `scripts/`).
2. **Claude Desktop MCP server** (`mcp_server.py`) — **38 tools across 11
   platforms + generic web pages**, the recommended path for non-technical
   teammates.

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
- **`scripts/obscura.py`** — the **"stealth browser"**: Obscura
  (github.com/h4ckf0r0day/obscura, Rust+V8 headless, no window, no login).
  Binary at `%LOCALAPPDATA%\social-scout\bin\` (installed by `obscura.py
  install`: pinned `v0.2.6`, render+stealth build, SHA-256 checked against
  GitHub's asset digest). Two engines: `fetch()` wraps the CLI (one-shot
  dump); `Session()` runs a private `obscura serve` on a free port and
  `.page()` returns a `Tab` with the **same `send/eval/navigate/close`
  interface as `browser.Tab`**, so existing in-page extractors run on either
  engine. Obscura only speaks *flattened-session* CDP (createTarget →
  attachToTarget flatten → sessionId on the browser socket) — per-page
  websockets and `/json/new` don't work. `looks_blocked()` detects bot-check
  pages. Opt-in `SOCIAL_SCOUT_PROXY`.
- **`mcp_server.py`** — thin wrapper: runs each script as a **subprocess**
  (never imports them), `cwd=scripts/`, stdin=DEVNULL. Registers tools via the
  `tool(name, desc, schema, builder, gate, timeout, obscura_ok, image)` helper.
  `gate=("x", bool)` ensures the scout browser is running (and optionally
  logged in) first; `gate=("obscura", False)` ensures the stealth browser is
  installed; `obscura_ok=True` skips the Chrome launch when the stealth
  browser is installed (the script falls back to Chrome itself);
  `image=True` returns the PNG at the script's JSON `path` as ImageContent.
  Builders put `--` before URLs so a URL can't be parsed as a flag.
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
- **Stealth browser — tested reality (Nov 2026), keep claims to this:**
  works logged-out on Meta Ad Library (identical ads/IDs to Chrome), Pinterest
  public spotlight (identical), x.com **profiles** (exact counts via the
  article aria-label summary; X serves it markup without `data-testid`, hence
  `X_LITE_EXTRACTOR`), and ordinary JS sites. **Blocked / no use:** Google
  Shopping (CAPTCHA), Quora + Trustpilot (Cloudflare check), Reddit (network
  block), Amazon (interstitial), TikTok + Creative Center, YouTube (SPA
  doesn't render — yt-dlp is better), logged-out Instagram, x.com search
  (login redirect). Never add CAPTCHA/challenge solving.
- **Engine order:** `web_read` = Jina → stealth (also when Jina says "not
  yet fully loaded"; longer read wins) → local. Meta/Pinterest-public =
  stealth → scout browser. Twitter = logged-in scout → stealth x.com →
  Nitter → logged-out scout → web search. `web_render`/`web_screenshot` =
  stealth → (on a bot-check page) scout browser + human handoff.
- **Reddit order (user decision: Pullpush LAST, it's often down):** global
  keyword search = stealth (Reddit's own `/search/` page; 7 results/screen,
  more via its own `/svc/shreddit/search/?…cursor=` "load more" fragment,
  fetched with 1.5–3s pauses) → Reddit `.json` → scout browser → Pullpush.
  `--subreddit` search = Arctic Shift → stealth → `.json` → scout → Pullpush.
  Feeds/comments = Arctic → `.json` → scout → Pullpush (Reddit blocks the
  stealth browser on feed/`.json`/post pages). Stealth tier has no post
  body → skipped for `--self-only`/`--before`. It always sorts by **relevance**
  (Reddit's sort=new search was 1/8 on-topic vs 8/8 in testing); `--after`
  adds Reddit's `t=` time filter (smallest of day/week/month/year covering
  the window), then `within_range` trims to the exact date. `with_fallback` skips a tier
  that returns None; Reddit's "couldn't find any results" → [] (real empty).
- **Human-check handoff (`browser.py`):** `Tab.navigate()` → `check_reason()`
  → `wait_for_human()` raises the window (bringToFront + minimize/restore),
  polls until the check is gone (`SOCIAL_SCOUT_CHECK_WAIT`, default 90s),
  else raises `CheckPending` and sets `keep_open` so `Tab.close()` leaves
  the check tab for the person. Software never interacts with the check.
  Detection must stay conservative — a false alarm stalls a scraper 90s.
  Learned the hard way: Google's reCAPTCHA **corner badge** (iframe in
  `.grecaptcha-badge`/`.grecaptcha-logo`, 256×60) sits on normal pages like
  Trustpilot. Rules now: URL = first path segment only (`/challenge`,
  `/checkpoint`, `/sorry`, …) or Amazon/Cloudflare substrings; gate widgets
  (TikTok slider, PerimeterX, Amazon captcha form) count when visible;
  generic CAPTCHA iframes only if visible, not badge/invisible, AND page
  text < 2500 chars; else `obscura.looks_blocked()` on short pages.
- **`--use-scout-login`** copies one site's cookies (domain-match rule) from
  the scout browser into the stealth browser; it **refuses** IG/TikTok/FB/X/
  Pinterest/Amazon so spare accounts never appear from a second
  fingerprint. Obscura itself silently refuses cookies for httpbin.org and
  postman-echo.com — test with httpbin.io.

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
7. The stealth browser is **optional**: every path that uses it must still
   work (via the scout browser or a clear install hint) when it's missing,
   and must fall back when it lands on a bot-check page.
8. **Security checks are passed by the human, never by code.** No CAPTCHA
   solving, challenge-cracking, clearance-cookie copying, or fingerprint
   tricks to beat a block. The handoff only detects, raises the window,
   and waits.

## How to run / test (Windows, Python 3.11)

```bash
pip install -r requirements.txt            # deps: yt-dlp, websocket-client,
                                           # faster-whisper, curl_cffi, mcp<2,
                                           # beautifulsoup4, lxml, pytrends
python scripts/browser.py launch           # start scout browser (one time)
python scripts/obscura.py install          # stealth browser (optional, ~73 MB)
python scripts/obscura.py render <url>     # quick stealth-browser test
python scripts/doctor.py --quick           # fast health check (<30s)
python scripts/doctor.py                   # full technical diagnostics
python scripts/<name>.py <subcommand> ...  # test any scraper standalone
```

MCP smoke test (38 tools, server starts clean):
```python
# minimal: stdio_client -> ClientSession -> initialize -> list_tools()
```

## Current state

- **38 tools, 11 platforms + generic web.** Stealth-browser integration
  (Phases 1–3: `obscura.py`, web_render/web_screenshot/web_scrape_many/
  stealth_browser_install, webread tier, Meta/Pinterest/X Chrome-free paths,
  proxy, scout-login reuse) built Nov 2026.
- MCP config lives in `C:\Users\Zaid\.claude.json` (machine-level) and the
  skill copy in `%USERPROFILE%\.claude\skills\social-scout\`.
- Attribution: user prefers commits authored by themselves only — **no
  co-author trailer.** Commit/push only when asked.

## Deliberately out of scope

Scheduling/automation of any kind; shared accounts; site-wide crawling;
claims of "scrape anything perfectly." It's manual, on-demand, best-effort
where noted.
