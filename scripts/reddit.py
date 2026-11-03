#!/usr/bin/env python3
"""Reddit scraping via free public sources — no OAuth, no API key, no account.

Sources, in the order they're tried:
  stealth browser  Reddit's own search page rendered headless
                   (scripts/obscura.py, if installed) — keyword search only
  Arctic Shift     (https://arctic-shift.photon-reddit.com/api)
  Reddit .json     Reddit's own public .json (https://www.reddit.com/....json)
  scout browser    same-origin .json fetch in real Chrome, if it's running
  Pullpush         (https://api.pullpush.io/reddit) — LAST: often down

Global keyword search tries the stealth browser first (Arctic Shift can't do
it). Subreddit-scoped search tries Arctic Shift first — it returns full post
bodies, which the search page doesn't show — then the stealth browser.
Subreddit feeds and comments skip the stealth browser: Reddit blocks it on
those pages. Each result reports which source answered ("via").

The stealth tier reads Reddit's live search results (exact current score and
comment count, exact timestamp) but no post body, so it's skipped for
--self-only and --before (search can't reach back to a cut-off date). It
returns the most RELEVANT posts (within the --after window when given).

The Reddit .json tier is deliberately a lightweight last resort, NOT a
primary path: Reddit rate-limits and can block unauthenticated .json traffic
if it's hit too often, so this tier jitters before every call and backs off
exponentially on 429/5xx (same pattern as the Instagram/TikTok backends).
Keep usage occasional. Its one upside: unlike the archives (which serve
crawl-time snapshots), .json returns Reddit's LIVE scores/counts.
Note: .json can't honor precise --after/--before server-side, so date bounds
are applied client-side to whatever it returns.

Exception: a keyword search across ALL of Reddit skips Arctic Shift (which
requires a subreddit/author filter for text search).

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

from common import backoff_retry, jitter_sleep

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
PULLPUSH = "https://api.pullpush.io/reddit"
REDDIT = "https://www.reddit.com"
UA = "social-scout-skill/1.0 (free research tool)"
# Reddit's own .json 403s the descriptive UA above; it's far more permissive to
# a normal browser UA (from a normal residential IP). Datacenter/VPN IPs may
# still be blocked outright — that's the documented last-resort limitation.
REDDIT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TRUNCATE = 1500


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def reddit_json_get(url, timeout=30):
    """GET Reddit's public .json with a browser UA. When Reddit is blocking or
    rate-limiting the request it answers with an HTML consent/"Welcome to
    Reddit" gate (HTTP 200) instead of JSON, or a 403 — surface either as a
    clear, specific error rather than a cryptic JSON-decode failure."""
    req = urllib.request.Request(
        url, headers={"User-Agent": REDDIT_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read().decode("utf-8", "replace")
    if "json" not in ctype.lower() and body.lstrip()[:1] not in ("{", "["):
        raise RuntimeError(
            "Reddit served a block/consent page instead of JSON — its public "
            ".json is rate-limiting or blocking this network (the documented "
            "lightweight-fallback limitation; use a residential IP or wait)")
    return json.loads(body)


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


def extract_reddit_json(payload):
    """Reddit's own .json wraps items as {"data": {"children": [{"data": …}]}}.
    Skip "more" placeholders (kind 'more') that carry no real content."""
    data = payload.get("data") if isinstance(payload, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        return []
    return [c.get("data", {}) for c in children
            if isinstance(c, dict) and c.get("kind") != "more"]


def within_range(item, date_range):
    """Reddit .json can't filter by precise unix range server-side, so honor
    --after/--before client-side against each item's created_utc."""
    if not date_range:
        return True
    after, before = date_range
    t = item.get("created_utc")
    if after and (t is None or t < after):
        return False
    if before and (t is None or t >= before):
        return False
    return True


def fetch_reddit_json(url, date_range=None):
    """Third-tier fallback: Reddit's public .json. Lightweight only — Reddit
    rate-limits/blocks unauthenticated hits, so jitter before the call and
    back off exponentially on 429/5xx. Not a primary path."""
    jitter_sleep(2, 5, "pacing Reddit public .json (lightweight fallback)")
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}raw_json=1"  # keep &<>" literal instead of HTML-escaped
    items = backoff_retry(lambda: extract_reddit_json(reddit_json_get(full)),
                          "Reddit public .json", attempts=3, base=8)
    return [it for it in items if within_range(it, date_range)]


def fetch_reddit_json_comments(post_id, limit, date_range=None):
    """Reddit's comment .json returns [post_listing, comment_listing]; the
    comments live in the second element. Same jitter/backoff as above."""
    jitter_sleep(2, 5, "pacing Reddit public .json (lightweight fallback)")
    url = f"{REDDIT}/comments/{post_id}.json?limit={limit}&sort=top&raw_json=1"

    def go():
        payload = reddit_json_get(url)
        if not (isinstance(payload, list) and len(payload) >= 2):
            return []
        return extract_reddit_json(payload[1])

    items = backoff_retry(go, "Reddit public .json comments", attempts=3, base=8)
    return [it for it in items if within_range(it, date_range)]


def fetch_reddit_browser(json_url, date_range=None, kind="posts"):
    """Optional last-resort tier: open reddit.com in the scout browser and do a
    same-origin fetch to the .json endpoint. A real Chrome isn't IP-blocked the
    way urllib is, and Reddit serves JSON to a same-origin browser fetch.
    Returns items, or None if the scout browser isn't running or anything fails
    — never a hard requirement."""
    try:
        import browser
        if not browser.is_running():
            return None
        sep = "&" if "?" in json_url else "?"
        target = f"{json_url}{sep}raw_json=1"
        tab = browser.Browser().page("https://www.reddit.com/", settle=4)
        try:
            js = ("fetch(" + json.dumps(target) + ", {credentials:'include'})"
                  ".then(r => r.ok ? r.json() : null)")
            payload = tab.eval(js, timeout=30)
        finally:
            tab.close()
    except Exception:
        return None
    if payload is None:
        return None
    if kind == "comments":
        if not (isinstance(payload, list) and len(payload) >= 2):
            return None
        items = extract_reddit_json(payload[1])
    else:
        items = extract_reddit_json(payload)
    return [it for it in items if within_range(it, date_range)]


# Reddit's search page (rendered by the stealth browser) shows 7 results per
# screen; further results arrive as "load more" fragments — the same request
# the page itself makes when a person scrolls — fetched here with a human-ish
# pause between pages. Each result card carries the post's id/title/author/
# subreddit in a tracking-context attribute, and exact counts/timestamps in
# faceplate-number / faceplate-timeago attributes.
SEARCH_PAGE_JS = r"""
(async () => {
  const MAX = __MAX__, PAGES = __PAGES__;
  const read = root => Array.from(root.querySelectorAll('[data-testid="sdui-post-unit"]')).map(u => {
    const a = u.querySelector('a[data-testid="post-title-text"]') || u.querySelector('a[data-testid="post-title"]');
    if (!a) return null;
    let ctx = {};
    for (const tr of u.querySelectorAll('[data-faceplate-tracking-context]')) {
      try { const c = JSON.parse(tr.getAttribute('data-faceplate-tracking-context'));
            if (c.post) { ctx = c; break; } } catch (e) {}
    }
    const nums = Array.from(u.querySelectorAll('[data-testid="search-counter-row"] faceplate-number'))
      .map(n => parseInt(n.getAttribute('number'), 10));
    const ta = u.querySelector('faceplate-timeago');
    return {id: ((ctx.post || {}).id || '').replace(/^t3_/, '') || null,
            title: (ctx.post || {}).title || a.textContent.trim(),
            author: (ctx.profile || {}).name || null,
            subreddit: (ctx.subreddit || {}).name || null,
            permalink: a.getAttribute('href'),
            score: Number.isFinite(nums[0]) ? nums[0] : null,
            num_comments: Number.isFinite(nums[1]) ? nums[1] : null,
            created: ta ? ta.getAttribute('ts') : null};
  }).filter(Boolean);
  const nextSrc = root => {
    const p = root.querySelector('faceplate-partial[src*="/svc/shreddit/search/"][src*="cursor="]');
    return p ? p.getAttribute('src') : null; };
  const seen = new Set(), out = [];
  const add = rows => { for (const r of rows) { const k = r.id || r.permalink;
                        if (!seen.has(k)) { seen.add(k); out.push(r); } } };
  add(read(document));
  const empty = out.length === 0 &&
    /couldn.t find any results|no results/i.test((document.body || {}).innerText || '');
  let src = nextSrc(document), pages = 1;
  while (src && out.length < MAX && pages < PAGES) {
    await new Promise(r => setTimeout(r, 1500 + Math.random() * 1500));
    const html = await fetch(src, {credentials: 'include'}).then(r => r.ok ? r.text() : '');
    if (!html) break;
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const before = out.length; add(read(doc)); pages++;
    if (out.length === before) break;
    src = nextSrc(doc);
  }
  return {rows: out.slice(0, MAX), empty: empty};
})()
"""


def _ts(value):
    """'2026-11-03T05:34:57.033000+0000' -> unix seconds."""
    try:
        return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp())
    except (TypeError, ValueError):
        try:
            return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").timestamp())
        except (TypeError, ValueError):
            return None


def fetch_reddit_stealth(query, subreddit, limit, date_range=None):
    """Stealth-browser tier: Reddit's own search page, rendered headless.
    Returns items shaped like Reddit .json posts, [] when Reddit says there
    are no results, or None (not installed / blocked / nothing parsed) so
    the next tier runs."""
    try:
        import obscura
        if not obscura.is_installed():
            return None
        # Always relevance: Reddit's newest-first search pulls in loose
        # matches (tested: 1/8 on-topic vs 8/8). A date window uses Reddit's
        # own time filter — the smallest that covers it — and within_range
        # trims to the exact --after date below.
        params = {"q": query, "type": "posts", "sort": "relevance"}
        after = date_range[0] if date_range else None
        if after:
            days = (time.time() - after) / 86400
            params["t"] = next((t for limit_days, t in ((1, "day"), (7, "week"),
                                (31, "month"), (366, "year")) if days <= limit_days), "all")
        if subreddit:
            params["restrict_sr"] = 1
            url = (f"{REDDIT}/r/{urllib.parse.quote(subreddit)}/search/?"
                   + urllib.parse.urlencode(params))
        else:
            url = f"{REDDIT}/search/?" + urllib.parse.urlencode(params)
        pages = min(15, limit // 7 + 2)
        with obscura.Session() as s:
            tab = s.page(url, settle=3)
            try:
                body = tab.eval("(document.body && document.body.innerText) || ''") or ""
                if obscura.looks_blocked(body):
                    return None
                res = tab.eval(SEARCH_PAGE_JS.replace("__MAX__", str(limit))
                               .replace("__PAGES__", str(pages)), timeout=180) or {}
            finally:
                tab.close()
    except Exception:
        return None
    rows = res.get("rows") or []
    if not rows:
        return [] if res.get("empty") else None
    items = [{"id": r.get("id"), "title": r.get("title"), "author": r.get("author"),
              "subreddit": r.get("subreddit"), "permalink": r.get("permalink"),
              "ups": r.get("score"), "num_comments": r.get("num_comments"),
              "created_utc": _ts(r.get("created")), "is_self": None, "selftext": ""}
             for r in rows]
    return [it for it in items if within_range(it, date_range)]


def stealth_ok(args):
    """The search page has no post bodies and lists newest first."""
    return not args.self_only and not args.before


# Shown to the user when every Reddit source is unreachable — deliberately
# free of HTTP codes and backend names (those only scare non-technical users).
REDDIT_DOWN_MSG = (
    "Reddit is temporarily unreachable from this network. This usually "
    "resolves within 15–60 minutes. Try again later, or try from a different "
    "network (home WiFi usually works better than office/VPN).")


def with_fallback(arctic_url, pullpush_url, reddit_json_fn=None, browser_fn=None,
                  stealth_fn=None, stealth_first=False):
    """Try, in order: [stealth browser if stealth_first] → Arctic Shift (with
    422 retries) → [stealth browser] → Reddit's public .json → (optional) the
    scout browser → Pullpush, last because it's frequently down. A tier that
    returns None is skipped. Raise a friendly message only if every
    available source fails."""
    tiers = []
    if stealth_fn is not None and stealth_first:
        tiers.append(("stealth_browser", stealth_fn))
    if arctic_url:
        tiers.append(("arctic_shift", lambda: fetch_arctic(arctic_url)))
    if stealth_fn is not None and not stealth_first:
        tiers.append(("stealth_browser", stealth_fn))
    if reddit_json_fn is not None:
        tiers.append(("reddit_json", reddit_json_fn))
    if browser_fn is not None:
        tiers.append(("scout_browser", browser_fn))
    tiers.append(("pullpush", lambda: extract_items(http_json(pullpush_url))))
    for via, fn in tiers:
        try:
            items = fn()
        except Exception:
            continue
        if items is not None:
            return items, via
    raise SystemExit(REDDIT_DOWN_MSG)


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
            rj_url = f"{REDDIT}/r/{urllib.parse.quote(sub)}/search.json?" + qs({
                "q": args.query, "restrict_sr": 1, "sort": "new", "limit": limit})
            rj = lambda u=rj_url: fetch_reddit_json(u, (after, before))
            bj = lambda u=rj_url: fetch_reddit_browser(u, (after, before), "posts")
            sj = (lambda s=sub: fetch_reddit_stealth(args.query, s, limit, (after, before))
                  ) if stealth_ok(args) else None
            items, via = with_fallback(a_url, p_url, rj, bj, sj)
            all_items.extend(items)
            via_used.add(via)
        emit(all_items, "+".join(sorted(via_used)), "post", args.full, args.min_score, args.self_only)
    else:
        # Global keyword search: Arctic Shift refuses text queries without a
        # subreddit/author filter, so the stealth browser (Reddit's own search
        # page) leads, then Reddit .json, the scout browser, and Pullpush.
        p_url = f"{PULLPUSH}/search/submission/?" + qs({
            "q": args.query, "size": limit, "after": after, "before": before})
        rj_url = f"{REDDIT}/search.json?" + qs({
            "q": args.query, "sort": "new", "limit": limit})
        rj = lambda u=rj_url: fetch_reddit_json(u, (after, before))
        bj = lambda u=rj_url: fetch_reddit_browser(u, (after, before), "posts")
        sj = (lambda: fetch_reddit_stealth(args.query, None, limit, (after, before))
              ) if stealth_ok(args) else None
        items, via = with_fallback(None, p_url, rj, bj, sj, stealth_first=True)
        emit(items, via, "post", args.full, args.min_score, args.self_only)


def cmd_subreddit(args):
    after, before = parse_date(args.after), parse_date(args.before)
    limit = min(args.limit, 100)
    a_url = f"{ARCTIC}/posts/search?" + qs({
        "subreddit": args.name, "limit": limit, "sort": "desc",
        "after": after, "before": before})
    p_url = f"{PULLPUSH}/search/submission/?" + qs({
        "subreddit": args.name, "size": limit, "after": after, "before": before})
    rj_url = f"{REDDIT}/r/{urllib.parse.quote(args.name)}/new.json?limit={limit}"
    rj = lambda u=rj_url: fetch_reddit_json(u, (after, before))
    bj = lambda u=rj_url: fetch_reddit_browser(u, (after, before), "posts")
    items, via = with_fallback(a_url, p_url, rj, bj)
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
    post_id = post_id_from(args.post) if args.post else None
    link_id = f"t3_{post_id}" if post_id else None
    a_url = f"{ARCTIC}/comments/search?" + qs({
        "link_id": link_id, "body": args.query, "subreddit": args.subreddit,
        "limit": limit, "sort": "desc", "after": after, "before": before})
    p_url = f"{PULLPUSH}/search/comment/?" + qs({
        "link_id": link_id, "q": args.query, "subreddit": args.subreddit,
        "size": limit, "after": after, "before": before})
    if post_id:
        # A specific post's comments come straight from that post's .json.
        c_url = f"{REDDIT}/comments/{post_id}.json?limit={limit}&sort=top"
        rj = lambda: fetch_reddit_json_comments(post_id, limit, (after, before))
        bj = lambda u=c_url: fetch_reddit_browser(u, (after, before), "comments")
    else:
        # Keyword comment search: Reddit's search.json with type=comment,
        # scoped to a subreddit when one was given.
        base = (f"{REDDIT}/r/{urllib.parse.quote(args.subreddit)}/search.json"
                if args.subreddit else f"{REDDIT}/search.json")
        params = {"q": args.query, "type": "comment", "sort": "new", "limit": limit}
        if args.subreddit:
            params["restrict_sr"] = 1
        c_url = base + "?" + qs(params)
        rj = lambda u=c_url: fetch_reddit_json(u, (after, before))
        bj = lambda u=c_url: fetch_reddit_browser(u, (after, before), "posts")
    items, via = with_fallback(a_url, p_url, rj, bj)
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
