#!/usr/bin/env python3
"""Instagram scraping through YOUR OWN logged-in scout browser session.

Requires the scout browser (scripts/browser.py launch) with a DEDICATED SPARE
Instagram account logged in — every command hard-stops with instructions if
no session is found, and never proceeds silently. All requests execute inside
the real browser page context, so Instagram sees ordinary web-app traffic
from your session; pacing/backoff comes from common.py.

CLI:
  post <url>                     caption, like/comment/view counts, date, author
  comments <url> [--max-comments N]   top comments (default 30, ceiling 100)
  transcript <url> [--lang en]   native captions -> Whisper (if key) -> note
  hashtags <niche>               hashtag discovery seeded by the niche keyword,
                                 ranked by Instagram's own search (IG exposes
                                 no global "trending hashtags" list)
"""

import argparse
import json
import re
import sys

import browser
import transcribe
from common import (DEFAULT_MAX_COMMENTS, HARD_MAX_COMMENTS, backoff_retry,
                    clamp_cap, die, emit, jitter_sleep)

IG_APP_ID = "936619743392459"  # instagram.com's own web-app id, sent by every browser
SHORTCODE_ALPHABET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      "abcdefghijklmnopqrstuvwxyz0123456789-_")


def shortcode_from_url(url):
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        die(f"'{url}' doesn't look like an Instagram post/reel URL "
            "(expected .../p/<code>/ or .../reel/<code>/)")
    return m.group(1)


def media_pk(shortcode):
    """The numeric media id is just the shortcode base64url-decoded."""
    n = 0
    for ch in shortcode[:11]:  # extra chars beyond 11 encode the poster id
        n = n * 64 + SHORTCODE_ALPHABET.index(ch)
    return n


def ig_fetch(tab, path):
    """Run a fetch against Instagram's own web API from inside the page, so
    cookies, TLS fingerprint, and headers are all genuinely the browser's."""
    js = f"""
      fetch({json.dumps(path)}, {{
        headers: {{"x-ig-app-id": "{IG_APP_ID}"}},
        credentials: "include"
      }}).then(r => r.ok ? r.json()
                         : r.text().then(t => {{throw new Error("HTTP " + r.status + " " + t.slice(0,120))}}))
    """
    return tab.eval(js, timeout=90)


def open_post(url):
    b = browser.attach()
    b.require_login("instagram.com", "Instagram")
    tab = b.page(url, settle=5)
    return b, tab


def media_info(tab, pk):
    data = ig_fetch(tab, f"/api/v1/media/{pk}/info/")
    items = data.get("items") or []
    if not items:
        raise RuntimeError("Instagram returned no media item — the post may be "
                           "private/deleted, or IG changed its web API "
                           "(run scripts/doctor.py)")
    return items[0]


def norm_post(m):
    caption = (m.get("caption") or {}).get("text", "")
    return {
        "code": m.get("code"),
        "url": f"https://www.instagram.com/p/{m.get('code')}/",
        "author": (m.get("user") or {}).get("username"),
        "caption": caption,
        "like_count": m.get("like_count"),
        "comment_count": m.get("comment_count"),
        "view_count": m.get("play_count") or m.get("view_count"),
        "posted_at": _iso(m.get("taken_at")),
        "duration_seconds": m.get("video_duration"),
        "is_video": m.get("media_type") == 2,
    }


def _iso(ts):
    from datetime import datetime, timezone
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_post(args):
    pk = media_pk(shortcode_from_url(args.url))
    _, tab = open_post(args.url)
    try:
        m = backoff_retry(lambda: media_info(tab, pk), "Instagram media info")
        emit(norm_post(m))
    finally:
        tab.close()


def cmd_comments(args):
    cap = clamp_cap(args.max_comments, DEFAULT_MAX_COMMENTS,
                    HARD_MAX_COMMENTS, "max-comments")
    pk = media_pk(shortcode_from_url(args.url))
    _, tab = open_post(args.url)
    try:
        rows, min_id = [], None
        while len(rows) < cap:
            path = (f"/api/v1/media/{pk}/comments/"
                    "?can_support_threading=true&permalink_enabled=false"
                    + (f"&min_id={min_id}" if min_id else ""))
            data = backoff_retry(lambda: ig_fetch(tab, path), "Instagram comments")
            batch = data.get("comments") or []
            if not batch:
                break
            for c in batch:
                rows.append({
                    "author": (c.get("user") or {}).get("username"),
                    "text": c.get("text"),
                    "like_count": c.get("comment_like_count"),
                    "created": _iso(c.get("created_at")),
                })
            min_id = data.get("next_min_id")
            if not min_id or len(rows) >= cap:
                break
            jitter_sleep(2, 6, "pacing comment pages")
        emit({"post": args.url, "count": min(len(rows), cap),
              "comments": rows[:cap]})
    finally:
        tab.close()


def cmd_transcript(args):
    b, tab = open_post(args.url)
    tab.close()
    cookies = b.cookies_txt(["instagram.com"])
    try:
        result = transcribe.transcribe(args.url, cookies, args.lang)
    except RuntimeError as e:
        die(str(e))
    finally:
        import os
        os.unlink(cookies)
    transcribe.emit_result(result, args.url)


def cmd_hashtags(args):
    b = browser.attach()
    b.require_login("instagram.com", "Instagram")
    tab = b.page("https://www.instagram.com/", settle=5)
    try:
        import urllib.parse
        # topsearch only populates its hashtags array when the query is
        # #-prefixed; a bare keyword returns users/places instead.
        q = urllib.parse.quote("#" + args.niche.lstrip("#").replace(" ", ""))
        data = backoff_retry(
            lambda: ig_fetch(tab, "/web/search/topsearch/"
                             f"?context=blended&query={q}"),
            "Instagram hashtag search")
        tags = [{
            "hashtag": "#" + h["hashtag"]["name"],
            "posts": h["hashtag"].get("media_count"),
        } for h in (data.get("hashtags") or []) if h.get("hashtag")]
        if not tags:
            die(f"Instagram's search returned no hashtags for '{args.niche}'. "
                "Try a broader niche keyword — or IG changed its search API "
                "(run scripts/doctor.py).")
        emit({"niche": args.niche,
              "note": ("ranked by Instagram's own search; IG has no public "
                       "'trending' list, so this is keyword-seeded discovery"),
              "hashtags": tags})
    finally:
        tab.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("post", help="metadata for one post/reel")
    a.add_argument("url")
    a.set_defaults(func=cmd_post)

    c = sub.add_parser("comments", help="top comments on a post/reel")
    c.add_argument("url")
    c.add_argument("--max-comments", type=int,
                   help=f"default {DEFAULT_MAX_COMMENTS}, ceiling {HARD_MAX_COMMENTS}")
    c.set_defaults(func=cmd_comments)

    t = sub.add_parser("transcript", help="download + transcript for a reel")
    t.add_argument("url")
    t.add_argument("--lang", default="en")
    t.set_defaults(func=cmd_transcript)

    h = sub.add_parser("hashtags", help="hashtag discovery for a niche")
    h.add_argument("niche")
    h.set_defaults(func=cmd_hashtags)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
