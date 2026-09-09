#!/usr/bin/env python3
"""Quora research — BEST-EFFORT ONLY, weaker than every other platform here.

There is no reliable free backend for Quora (no archive like Reddit's, no
tool like yt-dlp). So this does the honest cheap thing:
  1. find Quora question URLs by web search — DuckDuckGo first, then Google
     as a fallback (either can be blocked from a given IP),
  2. read each found page's visible text. Reading tries the generic web
     reader (scripts/webread.py: Jina → direct fetch) and, if that fails and
     the scout browser happens to be running, one more tier: load the page in
     that real Chrome, which Quora can't bot-block. The scout browser is a
     bonus, never a requirement — Quora needs no login there.

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
import urllib.error
import urllib.parse
import urllib.request

from common import die, emit, jitter_sleep, warn
from webread import BROWSER_UA, read_url

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DDG = "https://html.duckduckgo.com/html/"
GOOGLE = "https://www.google.com/search"


def is_question_url(url):
    """Keep only real Quora question pages — drop the homepage, tracking
    pixels/redirects (too short), and anything with no path after the domain."""
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    if "quora.com" not in host:
        return False
    if len(url) < 30:
        return False
    if not p.path.strip("/"):  # homepage, e.g. https://www.quora.com/
        return False
    # tracking pixels / internal ad endpoints, e.g. q.quora.com/_/ad/…/pixel
    if host.startswith("q.quora.com") or p.path.startswith("/_/") or "/pixel" in p.path:
        return False
    return True


def _clean_candidates(raw_urls, limit):
    """Unescape, strip fragments, filter to real question pages, dedupe."""
    urls, seen = [], set()
    for u in raw_urls:
        u = html.unescape(u).split("#")[0]
        if not is_question_url(u) or u in seen:
            continue
        seen.add(u)
        urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def ddg_search(topic, limit):
    """DuckDuckGo's plain-HTML endpoint, restricted to quora.com. Result links
    are /l/?uddg=<encoded-target>&..."""
    q = urllib.parse.urlencode({"q": f"site:quora.com {topic}"})
    req = urllib.request.Request(f"{DDG}?{q}", headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        page = r.read().decode("utf-8", errors="replace")
    raw = [urllib.parse.unquote(m.group(1))
           for m in re.finditer(r'href="[^"]*?uddg=([^"&]+)', page)]
    return _clean_candidates(raw, limit)


def google_search(topic, limit):
    """Google's HTML search, restricted to quora.com. No-JS Google wraps each
    result as /url?q=<target>&...; some builds link the target directly."""
    q = urllib.parse.urlencode({"q": f"site:quora.com {topic}",
                                "num": max(limit * 2, 10)})
    req = urllib.request.Request(
        f"{GOOGLE}?{q}",
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=45) as r:
        page = r.read().decode("utf-8", errors="replace")
    raw = [urllib.parse.unquote(m.group(1))
           for m in re.finditer(r'/url\?q=([^&"]+)', page)]
    raw += [m.group(1)
            for m in re.finditer(r'href="(https?://[^"]*quora\.com[^"]*)"', page)]
    return _clean_candidates(raw, limit)


def find_quora_urls_browser(topic, limit):
    """Optional last-resort discovery: search Google in the scout browser (a
    real Chrome isn't IP-blocked the way direct fetches are). Returns [] if the
    scout browser isn't running or anything fails — never required."""
    try:
        import browser
        if not browser.is_running():
            return []
        q = urllib.parse.quote(f"site:quora.com {topic}")
        tab = browser.attach().page(
            f"{GOOGLE}?q={q}&num={max(limit * 2, 10)}", settle=5)
        try:
            hrefs = tab.eval(
                "Array.from(document.querySelectorAll('a[href]')).map(a => a.href)") or []
        finally:
            tab.close()
    except Exception:
        return []
    # Google links are either direct (https://www.quora.com/…) or /url?q=<target>;
    # unwrap the redirect, then _clean_candidates keeps only real quora questions.
    raw = []
    for h in hrefs:
        target = h
        if "/url?" in h:
            qd = urllib.parse.parse_qs(urllib.parse.urlparse(h).query)
            target = (qd.get("q") or qd.get("url") or [""])[0]
        raw.append(target)
    return _clean_candidates(raw, limit)


def find_quora_urls(topic, limit):
    """Try DuckDuckGo → Google (direct) → Google via the scout browser. Each
    tier can be IP-blocked; the browser tier is optional (skipped if the scout
    browser isn't running). Raise only if every tier fails or finds nothing."""
    for engine in (ddg_search, google_search):
        try:
            urls = engine(topic, limit)
            if urls:
                return urls
        except Exception:
            pass
    urls = find_quora_urls_browser(topic, limit)  # optional bonus tier
    if urls:
        return urls
    raise RuntimeError("discovery blocked")


def read_via_browser(url):
    """Bonus last tier: read the page in the already-running scout browser (a
    real Chrome), which Quora can't bot-block. Returns text, or None if the
    scout browser isn't running or anything goes wrong — this must never be a
    hard requirement for Quora. No Quora login is needed."""
    try:
        import browser
        if not browser.is_running():
            return None
        b = browser.attach()
        tab = b.page(url, settle=5)
        try:
            text = (tab.eval("document.body.innerText") or "").strip()
        finally:
            tab.close()
        if not text:
            return None
        if len(text) > 12000:
            text = text[:12000] + "\n... [truncated]"
        return text
    except Exception:
        return None


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
    except (RuntimeError, urllib.error.URLError, OSError, TimeoutError):
        die("Quora search is temporarily unavailable — this is normal, Quora "
            "is a best-effort source that works intermittently. Try again in a "
            "few minutes.")
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
        text = engine = None
        reader_error = None
        try:
            text, engine = read_url(url, max_chars=12000)
        except RuntimeError as e:
            reader_error = e
            # Last resort: read it in the real scout browser, if it's running.
            btext = read_via_browser(url)
            if btext:
                text, engine = btext, "scout-browser"
        if text:
            print(f"(via {engine})\n")
            print(text)
        else:
            failures += 1
            warn(f"could not read {url}: {reader_error}")
            print(f"[unreadable: {reader_error}]")
    if failures == len(urls):
        die("Quora search is temporarily unavailable — this is normal, Quora "
            "is a best-effort source that works intermittently. Try again in a "
            "few minutes. (Tip: starting the scout browser lets a real Chrome "
            "read pages the direct fetch can't.)")


if __name__ == "__main__":
    main()
