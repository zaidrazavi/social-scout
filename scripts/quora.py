#!/usr/bin/env python3
"""Quora research — BEST-EFFORT ONLY, weaker than every other platform here.

There is no reliable free backend for Quora (no archive like Reddit's, no
tool like yt-dlp). So this does the honest cheap thing:
  1. web-search DuckDuckGo for Quora question URLs matching a topic,
  2. read each found page's visible text through the same generic web
     reader used for any URL (scripts/webread.py).

You get question text plus whatever answer text is publicly visible — NOT
vote counts, full comment threads, or guaranteed completeness. Quora
login-walls deeper content; expect partial answers sometimes. Do not promise
Reddit-level reliability from this.

CLI:
  python scripts/quora.py search "<topic>" [--limit 3] [--urls-only]
"""

import argparse
import html
import re
import sys
import urllib.parse
import urllib.request

from common import die, emit, jitter_sleep, warn
from webread import BROWSER_UA, read_url

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DDG = "https://html.duckduckgo.com/html/"


def find_quora_urls(topic, limit):
    """DuckDuckGo's plain-HTML endpoint, restricted to quora.com."""
    q = urllib.parse.urlencode({"q": f"site:quora.com {topic}"})
    req = urllib.request.Request(f"{DDG}?{q}", headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        page = r.read().decode("utf-8", errors="replace")
    urls, seen = [], set()
    # DDG result links are /l/?uddg=<encoded-target>&...
    for m in re.finditer(r'href="[^"]*?uddg=([^"&]+)', page):
        url = urllib.parse.unquote(m.group(1))
        url = html.unescape(url).split("#")[0]
        if "quora.com" not in urllib.parse.urlparse(url).netloc:
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("search", help="find and read Quora pages on a topic")
    s.add_argument("topic")
    s.add_argument("--limit", type=int, default=3,
                   help="pages to read (max 5 — this is best-effort tooling)")
    s.add_argument("--urls-only", action="store_true",
                   help="just list found URLs without reading them")
    args = p.parse_args()

    limit = min(max(args.limit, 1), 5)
    try:
        urls = find_quora_urls(args.topic, limit)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        die(f"Quora URL discovery failed (DuckDuckGo unreachable/blocked: {e}). "
            "This backend is best-effort; try again later or search manually.")
    if not urls:
        die(f"No Quora pages found for '{args.topic}'. Try broader phrasing — "
            "discovery here is best-effort web search, not a Quora API.")

    if args.urls_only:
        emit({"note": "best-effort backend", "topic": args.topic, "urls": urls})
        return

    print(f"# Quora (BEST-EFFORT) — topic: {args.topic}")
    print(f"# {len(urls)} page(s) found via web search; visible text only\n")
    failures = 0
    for i, url in enumerate(urls):
        if i:
            jitter_sleep(2, 5, "pacing page reads")
        print(f"\n{'=' * 70}\n## {url}\n")
        try:
            text, engine = read_url(url, max_chars=12000)
            print(f"(via {engine})\n")
            print(text)
        except RuntimeError as e:
            failures += 1
            warn(f"could not read {url}: {e}")
            print(f"[unreadable: {e}]")
    if failures == len(urls):
        die("Found Quora URLs but could not read any of them — "
            "Quora may be blocking readers right now (best-effort backend).")


if __name__ == "__main__":
    main()
