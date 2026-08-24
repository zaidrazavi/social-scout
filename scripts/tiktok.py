#!/usr/bin/env python3
"""TikTok scraping through YOUR OWN logged-in scout browser session.

Requires the scout browser (scripts/browser.py launch) with a DEDICATED SPARE
TikTok account logged in — commands hard-stop with instructions when no
session is detected.

TikTok blocks direct video-page loads in the browser, so:
- metadata/download come from yt-dlp. Counterintuitively this runs WITHOUT
  the scout cookies: an authenticated-but-unsigned request trips TikTok's
  anti-bot and 403s, whereas an anonymous yt-dlp fetch of the (public) video
  succeeds. Browser TLS impersonation (curl_cffi) is used when available for
  robustness, with a plain fallback so curl_cffi isn't a hard requirement.
- comments come from TikTok's own comment API called from inside a live,
  logged-in tiktok.com page (that endpoint needs the session and still
  answers a same-origin fetch).
Trending hashtags come from TikTok Creative Center (public, no login).

CLI:
  video <url>                    caption, like/comment/share/view counts,
                                 duration, hashtags, author, date
  comments <url> [--max-comments N]   top comments (default 30, ceiling 100)
  transcript <url> [--lang en]   native captions -> local Whisper -> note
  hashtags [niche] [--days 7|30|120]  Creative Center trending hashtags;
                                 optional niche keyword filters the list
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone

import browser
import transcribe
from common import (DEFAULT_MAX_COMMENTS, HARD_MAX_COMMENTS, backoff_retry,
                    clamp_cap, die, emit, jitter_sleep)

CREATIVE_CENTER = "https://ads.tiktok.com/creative/creativeCenter/trends/hashtag"
IMPERSONATE = "chrome"  # yt-dlp target that clears TikTok's TLS fingerprinting


def _iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_url(url):
    """Return (video_id, author_handle) from a standard TikTok video URL."""
    vid = re.search(r"/video/(\d+)", url)
    author = re.search(r"tiktok\.com/@([\w.\-]+)", url)
    return (vid.group(1) if vid else None,
            author.group(1) if author else None)


def context_page(b, author):
    """A live logged-in tiktok.com page to run same-origin API fetches from.
    Direct video URLs 403, but a profile/home page renders fine and carries
    the session, so comment-API fetches from here succeed."""
    url = f"https://www.tiktok.com/@{author}" if author else "https://www.tiktok.com/"
    return b.page(url, settle=6)


def hashtags_from(desc):
    return ["#" + h for h in re.findall(r"#(\w+)", desc or "")]


def norm_video(info):
    """Map a yt-dlp TikTok info dict to our shape."""
    desc = info.get("description") or info.get("title") or ""
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "author": info.get("uploader") or info.get("channel"),
        "caption": desc,
        "hashtags": hashtags_from(desc),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "share_count": info.get("repost_count"),
        "view_count": info.get("view_count"),
        "duration_seconds": info.get("duration"),
        "posted_at": _iso(info.get("timestamp")),
        "music": info.get("track"),
    }


def ytdlp_fetch(fn):
    """Run a yt-dlp call trying TLS impersonation first, then plain. Keeps
    curl_cffi optional: if the impersonate target is unavailable the plain
    attempt still works. `fn` takes the impersonate value and returns a result
    or raises RuntimeError."""
    last = None
    for imp in (IMPERSONATE, None):
        try:
            return fn(imp)
        except RuntimeError as e:
            last = e
    raise last


def cmd_video(args):
    b = browser.attach()
    b.require_login("tiktok.com", "TikTok")
    # NB: yt-dlp runs cookieless on purpose — passing the logged-in session
    # cookies makes TikTok 403 the request; the public video reads fine anon.
    info = backoff_retry(
        lambda: ytdlp_fetch(lambda imp: transcribe.ytdlp_info(args.url, None, imp)),
        "TikTok video metadata")
    emit(norm_video(info))


def cmd_comments(args):
    cap = clamp_cap(args.max_comments, DEFAULT_MAX_COMMENTS,
                    HARD_MAX_COMMENTS, "max-comments")
    video_id, author = parse_url(args.url)
    if not video_id:
        die(f"Couldn't find a video id in '{args.url}' "
            "(expected .../video/<digits>)")
    b = browser.attach()
    b.require_login("tiktok.com", "TikTok")
    tab = context_page(b, author)
    try:
        rows, cursor = [], 0
        while len(rows) < cap:
            js = f"""
              fetch("https://www.tiktok.com/api/comment/list/?aid=1988&aweme_id={video_id}&count=20&cursor={cursor}",
                    {{credentials: "include"}})
                .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
            """
            data = backoff_retry(lambda: tab.eval(js, timeout=90), "TikTok comments")
            if data.get("status_code") not in (0, None):
                raise RuntimeError(
                    f"TikTok comment API rejected the request "
                    f"(status_code={data.get('status_code')}) — usually a "
                    "captcha/verification wall; open the scout browser window, "
                    "browse normally for a minute, then retry")
            batch = data.get("comments") or []
            if not batch:
                break
            for c in batch:
                rows.append({
                    "author": (c.get("user") or {}).get("unique_id")
                              or (c.get("user") or {}).get("nickname"),
                    "text": c.get("text"),
                    "like_count": c.get("digg_count"),
                    "created": _iso(c.get("create_time")),
                })
            if not data.get("has_more"):
                break
            cursor = data.get("cursor", cursor + len(batch))
            if len(rows) >= cap:
                break
            jitter_sleep(2, 6, "pacing comment pages")
        emit({"video": args.url, "count": min(len(rows), cap),
              "comments": rows[:cap]})
    finally:
        tab.close()


def cmd_transcript(args):
    b = browser.attach()
    b.require_login("tiktok.com", "TikTok")
    # Cookieless for the same reason as cmd_video (avoids TikTok's 403).
    try:
        result = backoff_retry(
            lambda: ytdlp_fetch(lambda imp:
                                transcribe.transcribe(args.url, None, args.lang, imp)),
            "TikTok transcript")
    except RuntimeError as e:
        die(str(e))
    transcribe.emit_result(result, args.url)


def cmd_hashtags(args):
    # Creative Center is public — no login needed, so don't require one.
    b = browser.attach()
    url = CREATIVE_CENTER + f"?region=US&period={args.days}"
    tab = b.page(url, settle=8)
    try:
        tags = backoff_retry(lambda: scrape_creative_center(tab),
                             "Creative Center trending hashtags")
        note = (f"TikTok Creative Center trending, last {args.days} days. "
                "Without a TikTok One business login the page only exposes its "
                "top rows, so this list may be short.")
        if args.niche:
            wanted = args.niche.lower().split()
            filtered = [t for t in tags
                        if any(w in t["hashtag"].lower()
                               or w in (t.get("industry") or "").lower()
                               for w in wanted)]
            if filtered:
                tags = filtered
                note += f" Filtered by '{args.niche}' (name/industry match)."
            else:
                note += (f" Nothing matched '{args.niche}' by name or industry; "
                         "showing the full platform-wide list instead.")
        emit({"note": note, "count": len(tags), "hashtags": tags})
    finally:
        tab.close()


def scrape_creative_center(tab):
    """Parse the rendered trending-hashtags table out of the page text.
    The table renders as repeating runs of lines:
        <rank> / #<tag> / [industry] / <n> / Posts / <n> / Views
    Parsing visible text instead of CSS classes keeps this resilient to
    TikTok's frequent cosmetic redesigns. Without a (business) login the
    page shows only the top handful of rows — reported honestly in count."""
    body = tab.eval("document.body.innerText") or ""
    rows = []
    pattern = re.compile(
        r"^(\d{1,3})\n#([^\n]+)\n(?:([^\n#][^\n]*)\n)?"
        r"([\d.,]+[KMB]?)\nPosts\n([\d.,]+[KMB]?)\nViews",
        re.MULTILINE)
    for m in pattern.finditer(body):
        rank, tag, industry, posts, views = m.groups()
        rows.append({"rank": int(rank), "hashtag": "#" + tag.strip(),
                     "industry": industry.strip() if industry else None,
                     "posts": posts, "views": views})
    if not rows:
        raise RuntimeError(
            "Creative Center page loaded but no hashtag rows were found — "
            "TikTok may have redesigned the page or shown a consent screen; "
            "open the scout browser window to check what it displays")
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("video", help="metadata for one video")
    v.add_argument("url")
    v.set_defaults(func=cmd_video)

    c = sub.add_parser("comments", help="top comments on a video")
    c.add_argument("url")
    c.add_argument("--max-comments", type=int,
                   help=f"default {DEFAULT_MAX_COMMENTS}, ceiling {HARD_MAX_COMMENTS}")
    c.set_defaults(func=cmd_comments)

    t = sub.add_parser("transcript", help="download + transcript for a video")
    t.add_argument("url")
    t.add_argument("--lang", default="en")
    t.set_defaults(func=cmd_transcript)

    h = sub.add_parser("hashtags", help="Creative Center trending hashtags")
    h.add_argument("niche", nargs="?", help="optional keyword filter")
    h.add_argument("--days", type=int, choices=[7, 30, 120], default=7)
    h.set_defaults(func=cmd_hashtags)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
