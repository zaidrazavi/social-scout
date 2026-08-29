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

## Install

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
