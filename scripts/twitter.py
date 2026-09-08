#!/usr/bin/env python3
"""Twitter/X search + user timeline — ⚠️ BEST-EFFORT (like Quora).

Twitter/X blocks scraping aggressively and killed most third-party access, so
this tries a chain of unreliable sources and reports which one answered:

  1. Nitter mirrors (open-source X frontends) — direct fetch, then the scout
     browser to clear their "verify your browser" JS challenge.
  2. Scout browser on x.com search/profile — public results only, no login;
     X shows limited results logged-out.
  3. Web search (DuckDuckGo, site:x.com) — minimal data, just links + snippets.

Nitter instances go up and down constantly and X limits logged-out results, so
expect gaps, missing engagement counts, or total unavailability. Never promise
Twitter-level reliability from this.

Subcommands:
  search <query> [--limit N] [--sort top|latest]
  user <handle>  [--limit N] [--replies]
"""

import argparse
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from common import clamp_cap, die, emit, jitter_sleep, warn
from webread import BROWSER_UA

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from bs4 import BeautifulSoup
except ImportError:
    die("The 'beautifulsoup4' package is missing. Run: pip install -r requirements.txt")

DEFAULT_LIMIT = 20
HARD_MAX = 50
NITTER = ["https://nitter.net", "https://xcancel.com",
          "https://nitter.privacydev.net", "https://nitter.poast.org",
          "https://nitter.tiekoetter.com", "https://nitter.1d4.us"]
DDG = "https://html.duckduckgo.com/html/"


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------
def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def browser_html(url, settle=6):
    try:
        import browser
        if not browser.is_running():
            browser.launch()
        tab = browser.attach().page(url, settle=settle)
        try:
            return tab.eval("document.documentElement.outerHTML")
        finally:
            tab.close()
    except Exception:
        return None


def is_challenge(html):
    head = (html or "")[:2000]
    return ("Verifying your browser" in head or "Just a moment" in head
            or "Checking your browser" in head)


# --------------------------------------------------------------------------
# Nitter parsing
# --------------------------------------------------------------------------
def parse_nitter(html, base):
    soup = BeautifulSoup(html, "lxml")
    tweets = []
    for item in soup.select(".timeline-item"):
        if item.select_one(".show-more"):
            continue
        content = item.select_one(".tweet-content")
        if not content:
            continue
        user = item.select_one(".username")
        name = item.select_one(".fullname")
        date = item.select_one(".tweet-date a")
        link = item.select_one("a.tweet-link")
        stats = {}
        for stat in item.select(".tweet-stat"):
            icon = stat.select_one("[class*=icon-]")
            num = re.sub(r"[^\d]", "", stat.get_text(" ", strip=True)) or "0"
            if icon:
                cls = " ".join(icon.get("class", []))
                key = ("replies" if "comment" in cls else "retweets"
                       if "retweet" in cls else "likes" if "heart" in cls else None)
                if key:
                    stats[key] = int(num)
        path = link.get("href", "").split("#")[0] if link else ""
        tweets.append({
            "author": user.get_text(strip=True) if user else None,
            "display_name": name.get_text(strip=True) if name else None,
            "text": content.get_text(" ", strip=True),
            "date": (date.get("title") or date.get_text(strip=True)) if date else None,
            "likes": stats.get("likes"),
            "retweets": stats.get("retweets"),
            "replies": stats.get("replies"),
            "url": ("https://x.com" + path) if path.startswith("/") else path or None,
        })
    return tweets


def try_nitter(path, limit):
    """Loop Nitter instances: direct fetch, then scout browser for challenge
    pages. Returns (tweets, source) or (None, None)."""
    for base in NITTER:
        url = base + path
        html = None
        try:
            html = http_get(url)
        except Exception:
            html = None
        if html and is_challenge(html):
            html = browser_html(url)  # clear the JS challenge in real Chrome
        if not html or is_challenge(html):
            continue
        tweets = parse_nitter(html, base)
        if tweets:
            return tweets[:limit], base.replace("https://", "")
    return None, None


# --------------------------------------------------------------------------
# x.com scout-browser fallback
# --------------------------------------------------------------------------
X_EXTRACTOR = r"""
(() => {
  const MAX = __MAX__, out = [], seen = new Set();
  for (const art of document.querySelectorAll('article')) {
    if (out.length >= MAX) break;
    const textEl = art.querySelector('[data-testid="tweetText"]');
    if (!textEl) continue;
    const text = textEl.innerText.trim();
    if (!text || seen.has(text)) continue; seen.add(text);
    let handle = null, name = null, url = null, date = null;
    const nameEl = art.querySelector('[data-testid="User-Name"]');
    if (nameEl) {
      const m = nameEl.innerText.match(/@[\w]+/);
      handle = m ? m[0] : null;
      name = nameEl.innerText.split("\n")[0] || null;
    }
    const t = art.querySelector('time');
    if (t) { date = t.getAttribute('datetime'); const a = t.closest('a'); if (a) url = a.href; }
    const stat = k => {
      const el = art.querySelector('[data-testid="'+k+'"]');
      if (!el) return null;
      const m = (el.getAttribute('aria-label')||'').match(/([\d,]+)/);
      return m ? parseInt(m[1].replace(/,/g,'')) : null;
    };
    // views have no stable testid — read them from the "N Views" analytics link
    let views = null;
    const vm = (art.innerText||'').match(/([\d,.]+[KM]?)\s*Views/i);
    if (vm) views = vm[1];
    out.push({author: handle, display_name: name, text: text,
              date: date ? date.split("T")[0] : null,
              likes: stat('like'), retweets: stat('retweet'), replies: stat('reply'),
              views: views, url: url});
  }
  return out;
})()
"""

X_ERROR = ("Something went wrong", "Try again", "Rate limit exceeded",
           "This account doesn’t exist", "Caution: This account is temporarily")


def try_x_com(url, limit, settle=8, source="x.com (scout browser, logged out)"):
    """Extract tweets from x.com in the scout browser. Returns (tweets, source)
    or (None, None) when login-walled, error-paged, or empty. `settle` is
    longer for the logged-in tier — x.com is slow to render."""
    try:
        import browser
        if not browser.is_running():
            browser.launch()
        tab = browser.attach().page(url, settle=settle)
        try:
            landed = (tab.eval("location.href") or "").lower()
            if "login" in landed or "/i/flow" in landed or "/account/access" in landed:
                return None, None  # login/verification wall
            head = (tab.eval("document.body.innerText") or "")[:400]
            if any(e in head for e in X_ERROR):
                return None, None  # x.com error / rate-limit page
            tab.eval("window.scrollTo(0, document.body.scrollHeight)")
            jitter_sleep(2, 4, "loading x.com results")
            tweets = tab.eval(X_EXTRACTOR.replace("__MAX__", str(limit))) or []
        finally:
            tab.close()
        return (tweets[:limit], source) if tweets else (None, None)
    except Exception:
        return None, None


def twitter_session():
    """The scout Browser if it's running AND has an X login, else None. Never
    launches the browser just to check — login is optional for this scraper."""
    try:
        import browser
        if not browser.is_running():
            return None
        b = browser.attach()
        return b if b.twitter_logged_in() else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# web-search last resort
# --------------------------------------------------------------------------
def try_web_search(query, limit):
    q = urllib.parse.urlencode({"q": f"site:x.com OR site:twitter.com {query}"})
    try:
        html = http_get(f"{DDG}?{q}", timeout=30)
    except Exception:
        return None, None
    urls, seen = [], set()
    for m in re.finditer(r'href="[^"]*?uddg=([^"&]+)', html):
        u = urllib.parse.unquote(m.group(1)).split("#")[0]
        host = urllib.parse.urlparse(u).netloc
        if ("x.com" in host or "twitter.com" in host) and "/status/" in u and u not in seen:
            seen.add(u)
            urls.append(u)
        if len(urls) >= limit:
            break
    if not urls:
        return None, None
    return [{"url": u, "text": None, "author": None,
             "note": "link only (web-search fallback)"} for u in urls], "web search (DuckDuckGo)"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def run_chain(nitter_path, x_url, limit, web_query=None):
    # Tier 0 (best data): logged-in x.com in the scout browser — full tweet
    # text + engagement. Only when a spare X account is logged in; optional.
    if twitter_session() is not None:
        tweets, source = try_x_com(x_url, limit, settle=9,
                                   source="x.com (scout browser, logged in)")
        if tweets:
            return tweets, source
        warn("logged into X but no results extracted — falling back...")
    # Tier 1: Nitter mirrors.
    tweets, source = try_nitter(nitter_path, limit)
    if tweets:
        return tweets, source
    # Tier 2: logged-out x.com (limited public results).
    warn("Nitter mirrors unavailable — trying x.com logged out...")
    tweets, source = try_x_com(x_url, limit)
    if tweets:
        return tweets, source
    # Tier 3: web search (links only).
    if web_query:
        warn("x.com logged-out results unavailable — trying web search...")
        tweets, source = try_web_search(web_query, limit)
        if tweets:
            return tweets, source
    return None, None


def cmd_search(args):
    limit = clamp_cap(args.limit, DEFAULT_LIMIT, HARD_MAX, "limit")
    f = "live" if args.sort == "latest" else ""
    npath = "/search?" + urllib.parse.urlencode(
        {"q": args.query, "f": "tweets"})
    xurl = ("https://x.com/search?" + urllib.parse.urlencode(
        {"q": args.query, "src": "typed_query", "f": args.sort}))
    tweets, source = run_chain(npath, xurl, limit, web_query=args.query)
    if tweets is None:
        die("Twitter/X is currently unreachable via every fallback (Nitter "
            "mirrors down, x.com logged-out blocked, web search empty). This "
            "backend is best-effort — try again later.")
    emit({"query": args.query, "source": source, "sort": args.sort,
          "count": len(tweets), "best_effort": True, "tweets": tweets})


def cmd_user(args):
    limit = clamp_cap(args.limit, DEFAULT_LIMIT, HARD_MAX, "limit")
    handle = args.handle.lstrip("@")
    npath = f"/{handle}" + ("/with_replies" if args.replies else "")
    xurl = f"https://x.com/{handle}" + ("/with_replies" if args.replies else "")
    tweets, source = run_chain(npath, xurl, limit,
                               web_query=f"from:{handle}")
    if tweets is None:
        die(f"Couldn't reach @{handle} via any fallback (Nitter down, x.com "
            "logged-out blocked). Best-effort backend — try again later.")
    emit({"handle": f"@{handle}", "source": source,
          "includes_replies": bool(args.replies),
          "count": len(tweets), "best_effort": True, "tweets": tweets})


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="search tweets (best-effort)")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    s.add_argument("--sort", default="top", choices=["top", "latest"])
    s.set_defaults(func=cmd_search)

    u = sub.add_parser("user", help="recent tweets from a user (best-effort)")
    u.add_argument("handle", help="@handle or handle")
    u.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    u.add_argument("--replies", action="store_true", help="include replies")
    u.set_defaults(func=cmd_user)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
