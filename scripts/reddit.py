#!/usr/bin/env python3
"""Reddit scraping via free public archive APIs — no OAuth, no API key, no account.

PRIMARY:  Arctic Shift  (https://arctic-shift.photon-reddit.com/api)
FALLBACK: Pullpush      (https://api.pullpush.io/reddit)

Every request tries Arctic Shift first; on any failure the same request is
retried against Pullpush. Each result reports which API answered ("via").
Exception: a keyword search across ALL of Reddit goes straight to Pullpush,
because Arctic Shift requires a subreddit or author filter for text search.

Subcommands:
  search <query>       Keyword search for posts. Options: --subreddit X[,Y,...],
                       --limit N, --after DATE, --before DATE, --min-score N, --full
  subreddit <name>     Newest posts from one subreddit (no keyword).
                       Options: --limit, --after, --before, --min-score, --full
  comments             Comments, by post or by keyword. Options: --post <id_or_url>,
                       --query TEXT, --subreddit X, --limit, --after, --before, --full

DATE accepts YYYY-MM-DD or a relative "Nd" (N days ago), e.g. --after 30d.
Text bodies are truncated to 1500 chars unless --full is given.
Note: scores/comment counts are snapshots from when the archive crawled the
post, so very recent posts may show lower numbers than live Reddit.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
PULLPUSH = "https://api.pullpush.io/reddit"
UA = "social-scout-skill/1.0 (free research tool)"
TRUNCATE = 1500


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def extract_items(payload):
    """Both APIs wrap results as {"data": [...]}; tolerate a bare array too."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def parse_date(value):
    """YYYY-MM-DD or 'Nd' (N days ago) -> unix seconds."""
    if value is None:
        return None
    m = re.fullmatch(r"(\d+)d", value.strip())
    if m:
        return int(time.time()) - int(m.group(1)) * 86400
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        sys.stderr.write(f"Bad date '{value}': use YYYY-MM-DD or Nd (e.g. 30d)\n")
        sys.exit(2)


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip(text, full):
    text = text or ""
    if full or len(text) <= TRUNCATE:
        return text
    return text[:TRUNCATE] + f"... [truncated, {len(text)} chars total; rerun with --full]"


def norm_post(p, full):
    return {
        "id": p.get("id"),
        "title": p.get("title") or "",
        "author": p.get("author"),
        "subreddit": p.get("subreddit"),
        "url": ("https://www.reddit.com" + p["permalink"]) if p.get("permalink")
               else p.get("full_link") or p.get("url") or "",
        "score": p.get("ups") if p.get("ups") is not None else p.get("score") or 0,
        "num_comments": p.get("num_comments") or 0,
        "created": iso(p.get("created_utc")),
        "is_self": p.get("is_self"),
        "selftext": clip(p.get("selftext"), full),
    }


def norm_comment(c, full):
    return {
        "id": c.get("id"),
        "author": c.get("author"),
        "subreddit": c.get("subreddit"),
        "score": c.get("ups") if c.get("ups") is not None else c.get("score") or 0,
        "created": iso(c.get("created_utc")),
        "link_id": c.get("link_id"),
        "url": ("https://www.reddit.com" + c["permalink"]) if c.get("permalink") else None,
        "body": clip(c.get("body"), full),
    }


def qs(params):
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def fetch_arctic(url):
    """Arctic Shift returns HTTP 422 ("Timeout. Maybe slow down a bit") when a
    query is expensive or cold; the retry usually succeeds because the server
    has warmed its cache by then. So wait briefly and try again before giving
    up on the primary API."""
    for attempt in range(3):
        try:
            return extract_items(http_json(url))
        except urllib.error.HTTPError as e:
            if e.code != 422 or attempt == 2:
                raise
            time.sleep(5)


def with_fallback(arctic_url, pullpush_url):
    """Try Arctic Shift (with 422 retries), then Pullpush. Raise only if both fail."""
    primary_error = None
    if arctic_url:
        try:
            return fetch_arctic(arctic_url), "arctic_shift"
        except Exception as e:
            primary_error = str(e)
    try:
        return extract_items(http_json(pullpush_url)), "pullpush"
    except Exception as e:
        parts = []
        if primary_error:
            parts.append(f"Arctic Shift: {primary_error}")
        parts.append(f"Pullpush: {e}")
        raise SystemExit("Both Reddit archive APIs failed. " + " | ".join(parts))


def usable_self_post(r):
    """A text post with a real body — not a link post, not removed/deleted."""
    body = r.get("selftext") or ""
    return (r.get("is_self") is not False and len(body) >= 100
            and body not in ("[removed]", "[deleted]"))


def emit(items, via, kind, full, min_score=None, self_only=False):
    norm = norm_post if kind == "post" else norm_comment
    rows = [norm(item, full) for item in items]
    if self_only:
        rows = [r for r in rows if usable_self_post(r)]
    if min_score:
        rows = [r for r in rows if (r["score"] or 0) >= min_score]
    print(json.dumps({"via": via, "count": len(rows), "results": rows},
                     ensure_ascii=False, indent=2))


def cmd_search(args):
    after, before = parse_date(args.after), parse_date(args.before)
    limit = min(args.limit, 100)
    if args.subreddit:
        all_items, via_used = [], set()
        for sub in [s.strip() for s in args.subreddit.split(",") if s.strip()]:
            a_url = f"{ARCTIC}/posts/search?" + qs({
                "subreddit": sub, "query": args.query, "limit": limit,
                "sort": "desc", "after": after, "before": before})
            p_url = f"{PULLPUSH}/search/submission/?" + qs({
                "q": args.query, "subreddit": sub, "size": limit,
                "after": after, "before": before})
            items, via = with_fallback(a_url, p_url)
            all_items.extend(items)
            via_used.add(via)
        emit(all_items, "+".join(sorted(via_used)), "post", args.full, args.min_score, args.self_only)
    else:
        # Global keyword search: Arctic Shift refuses text queries without a
        # subreddit/author filter, so this is Pullpush-only (no fallback).
        p_url = f"{PULLPUSH}/search/submission/?" + qs({
            "q": args.query, "size": limit, "after": after, "before": before})
        try:
            items = extract_items(http_json(p_url))
        except Exception as e:
            raise SystemExit(
                f"Global search needs Pullpush, which failed ({e}). "
                "Retry with --subreddit <name> to use Arctic Shift instead.")
        emit(items, "pullpush", "post", args.full, args.min_score, args.self_only)


def cmd_subreddit(args):
    after, before = parse_date(args.after), parse_date(args.before)
    limit = min(args.limit, 100)
    a_url = f"{ARCTIC}/posts/search?" + qs({
        "subreddit": args.name, "limit": limit, "sort": "desc",
        "after": after, "before": before})
    p_url = f"{PULLPUSH}/search/submission/?" + qs({
        "subreddit": args.name, "size": limit, "after": after, "before": before})
    items, via = with_fallback(a_url, p_url)
    emit(items, via, "post", args.full, args.min_score, args.self_only)


def post_id_from(value):
    """Accept a bare id, t3_ id, or a full reddit URL."""
    m = re.search(r"/comments/([a-z0-9]+)", value)
    if m:
        return m.group(1)
    return value.removeprefix("t3_")


def cmd_comments(args):
    if not args.post and not args.query:
        sys.exit("comments needs --post <id_or_url> or --query <text>")
    after, before = parse_date(args.after), parse_date(args.before)
    limit = min(args.limit, 100)
    link_id = f"t3_{post_id_from(args.post)}" if args.post else None
    a_url = f"{ARCTIC}/comments/search?" + qs({
        "link_id": link_id, "body": args.query, "subreddit": args.subreddit,
        "limit": limit, "sort": "desc", "after": after, "before": before})
    p_url = f"{PULLPUSH}/search/comment/?" + qs({
        "link_id": link_id, "q": args.query, "subreddit": args.subreddit,
        "size": limit, "after": after, "before": before})
    items, via = with_fallback(a_url, p_url)
    emit(items, via, "comment", args.full)


def add_common(sp, min_score=True):
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--after", help="YYYY-MM-DD or Nd (days ago)")
    sp.add_argument("--before", help="YYYY-MM-DD or Nd (days ago)")
    sp.add_argument("--full", action="store_true", help="don't truncate text bodies")
    if min_score:
        sp.add_argument("--min-score", type=int)
        sp.add_argument("--self-only", action="store_true",
                        help="keep only text posts with a real body (drops link "
                             "posts and [removed]/[deleted])")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="keyword search for posts")
    s.add_argument("query")
    s.add_argument("--subreddit", help="one or more subreddits, comma-separated")
    add_common(s)
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("subreddit", help="newest posts from one subreddit")
    r.add_argument("name")
    add_common(r)
    r.set_defaults(func=cmd_subreddit)

    c = sub.add_parser("comments", help="comments by post or keyword")
    c.add_argument("--post", help="post id, t3_ id, or full reddit URL")
    c.add_argument("--query", help="keyword to search in comment bodies")
    c.add_argument("--subreddit")
    add_common(c, min_score=False)
    c.set_defaults(func=cmd_comments)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
