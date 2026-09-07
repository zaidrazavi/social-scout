---
name: social-scout
description: >
  Free scraping of YouTube, Reddit, Instagram, TikTok, Quora, and any web
  page — no API keys. Use this skill whenever the user wants to search,
  research, or pull data from any of these: trending/viral videos, video
  transcripts, video stats (views/likes/comments/shares), top comments on a
  reel or TikTok, a channel's uploads, Reddit keyword searches, subreddit
  posts, trending hashtags for a niche, Quora answers on a topic, or the
  readable text of any URL. Trigger even when no tool is named — "find videos
  about X", "what is Reddit/Quora saying about Y", "scrape this reel",
  "what's trending on TikTok for Z", "get this webpage's content", "run the
  scraper doctor / health check" all mean this skill. YouTube/Reddit/web
  reading/Quora need no login; Instagram and TikTok reuse the user's own
  spare-account Chrome session via the scout browser. Manual, on-demand runs
  only — never schedule or automate it.
compatibility: Python 3.8+, pip install -r requirements.txt, Google Chrome
  (only for Instagram/TikTok). Windows/macOS/Linux.
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
| "read this webpage / get this URL's content" | `python scripts/webread.py read <url>` |
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

Self-throttling is by design: `[throttle] waiting Ns` lines on stderr are
normal operation, not errors — mention to the user that pacing is intentional
anti-ban behavior. Comment caps default to 30/post (ceiling 100) for the same
reason; don't fight the caps.

## Reliability tiers — set expectations honestly

- **Solid:** YouTube, Reddit (dual archive backends), web reading.
- **Session-based, good:** Instagram, TikTok — can hit captcha walls; the
  fix is human (open the scout window, complete the check, retry).
- **BEST-EFFORT, weaker by design:** Quora — web-search discovery + visible
  page text only. No vote counts, no full threads, no guarantees. Say so
  when presenting Quora results.
- Web reading is **single-URL** extraction, not crawling or internet search.

## Platform quirks worth knowing

1. **Reddit source order is Arctic Shift → Pullpush → Reddit's own `.json`.**
   The third tier (Reddit's public `.json`) is a **lightweight last resort**,
   not a primary path — it only fires when both archives fail. Reddit
   rate-limits/blocks unauthenticated `.json`, especially from datacenter/VPN
   IPs, so it jitters + backs off and may still return a clear "Reddit is
   blocking this network" error. Don't lean on it for bulk work. For global
   keyword search Arctic Shift is skipped (it needs a subreddit/author
   filter), so that path is Pullpush → Reddit `.json`; `--subreddit` lists
   still give the most reliable searches.
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
