#!/usr/bin/env python3
"""Trustpilot reviews + brand overview — public pages, no login.

Trustpilot is a Next.js app, so the reliable data source is the embedded
__NEXT_DATA__ JSON blob (businessUnit + reviews), not fragile CSS selectors.
Trustpilot 403s plain scripted requests, so this tries a direct fetch first
(works from some IPs) and falls back to the scout browser (real Chrome) like
amazon.py / quora.py.

Note: Trustpilot pages are locale-specific — the same brand can have different
reviews on .com vs .co.uk.

Subcommands:
  reviews <brand|url> [--stars 1-5|all] [--sort recency|relevance]
                      [--pages N] [--max-reviews N]
  brand <brand|url>
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from common import clamp_cap, die, emit, jitter_sleep, warn
from webread import BROWSER_UA

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MAX_REVIEWS = 30
HARD_MAX_REVIEWS = 100
DEFAULT_PAGES = 3
HARD_MAX_PAGES = 10
NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def review_url(brand):
    """Accept a full Trustpilot URL or a bare domain (example.com)."""
    brand = brand.strip()
    if "trustpilot.com/review/" in brand:
        return brand.split("?")[0].rstrip("/")
    domain = re.sub(r"^https?://(www\.)?", "", brand).strip("/").split("/")[0]
    if not domain or "." not in domain:
        die(f"'{brand}' doesn't look like a brand domain (e.g. example.com) "
            "or a Trustpilot review URL.")
    return f"https://www.trustpilot.com/review/{domain}"


def _fetch_direct(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _fetch_browser(url):
    try:
        import browser
        if not browser.is_running():
            return None
        tab = browser.attach().page(url, settle=6)
        try:
            return tab.eval("document.documentElement.outerHTML")
        finally:
            tab.close()
    except Exception:
        return None


def fetch_next_data(url):
    """Return the parsed __NEXT_DATA__ pageProps for a Trustpilot URL.
    Direct fetch first, scout browser fallback. Raises on total failure or a
    genuine 'brand not on Trustpilot' 404."""
    direct_err = None
    html = None
    try:
        html = _fetch_direct(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(
                f"No Trustpilot page found for this brand ({url}). Check the "
                "domain, or the brand may not be on Trustpilot.")
        direct_err = f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        direct_err = str(e)

    if not html or "__NEXT_DATA__" not in html:
        html = _fetch_browser(url)
    if not html:
        raise RuntimeError(
            f"Could not load Trustpilot ({direct_err or 'blocked'}). Trustpilot "
            "blocks scripted access — start the scout browser "
            "(python scripts/browser.py launch) and retry.")
    m = NEXT_RE.search(html)
    if not m:
        # a real 404 page also lacks __NEXT_DATA__
        if "Page not found" in html[:5000] or "/404" in html[:5000]:
            raise RuntimeError(f"No Trustpilot page found for this brand ({url}).")
        raise RuntimeError("Trustpilot loaded but its data blob was missing — "
                           "the page markup may have changed.")
    return json.loads(m.group(1)).get("props", {}).get("pageProps", {})


def iso_date(s):
    return s.split("T")[0] if s else None


def norm_review(r, full_body=True):
    dates = r.get("dates") or {}
    labels = r.get("labels") or {}
    verification = labels.get("verification") or {}
    reply = r.get("reply") or None
    body = r.get("text") or ""
    return {
        "author": (r.get("consumer") or {}).get("displayName"),
        "rating": r.get("rating"),
        "title": r.get("title"),
        "body": body if full_body or len(body) <= 1500 else body[:1500] + "…",
        "date": iso_date(dates.get("publishedDate")),
        "verified": bool(verification.get("isVerified")),
        "reply": (reply.get("message") if isinstance(reply, dict) else None),
    }


def rating_breakdown(pp):
    """Best-effort star distribution → percentages, if the page carries counts
    (usually under the star filter options)."""
    filters = pp.get("filters") or {}
    # look for a list of {stars/starRating, count/numberOfReviews}
    candidates = []
    for key in ("reviewStatistics", "starFilters", "stars"):
        v = filters.get(key)
        if isinstance(v, list):
            candidates = v
            break
    counts = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        star = item.get("stars") or item.get("starRating") or item.get("value")
        cnt = item.get("count") or item.get("numberOfReviews")
        if star in (1, 2, 3, 4, 5) and isinstance(cnt, int):
            counts[str(star)] = cnt
    if not counts:
        return None
    total = sum(counts.values()) or 1
    return {s: f"{round(counts[s] / total * 100)}%" for s in sorted(counts, reverse=True)}


def cmd_reviews(args):
    base = review_url(args.brand)
    pages = clamp_cap(args.pages, DEFAULT_PAGES, HARD_MAX_PAGES, "pages")
    cap = clamp_cap(args.max_reviews, DEFAULT_MAX_REVIEWS, HARD_MAX_REVIEWS,
                    "max-reviews")
    star = args.stars if args.stars in ("1", "2", "3", "4", "5") else None

    reviews, biz = [], None
    for page in range(1, pages + 1):
        if page > 1:
            jitter_sleep(2, 6, "pacing Trustpilot pages")
        params = {"sort": args.sort, "page": page}
        if star:
            params["stars"] = star
        url = base + "?" + urllib.parse.urlencode(params)
        try:
            pp = fetch_next_data(url)
        except RuntimeError as e:
            if reviews:
                warn(f"stopped on page {page}: {e}")
                break
            die(str(e))
        if biz is None:
            biz = pp.get("businessUnit") or {}
        batch = pp.get("reviews") or []
        if not batch:
            break
        for r in batch:
            if star and r.get("rating") != int(star):
                continue
            reviews.append(norm_review(r))
        if len(reviews) >= cap:
            break

    reviews = reviews[:cap]
    payload = {
        "brand": (biz or {}).get("displayName") or args.brand,
        "trustpilot_url": base,
        "overall_rating": (biz or {}).get("trustScore"),
        "total_reviews": (biz or {}).get("numberOfReviews"),
        "stars_filter": star or "all",
        "sort": args.sort,
        "review_count": len(reviews),
        "reviews": reviews,
    }
    if not reviews:
        payload["note"] = ("No reviews parsed for that filter. Try --stars all, "
                           "or the brand may have no reviews on this locale.")
    emit(payload)


def cmd_brand(args):
    base = review_url(args.brand)
    try:
        pp = fetch_next_data(base)
    except RuntimeError as e:
        die(str(e))
    biz = pp.get("businessUnit") or {}
    cats = [c.get("name") for c in (biz.get("categories") or []) if c.get("name")]
    emit({
        "brand_name": biz.get("displayName"),
        "trustpilot_url": base,
        "overall_rating": biz.get("trustScore"),
        "stars": biz.get("stars"),
        "total_reviews": biz.get("numberOfReviews"),
        "reviews_last_12m": biz.get("numberOfReviewsLast12Months"),
        "trust_score": biz.get("trustScore"),
        "rating_breakdown": rating_breakdown(pp),
        "categories": cats or None,
        "website": biz.get("websiteUrl"),
        "is_claimed": biz.get("isClaimed"),
    })


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("reviews", help="pull reviews for a brand")
    r.add_argument("brand", help="brand domain (example.com) or Trustpilot URL")
    r.add_argument("--stars", default="all",
                   choices=["1", "2", "3", "4", "5", "all"])
    r.add_argument("--sort", default="recency", choices=["recency", "relevance"])
    r.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    r.add_argument("--max-reviews", type=int, default=DEFAULT_MAX_REVIEWS)
    r.set_defaults(func=cmd_reviews)

    b = sub.add_parser("brand", help="brand overview only (no reviews)")
    b.add_argument("brand", help="brand domain or Trustpilot URL")
    b.set_defaults(func=cmd_brand)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
