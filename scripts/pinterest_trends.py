#!/usr/bin/env python3
"""Pinterest Trends — visual/aesthetic trend signals for a niche.

Pinterest Trends (https://trends.pinterest.com) is JavaScript-rendered and
blocks non-browser requests, so this uses the scout browser (auto-launched, no
Pinterest login needed for the public view). Parsing is resilient — it reads
the rendered text (like tiktok.py's Creative Center scraper) rather than
brittle CSS selectors, since Pinterest redesigns often.

⚠️ BEST-EFFORT / directional. Pinterest gates true per-keyword Trends behind a
login, so the public page shows a regional "Trends in the spotlight" ranked
list (month-over-month growth). `trending` returns that list; `search` returns
it filtered to your query where possible. Numbers are relative signals, not
exact volumes.

Subcommands:
  search <query> [--geo US] [--timeframe last_12_months]
  trending [--geo US] [--category NAME]
"""

import argparse
import re
import sys
import urllib.parse

from common import die, emit, jitter_sleep, warn

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MOM_RE = re.compile(r"([\d,]+%)\s*MoM(?:\s*[•·-]\s*Popular in (.+))?", re.I)


def open_scout():
    try:
        import browser
    except Exception as e:
        die(f"Could not load the scout browser module: {e}")
    if not browser.is_running():
        warn("scout browser not running — launching it (no Pinterest login "
             "needed for public Trends)...")
        browser.launch()
    return browser.attach()


def load_trends(geo, query=None):
    params = {"country": geo}
    if query:
        params["query"] = query
    url = "https://trends.pinterest.com/?" + urllib.parse.urlencode(params)
    tab = open_scout().page(url, settle=9)
    try:
        landed = (tab.eval("location.href") or "").lower()
        if "login" in landed or "/business" in landed:
            die("Pinterest bounced to a login page. Open the scout browser "
                "window, load trends.pinterest.com once, then retry.")
        tab.eval("window.scrollTo(0, document.body.scrollHeight)")
        jitter_sleep(2, 4, "loading Pinterest Trends")
        title = tab.eval("document.title") or ""
        body = tab.eval("document.body.innerText") or ""
    finally:
        tab.close()
    if "Trends" not in title and "Trend" not in body[:200]:
        die(f"Pinterest Trends didn't load as expected (title '{title[:50]}'). "
            "It may be blocking or redesigned — open the scout browser to check.")
    return body


def parse_spotlight(body):
    """The spotlight renders as repeating triples:
        <rank> / <term> / "<X>% MoM • Popular in <categories>"
    Parse the rendered text into structured trend entries (resilient to
    cosmetic redesigns)."""
    lines = [l.strip() for l in body.split("\n")]
    out, seen = [], set()
    for i, l in enumerate(lines):
        if not (l.isdigit() and 1 <= int(l) <= 50 and i + 2 < len(lines)):
            continue
        term = lines[i + 1]
        m = MOM_RE.match(lines[i + 2])
        if not term or not m or term in seen:
            continue
        seen.add(term)
        out.append({
            "rank": int(l),
            "term": term,
            "growth": f"{m.group(1)} MoM",
            "trend": "rising",  # spotlight entries are all rising by definition
            "categories": [c.strip() for c in re.split(r"\s+and\s+|,",
                           m.group(2))] if m.group(2) else None,
        })
    return out


def cmd_trending(args):
    body = load_trends(args.geo)
    trends = parse_spotlight(body)
    if args.category:
        want = args.category.lower()
        filtered = [t for t in trends
                    if t.get("categories") and any(want in c.lower()
                                                    for c in t["categories"])]
        trends = filtered or trends
    if not trends:
        die("Couldn't parse Pinterest's trending spotlight — the page may have "
            "changed. Open the scout browser window to check what it shows.")
    emit({
        "geo": args.geo,
        "category_filter": args.category,
        "note": ("Pinterest 'Trends in the spotlight' (public, month-over-month "
                 "growth) — relative directional signals, not exact volumes"),
        "count": len(trends),
        "trending": trends,
    })


def pinterest_session():
    """Scout Browser if running AND Pinterest is logged in, else None. Never
    launches just to check — login is optional (it upgrades keyword data)."""
    try:
        import browser
        if not browser.is_running():
            return None
        b = browser.attach()
        return b if b.pinterest_logged_in() else None
    except Exception:
        return None


def keyword_trends(b, query, geo):
    """Logged-in tier: Pinterest's per-keyword trends page. Returns a list of
    related trend terms (with growth where shown), or None if nothing parsed.
    Resilient innerText parsing — the numeric interest chart is an SVG and not
    reliably text-extractable, so we surface related terms, not the graph."""
    url = "https://trends.pinterest.com/search?" + urllib.parse.urlencode(
        {"q": query, "geo": geo})
    tab = b.page(url, settle=9)
    try:
        landed = (tab.eval("location.href") or "").lower()
        if "login" in landed:
            return None
        tab.eval("window.scrollTo(0, document.body.scrollHeight)")
        jitter_sleep(2, 4, "loading Pinterest keyword trends")
        body = tab.eval("document.body.innerText") or ""
    finally:
        tab.close()
    entries = parse_spotlight(body)  # keyword page also uses "% MoM • Popular in"
    related = [{"term": e["term"], "growth": e["growth"],
                "categories": e["categories"]} for e in entries
               if e["term"].lower() != query.lower()]
    return related or None


def cmd_search(args):
    # Tier 0 (best): logged-in per-keyword trends, if a Pinterest account is
    # logged into the scout browser. Optional — falls back to public spotlight.
    session = pinterest_session()
    if session is not None:
        try:
            related = keyword_trends(session, args.query, args.geo)
        except Exception as e:
            warn(f"logged-in keyword trends failed ({e}); using public spotlight")
            related = None
        if related:
            emit({
                "query": args.query, "geo": args.geo, "timeframe": args.timeframe,
                "source": "logged-in (per-keyword)",
                "note": ("Per-keyword related trends from a logged-in Pinterest "
                         "session. The interest-over-time chart is an SVG and "
                         "isn't text-extractable; these are relative signals."),
                "related_trends": related,
            })
            return
        warn("logged in but no per-keyword data parsed — using public spotlight")

    # Degraded (no login): public regional spotlight filtered to the query.
    body = load_trends(args.geo, args.query)
    trends = parse_spotlight(body)
    words = [w.lower() for w in args.query.split()]
    matches = [t for t in trends
               if any(w in t["term"].lower() for w in words)
               or (t.get("categories") and any(w in c.lower()
                   for c in t["categories"] for w in words))]
    emit({
        "query": args.query,
        "geo": args.geo,
        "timeframe": args.timeframe,
        "source": "public spotlight (not logged in)",
        "note": ("BEST-EFFORT: Pinterest gates per-keyword Trends behind a "
                 "login, so this is the public regional spotlight. Log into a "
                 "Pinterest account in the scout browser for full per-keyword "
                 "trend data. 'related_trends' are spotlight entries matching "
                 "your query; 'all_spotlight' is the full list. Relative "
                 "signals, not exact volumes."),
        "related_trends": [{"term": t["term"], "trend": t["trend"],
                            "growth": t["growth"]} for t in matches] or None,
        "all_spotlight": [{"term": t["term"], "growth": t["growth"],
                          "categories": t["categories"]} for t in trends],
    })


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="Pinterest Trends for a topic")
    s.add_argument("query")
    s.add_argument("--geo", default="US")
    s.add_argument("--timeframe", default="last_12_months",
                   choices=["last_30_days", "last_3_months", "last_12_months"])
    s.set_defaults(func=cmd_search)

    t = sub.add_parser("trending", help="currently trending topics on Pinterest")
    t.add_argument("--geo", default="US")
    t.add_argument("--category", help="optional category filter")
    t.set_defaults(func=cmd_trending)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
