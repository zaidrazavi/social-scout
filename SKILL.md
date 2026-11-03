---
name: social-scout
description: >
  Free scraping of YouTube, Reddit, Instagram, TikTok, Quora, and any web
  page — no API keys. Use this skill whenever the user wants to search,
  research, or pull data from any of these: trending/viral videos, video
  transcripts, video stats (views/likes/comments/shares), top comments on a
  reel or TikTok, a channel's uploads, Reddit keyword searches, subreddit
  posts, trending hashtags for a niche, Quora answers on a topic, the
  readable text of any URL, a screenshot of a web page, or the same data
  pulled from a list of URLs. Trigger even when no tool is named — "find
  videos about X", "what is Reddit/Quora saying about Y", "scrape this reel",
  "what's trending on TikTok for Z", "get this webpage's content",
  "screenshot this page", "pull the prices from these 20 product pages", "run
  the scraper doctor / health check" all mean this skill. YouTube/Reddit/web
  reading/Quora need no login; Instagram and TikTok reuse the user's own
  spare-account Chrome session via the scout browser. Manual, on-demand runs
  only — never schedule or automate it.
compatibility: Python 3.8+, pip install -r requirements.txt, Google Chrome
  (only for Instagram/TikTok), optional stealth browser (python
  scripts/obscura.py install). Windows/macOS/Linux.
---

# Social Scout — free multi-platform research scraping

Python scripts under `scripts/`, run with `python` from this skill's
directory. Structured output is JSON on stdout; transcripts and page reads
are plain text. Errors go to stderr with non-zero exit — a failed scrape
always says specifically what broke, never returns a silent empty.

**Manual trigger only:** run these when the user asks. Never schedule them,
loop them, or wire them into any automation, even if asked to "keep an eye
on" something — offer a manual re-run instead.

## Routing a natural-language request

| User asks for | Run |
|---|---|
| "trending/viral YouTube videos about X" | `python scripts/youtube.py search "X" --sort-views` |
| "search YouTube for X" | `python scripts/youtube.py search "X"` |
| "transcript of this YouTube video" | `python scripts/youtube.py transcript <url>` |
| "stats for this YouTube video" / "latest from channel Z" | `python scripts/youtube.py video <url>` / `videos <channel_url>` |
| "search Reddit for Y" | `python scripts/reddit.py search "Y"` (add `--subreddit a,b,c` when plausible subreddits exist) |
| "posts in r/Z" / "comments on this Reddit post" | `python scripts/reddit.py subreddit Z` / `comments --post <url>` |
| "scrape this reel / IG post stats" | `python scripts/instagram.py post <url>` |
| "comments on this reel" | `python scripts/instagram.py comments <url>` |
| "transcribe this reel" | `python scripts/instagram.py transcript <url>` |
| "Instagram hashtags for niche N" | `python scripts/instagram.py hashtags "N"` |
| "stats/caption/hashtags for this TikTok" | `python scripts/tiktok.py video <url>` |
| "comments on this TikTok" | `python scripts/tiktok.py comments <url>` |
| "transcribe this TikTok" | `python scripts/tiktok.py transcript <url>` |
| "trending TikTok hashtags (for niche N)" | `python scripts/tiktok.py hashtags ["N"] [--days 7\|30\|120]` |
| "what does Quora say about T" | `python scripts/quora.py search "T"` |
| "reviews for this Amazon product" | `python scripts/amazon.py reviews <url>` |
| "1-star reviews for this product" | `python scripts/amazon.py reviews <url> --stars 1` |
| "what are people saying about this product on Amazon" | `python scripts/amazon.py reviews <url> --sort helpful` |
| "Amazon product info" | `python scripts/amazon.py product <url>` |
| "active ads for handmade soap" / "competitor ads on Facebook" | `python scripts/meta_ads.py search "handmade soap"` |
| "all ads from this Facebook page" | `python scripts/meta_ads.py page "PageName"` |
| "Google Trends for handmade soap" / "is this keyword trending" | `python scripts/google_trends.py interest "handmade soap"` |
| "compare these keywords on Google Trends" | `python scripts/google_trends.py compare "soap,candles,bath bombs"` |
| "related searches for handmade soap" | `python scripts/google_trends.py related "handmade soap"` |
| "where is handmade soap trending" | `python scripts/google_trends.py by_region "handmade soap"` |
| "reviews for this brand on Trustpilot" | `python scripts/trustpilot.py reviews <domain>` |
| "1-star Trustpilot reviews for competitor" | `python scripts/trustpilot.py reviews <domain> --stars 1` |
| "Trustpilot rating for this brand" | `python scripts/trustpilot.py brand <domain>` |
| "search Google Shopping for handmade soap" / "price comparison" | `python scripts/google_shopping.py search "handmade soap"` |
| "Google Shopping product details" | `python scripts/google_shopping.py product <url>` |
| "Pinterest trends for handmade soap" / "what's trending on Pinterest" | `python scripts/pinterest_trends.py search "handmade soap"` |
| "currently trending on Pinterest" | `python scripts/pinterest_trends.py trending` |
| "what's Twitter saying about X" / "tweets about handmade soap" | `python scripts/twitter.py search "handmade soap"` |
| "recent tweets from this account" | `python scripts/twitter.py user <handle>` |
| "read this webpage / get this URL's content" | `python scripts/webread.py read <url>` |
| "this page comes back empty / it's a JavaScript app" | `python scripts/obscura.py render <url>` |
| "pull every price / headline / link matching X from this page" | `python scripts/obscura.py render <url> --extract "<css selector>"` |
| "run this JavaScript on the page / get a value from the page" | `python scripts/obscura.py render <url> --js "<expression>"` |
| "read this page I'm logged into (not IG/TikTok/FB/X/Pinterest/Amazon)" | `python scripts/obscura.py render <url> --use-scout-login` |
| "screenshot this page / what does this landing page look like" | `python scripts/obscura.py screenshot <url> [--full-page]` |
| "scrape these URLs / get titles+descriptions for this list" | `python scripts/obscura.py scrape <url> <url> ... [--extract "<css>"]` |
| "install the stealth browser" | `python scripts/obscura.py install` (ask first — ~73 MB download) |
| "is the scraper working / doctor / health check" | `python scripts/doctor.py` |

Every script documents its full options via `--help`.

## Session platforms (Instagram, TikTok) — how to handle failures

Both need the **scout browser**: a dedicated Chrome profile with the user's
spare account logged in. Commands stop with exact remediation text when it's
missing — relay that text to the user rather than improvising:

- "Scout browser is not running" → run `python scripts/browser.py launch`
  (you can run this yourself; a Chrome window opens on their machine).
- "No active … login" → the *user* must log into their dedicated spare
  account in that window — one-time per machine. Never ask for or handle
  their credentials yourself; just point them at the window.
- **Security checks (CAPTCHA / "verify you're human") are handed to the
  user, never solved by software.** When a scout-browser page lands on one,
  the scraper raises the Chrome window, prints "A security check … please
  complete it in the Chrome window", waits up to 90s
  (`SOCIAL_SCOUT_CHECK_WAIT`), and carries on by itself once the person has
  done it. If nobody does, it stops with "A security check is waiting in the
  scout browser…" and **leaves that tab open** — tell the user to complete
  it in the Chrome window, then rerun. Never try to click, solve, or work
  around the check yourself.

Self-throttling is by design: `[throttle] waiting Ns` lines on stderr are
normal operation, not errors — mention to the user that pacing is intentional
anti-ban behavior. Comment caps default to 30/post (ceiling 100) for the same
reason; don't fight the caps.

## Reliability tiers — set expectations honestly

- **Solid:** YouTube, Reddit (Arctic Shift + the stealth browser's live
  search, with Reddit `.json` / scout browser / Pullpush behind them), web
  reading, Amazon product info, Trustpilot (direct fetch + scout-browser
  fallback).
- **Session-based, good:** Instagram, TikTok, Amazon reviews — need a spare
  account logged into the scout browser; can hit captcha walls (open the
  scout window, complete the check, retry). Google Shopping is browser-based
  (scout browser) and now returns consistent results.
- **Optional login upgrades these** (they work WITHOUT login in degraded
  mode, login just adds data):
  - **Twitter/X** — logged into a spare X account = full tweet text +
    engagement; **without login it's BEST-EFFORT** (Nitter mirrors, then
    logged-out x.com, then web-search links only).
  - **Pinterest Trends** — logged in = full per-keyword related trends;
    **without login it's limited** to the public regional spotlight.
- **BEST-EFFORT, weaker by design:** Quora — web-search discovery + visible
  page text only. No vote counts, no full threads, no guarantees. Say so
  when presenting Quora results.
- Web reading is **single-URL** extraction, not crawling or internet search.
- **Stealth browser (Obscura, optional):** a headless engine with no window
  and no login. Solid for ordinary public sites, including JavaScript-heavy
  ones. It **cannot** get past logins, CAPTCHAs or "verify you are human"
  pages — Google Shopping, Quora, Trustpilot, Reddit, Amazon, TikTok and
  logged-out Instagram all show it one, which is why those keep their own
  paths. Never present it as a way into login-walled data.

## Platform quirks worth knowing

1. **Reddit source order** (Pullpush is **last** everywhere — it's often down):
   - **Global keyword search:** stealth browser (Reddit's own search page,
     live scores) → Reddit `.json` → scout browser → Pullpush. Arctic Shift
     can't do global text search.
   - **`--subreddit` search:** Arctic Shift (full post bodies) → stealth
     browser → Reddit `.json` → scout browser → Pullpush.
   - **Subreddit feeds / comments:** Arctic Shift → Reddit `.json` → scout
     browser → Pullpush (Reddit blocks the stealth browser on those pages).
   The stealth tier gives title/author/subreddit/exact live score/comment
   count/timestamp but **no post body**, so it's skipped for `--self-only`
   and `--before`. It returns the **most relevant** posts (within the `--after`
   window when set), whereas the archives return newest first. Reddit `.json` is a lightweight tier that Reddit often
   blocks (especially datacenter/VPN IPs) — don't lean on it for bulk work.
   `via` in the output says which source answered (`stealth_browser`,
   `arctic_shift`, `reddit_json`, `scout_browser`, `pullpush`).
2. **Arctic Shift 422s under load** — scripts retry then fall back; don't
   hammer.
3. **Archive scores are snapshots; Reddit `.json` is live.** Very recent
   posts show near-zero scores via the archives — don't report those as live
   counts. If a result's `"via"` is `reddit_json`, those numbers ARE live.
4. **YouTube bot checks are intermittent** — scripts auto-retry with an
   alternate client; persistent failure → `pip install -U yt-dlp`.
5. **IG view counts**: Instagram exposes `play_count` for reels; carousels
   and photos have no view count — report what exists.
6. **TikTok trending list may be short** (top rows only without a TikTok One
   business login) — the output's `note` field says so; pass it on.
7. **Transcripts ladder**: native captions → **local Whisper** (faster-whisper,
   runs on the user's machine, no key; model auto-downloads on first use, so
   the first transcript in a session is slow) → video downloaded + explicit
   "no transcript possible" note (music-only/silent videos land here). Always
   tell the user which source produced the text (auto-transcripts contain
   errors) and that the first run may take a bit while the model downloads.
8. **Reddit output reports `"via"`**, transcripts report `# source:` — pass
   these through when relevant.
9. **Amazon**: `product` (public `/dp/` page) needs no login. `reviews` are
   gated behind an Amazon **sign-in wall**, so they need an Amazon account
   logged into the scout browser once (a spare is safest; it persists like
   IG/TikTok) — the tool returns a clear message if not. Amazon bot-blocks
   scripted requests, so both commands lean on the running scout browser;
   `--stars 1` surfaces complaints/pain points, `--stars 5` what people love.
10. **Meta Ad Library** needs **no Facebook login** (it's public). It renders
    in the stealth browser when installed (no Chrome window), else in the
    scout browser (auto-launched); the output's `engine` says which.
    Platforms (FB/IG) are icon-only on the summary card and usually can't be
    read — the output says so. Longer `started_running` dates ≈ winning ads.
11. **Google Trends** (via pytrends) rate-limits hard — use sparingly; on a
    429 it backs off then returns a clear "rate-limited, retry later" message.
    Numbers are **relative interest 0-100, not absolute search volume**;
    `rising` related queries (big % jumps) are the emerging-trend signal.
12. **Trustpilot** is locale-specific — the same brand differs on `.com` vs
    `.co.uk`. It blocks scripts, so in practice it runs through the scout
    browser; `--stars 1` = pain points, `--stars 5` = what customers love.
13. **Google Shopping** uses the scout browser on Google's `udm=28` surface
    and returns product title/price/seller consistently. Prices are
    region/IP-dependent; ratings often don't render. Still supplementary —
    don't treat it as authoritative pricing.
14. **Pinterest Trends** — **login is optional and upgrades it:** logged into
    a Pinterest account in the scout browser → full per-keyword related
    trends (`source: logged-in`); logged out → the **public regional
    spotlight** matched to the query (`source: public spotlight`), rendered
    by the stealth browser when installed, else the scout browser. Either
    way, relative signals, not exact volumes. The interest chart is an SVG
    and isn't extracted.
15. **Twitter/X — login is optional and upgrades it.** Logged into a spare X
    account in the scout browser → full tweet text + engagement (tier 0).
    Without login: the stealth browser reads **profiles** (`user`) with
    exact like/repost/reply/view counts, real dates and a `profile` block
    (followers, following, posts, joined) — but X sends logged-out **search**
    to a login page, so `search` stays BEST-EFFORT: Nitter mirrors (mostly
    dead) → logged-out x.com in the scout browser → web-search **link-only**
    results. Always report the `source` field.
16. **Stealth browser (`obscura.py`)**: `render` gives markdown by default
    (`--format text|html|links`); `--extract` takes a CSS selector, `--js`
    an expression (promises awaited). `scrape` runs up to 100 URLs, 5 at a
    time, and reports failures per URL; its `--js` must be synchronous.
    `screenshot` saves to `downloads/screenshots/` (full pages capped at
    8000px tall). Bot-check pages are never returned as content: `render`
    and `screenshot` **relay** them to the scout browser (real Chrome), where
    the user can pass any check (see "Security checks" above) — the output's
    `engine`/`# via:` then says `scout browser`, and markdown becomes plain
    text. `--no-chrome-fallback` (MCP: `chrome_fallback=false`) stops
    instead. `scrape` never relays; blocked URLs are marked per result —
    rerun those individually with `render`. `--use-scout-login` copies only that site's cookies
    from the scout browser and **refuses** Instagram/TikTok/Facebook/X/
    Pinterest/Amazon to protect the spare accounts — use their own commands.
    If it isn't installed, the commands say how; ask the user before running
    `install` (~73 MB from GitHub, SHA-256 verified). Optional proxy: set
    `SOCIAL_SCOUT_PROXY` (e.g. `socks5://127.0.0.1:1080`).
17. **`web_read` picks the better engine itself:** Jina Reader first; if Jina
    fails, hits a bot check, or flags the page as not fully loaded, the
    stealth browser renders it and the longer read wins. Output `# via:`
    says which (`jina-reader`, `stealth-browser`, `local-fallback`).

All four of these (Trustpilot, Google Shopping, Pinterest, Twitter/X) are
supplementary research tools, not primary platforms.

## Presenting results

Summarize rather than dumping raw JSON: the numbers asked for, links,
patterns you noticed, result counts, date windows. Note when a fallback
backend answered, when output was truncated, and when a platform was
degraded. For Quora, lead with the best-effort caveat.

## Install

See [README.md](README.md) for full team-facing setup. Short version:
`pip install -r requirements.txt`, copy this folder to
`~/.claude/skills/social-scout/` (or `%USERPROFILE%\.claude\skills\` on
Windows), run `python scripts/doctor.py`. Instagram/TikTok additionally need
the one-time `python scripts/browser.py launch` + spare-account login.
Optional: `python scripts/obscura.py install` for the stealth browser.
