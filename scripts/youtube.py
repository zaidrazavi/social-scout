#!/usr/bin/env python3
"""YouTube scraping via yt-dlp — no API key, no login, no account.

Subcommands:
  search <query>       Search YouTube. Options: --limit N, --order relevance|date,
                       --sort-views (rank results by view count = "trending" style)
  video <url_or_id>    Full metadata for one video (views, likes, upload date, tags...)
  transcript <url_or_id>  Plain-text transcript from captions (manual preferred,
                       auto-generated fallback). Options: --lang en, --timestamps
  videos <url>         Recent uploads from a channel or playlist URL. Option: --limit N

All output goes to stdout: JSON for search/video/videos, plain text for transcript.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request

# Windows consoles default to cp1252, which crashes on emoji-laden titles.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SEARCH_FIELDS = ("id", "title", "url", "channel", "uploader", "duration",
                 "view_count", "description", "channel_is_verified")
VIDEO_FIELDS = ("id", "title", "webpage_url", "channel", "channel_url",
                "channel_follower_count", "view_count", "like_count",
                "comment_count", "upload_date", "duration", "duration_string",
                "description", "tags", "categories", "language", "chapters")


def ytdlp_cmd():
    """Return the yt-dlp invocation: the binary if on PATH, else the module."""
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


BOT_CHECK = "Sign in to confirm"


def run_ytdlp(args, timeout=180):
    proc = subprocess.run(ytdlp_cmd() + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0 and BOT_CHECK in (proc.stderr or ""):
        # YouTube intermittently bot-checks the default web client; the android
        # client usually sails through, still with no login or cookies needed.
        proc = subprocess.run(
            ytdlp_cmd() + ["--extractor-args", "youtube:player_client=android"] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or "yt-dlp failed with no error output\n")
        sys.exit(1)
    return proc.stdout


def pick(d, fields):
    return {k: d.get(k) for k in fields if d.get(k) is not None}


def cmd_search(args):
    # ytsearchN = relevance order, ytsearchdateN = newest first.
    prefix = "ytsearchdate" if args.order == "date" else "ytsearch"
    # When ranking by views, over-fetch so the sort has something to work with.
    fetch = min(args.limit * 3, 50) if args.sort_views else args.limit
    out = run_ytdlp(["--flat-playlist", "--dump-json", "--skip-download",
                     f"{prefix}{fetch}:{args.query}"])
    results = [pick(json.loads(line), SEARCH_FIELDS)
               for line in out.splitlines() if line.strip()]
    if args.sort_views:
        results.sort(key=lambda r: r.get("view_count") or 0, reverse=True)
        results = results[:args.limit]
    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_video(args):
    info = json.loads(run_ytdlp(["-J", "--skip-download", args.url]))
    print(json.dumps(pick(info, VIDEO_FIELDS), ensure_ascii=False, indent=2))


def find_track(tracks, lang):
    """Find a caption track for `lang`, tolerating variants like en-US / en-orig."""
    if not tracks:
        return None, None
    if lang in tracks:
        return lang, tracks[lang]
    for code in tracks:
        if code.startswith(lang + "-") or code.split("-")[0] == lang:
            return code, tracks[code]
    return None, None


def fetch_json3(formats):
    """Download a caption track as json3, whichever format entry we were given."""
    url = None
    for f in formats:
        if f.get("ext") == "json3":
            url = f["url"]
            break
    if url is None:
        # timedtext URLs accept a fmt override, so any entry can serve json3
        url = formats[0]["url"]
        url += ("&" if "?" in url else "?") + "fmt=json3"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def cmd_transcript(args):
    info = json.loads(run_ytdlp(["-J", "--skip-download", args.url]))
    lang, formats = find_track(info.get("subtitles"), args.lang)
    source = "manual captions"
    if formats is None:
        lang, formats = find_track(info.get("automatic_captions"), args.lang)
        source = "auto-generated captions"
    if formats is None:
        available = sorted(set(list((info.get("subtitles") or {}).keys()) +
                               list((info.get("automatic_captions") or {}).keys())))
        sys.stderr.write(f"No '{args.lang}' captions for this video. "
                         f"Available languages: {available or 'none'}\n")
        sys.exit(1)

    data = fetch_json3(formats)
    lines = []
    for ev in data.get("events", []):
        if ev.get("aAppend"):  # rolling-window duplicates in auto captions
            continue
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        if args.timestamps:
            t = int(ev.get("tStartMs", 0)) // 1000
            lines.append(f"[{t // 60:02d}:{t % 60:02d}] {text}")
        else:
            lines.append(text)

    print(f"# {info.get('title')}")
    print(f"# video: {info.get('id')} | language: {lang} | source: {source}")
    print()
    print("\n".join(lines))


def cmd_videos(args):
    out = run_ytdlp(["--flat-playlist", "--dump-json", "--skip-download",
                     "--playlist-items", f"1:{args.limit}", args.url])
    results = [pick(json.loads(line), SEARCH_FIELDS)
               for line in out.splitlines() if line.strip()]
    print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="search YouTube")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--order", choices=["relevance", "date"], default="relevance")
    s.add_argument("--sort-views", action="store_true",
                   help="rank results by view count (trending-style)")
    s.set_defaults(func=cmd_search)

    v = sub.add_parser("video", help="metadata for one video")
    v.add_argument("url")
    v.set_defaults(func=cmd_video)

    t = sub.add_parser("transcript", help="plain-text transcript from captions")
    t.add_argument("url")
    t.add_argument("--lang", default="en")
    t.add_argument("--timestamps", action="store_true")
    t.set_defaults(func=cmd_transcript)

    c = sub.add_parser("videos", help="recent uploads from a channel/playlist URL")
    c.add_argument("url")
    c.add_argument("--limit", type=int, default=15)
    c.set_defaults(func=cmd_videos)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
