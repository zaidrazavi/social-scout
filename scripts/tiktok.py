#!/usr/bin/env python3
"""TikTok scraping through YOUR OWN logged-in scout browser session.

Requires the scout browser (scripts/browser.py launch) with a DEDICATED SPARE
TikTok account logged in — commands hard-stop with instructions when no
session is detected. Video pages embed a full JSON state blob
(__UNIVERSAL_DATA_FOR_REHYDRATION__) which supplies metadata without any
extra API calls; comments use TikTok's own web API from inside the page.
Trending hashtags come from TikTok Creative Center (public, no login).

CLI:
  video <url>                    caption, like/comment/share/view counts,
                                 duration, hashtags, author, date
  comments <url> [--max-comments N]   top comments (default 30, ceiling 100)
  transcript <url> [--lang en]   native captions -> Whisper (if key) -> note
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


def _iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_video(url, require_login=True):
    b = browser.attach()
    if require_login:
        b.require_login("tiktok.com", "TikTok")
    tab = b.page(url, settle=6)
    return b, tab


def item_struct(tab):
    """Pull the embedded JSON state from the video page."""
    raw = tab.eval(
        "document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')?.textContent")
    if not raw:
        raise RuntimeError(
            "TikTok page no longer embeds __UNIVERSAL_DATA_FOR_REHYDRATION__ — "
            "the platform likely changed its page structure, or you hit a "
            "captcha wall. Open the scout browser window to check, and run "
            "scripts/doctor.py")
    data = json.loads(raw)
    detail = (data.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}
    item = (detail.get("itemInfo") or {}).get("itemStruct")
    if not item:
        status = detail.get("statusMsg") or detail.get("statusCode")
        raise RuntimeError(
            f"TikTok returned no video data (status: {status}). The video may "
            "be private/deleted/region-locked, or you hit a captcha — open the "
            "scout browser window to check.")
    return item


def norm_video(item):
    stats = item.get("stats") or {}
    return {
        "id": item.get("id"),
        "url": f"https://www.tiktok.com/@{(item.get('author') or {}).get('uniqueId')}/video/{item.get('id')}",
        "author": (item.get("author") or {}).get("uniqueId"),
        "caption": item.get("desc"),
        "hashtags": ["#" + c.get("title", "") for c in item.get("challenges") or []],
        "like_count": stats.get("diggCount"),
        "comment_count": stats.get("commentCount"),
        "share_count": stats.get("shareCount"),
        "view_count": stats.get("playCount"),
        "saved_count": stats.get("collectCount"),
        "duration_seconds": (item.get("video") or {}).get("duration"),
        "posted_at": _iso(item.get("createTime")),
        "music": (item.get("music") or {}).get("title"),
    }


def cmd_video(args):
    _, tab = open_video(args.url)
    try:
        emit(norm_video(backoff_retry(lambda: item_struct(tab), "TikTok video data")))
    finally:
        tab.close()


def cmd_comments(args):
    cap = clamp_cap(args.max_comments, DEFAULT_MAX_COMMENTS,
                    HARD_MAX_COMMENTS, "max-comments")
    _, tab = open_video(args.url)
    try:
        item = item_struct(tab)
        video_id = item["id"]
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
    b, tab = open_video(args.url)
    tab.close()
    cookies = b.cookies_txt(["tiktok.com"])
    try:
        result = transcribe.transcribe(args.url, cookies, args.lang)
    except RuntimeError as e:
        die(str(e))
    finally:
        import os
        os.unlink(cookies)
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
