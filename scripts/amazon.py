#!/usr/bin/env python3
"""Amazon review + product scraper — no login, no API key.

Amazon aggressively bot-blocks scripted requests, so this treats Amazon like
any other rate-sensitive platform: a browser User-Agent, jittered pauses
between page fetches, and — when a direct fetch is blocked (503 / robot
check) — a fallback to the already-running scout browser (real Chrome, which
Amazon is far less likely to block). The scout browser is a bonus, never a
requirement; if it isn't running the command fails with a clear message.

Reviews: Amazon gates its /product-reviews/ pages behind a sign-in wall, so
`reviews` needs the scout browser logged into an Amazon account once (a spare
is safest; the login persists like Instagram/TikTok). `product` reads the
public /dp/ page and needs no login. Coverage is best-effort — Amazon changes
its markup often — so expect partial results sometimes and never a silent empty.

Subcommands:
  reviews <url> [--stars 1|2|3|4|5|all] [--sort helpful|recent]
                [--pages N] [--max-reviews N]
  product <url>

Works on amazon.com, amazon.co.uk, amazon.in, amazon.de, … (ASIN is extracted
the same way; the URL's domain is preserved). amzn.to short links are followed.
"""

import argparse
import re
import sys
import urllib.error
import urllib.request

from common import (clamp_cap, die, emit, jitter_sleep, warn)
from webread import BROWSER_UA

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from bs4 import BeautifulSoup
except ImportError:
    die("The 'beautifulsoup4' package is missing. "
        "Run: pip install -r requirements.txt")

# Same conservative philosophy as the IG/TikTok comment caps in common.py.
DEFAULT_MAX_REVIEWS = 30
HARD_MAX_REVIEWS = 100
DEFAULT_PAGES = 3
HARD_MAX_PAGES = 10

# Amazon's filter tokens for ?filterByStar= (numbers aren't accepted).
STAR_FILTER = {"1": "one_star", "2": "two_star", "3": "three_star",
               "4": "four_star", "5": "five_star"}

HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}


def bs(html):
    """Parse with lxml when available (fast), else the stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


# --------------------------------------------------------------------------
# URL / ASIN handling
# --------------------------------------------------------------------------
def resolve_short_link(url):
    """amzn.to links redirect to the real product URL — follow them once."""
    if "amzn.to" not in url:
        return url
    try:
        req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.geturl()
    except Exception:
        try:  # some hosts reject HEAD; fall back to GET
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.geturl()
        except Exception:
            return url


ASIN_RE = re.compile(r"/(?:dp|gp/product|product-reviews|gp/aw/d)/([A-Z0-9]{10})",
                     re.IGNORECASE)


def extract_asin_and_domain(url):
    """Return (asin, domain) from any Amazon product/review URL. Domain is kept
    so amazon.co.uk / .in / .de etc. keep working."""
    url = resolve_short_link(url.strip())
    m = ASIN_RE.search(url)
    if not m:
        # last resort: a bare 10-char ASIN anywhere in the path/query
        m = re.search(r"\b([A-Z0-9]{10})\b", url)
    if not m:
        die(f"Couldn't find an ASIN (the 10-character product code) in '{url}'. "
            "Paste a full Amazon product URL, e.g. .../dp/B08N5WRWNW")
    asin = m.group(1).upper()
    dom = re.search(r"https?://([^/]*amazon\.[a-z.]+)", url, re.IGNORECASE)
    domain = dom.group(1) if dom else "www.amazon.com"
    return asin, domain


# --------------------------------------------------------------------------
# fetching (direct -> scout browser fallback)
# --------------------------------------------------------------------------
BLOCK_MARKERS = ("Robot Check", "Enter the characters you see below",
                 "api-services-support@amazon.com", "To discuss automated access",
                 "/errors/validateCaptcha")


def looks_blocked(html):
    head = (html or "")[:4000]
    return any(m in head for m in BLOCK_MARKERS)


# Markers unique to the sign-in PAGE itself — deliberately NOT "/ap/signin",
# which also appears in the ordinary "Hello, sign in" nav link on every public
# page (including product pages).
SIGNIN_MARKERS = ("ap_email", "authportal-main-section", "signInSubmit",
                  'name="signIn"', "Sign in or create account")


def looks_signin(html):
    """Amazon gates its /product-reviews/ pages behind a sign-in wall for
    logged-out sessions (the /dp/ product page is public, reviews are not).
    Scan the whole document — the sign-in form sits below a large <head>."""
    return any(m in (html or "") for m in SIGNIN_MARKERS)


class LoginRequired(RuntimeError):
    """Raised when Amazon shows a sign-in wall instead of the page."""


SIGNIN_HELP = (
    "Amazon requires being signed in to read its review pages. Open the scout "
    "browser (python scripts/browser.py launch) and log into an Amazon account "
    "once (a spare account is safest) — the login persists like Instagram/"
    "TikTok — then retry. (Product info via `amazon.py product` needs no login.)")


def fetch_direct(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_via_browser(url):
    """Load the page in the running scout browser and return its full HTML.
    None if the scout browser isn't running or anything goes wrong."""
    try:
        import browser
        if not browser.is_running():
            return None
        b = browser.attach()
        tab = b.page(url, settle=5)
        try:
            html = tab.eval("document.documentElement.outerHTML")
        finally:
            tab.close()
        return html or None
    except Exception:
        return None


def fetch_html(url):
    """Direct fetch first; on a block/503 fall back to the scout browser.
    Raises RuntimeError with a clear message if everything fails."""
    direct_error = None
    try:
        html = fetch_direct(url)
        if looks_signin(html):
            # a logged-in scout browser may still get the real page
            direct_error = "Amazon returned a sign-in wall"
        elif not looks_blocked(html):
            return html
        else:
            direct_error = "Amazon returned a robot-check/captcha page"
    except urllib.error.HTTPError as e:
        # Amazon commonly answers scripted requests with 403/404/429/503 even
        # for pages that load fine in a real browser, so treat all of these as
        # "try the scout browser" rather than fatal.
        if e.code not in (503, 429, 403, 404):
            raise RuntimeError(f"Amazon fetch failed: HTTP {e.code}")
        direct_error = f"HTTP {e.code} from Amazon (blocked, rate-limited, "
        direct_error += "or the page isn't served to scripts)"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        direct_error = f"network error: {e}"

    html = fetch_via_browser(url)
    if html and looks_signin(html):
        raise LoginRequired(SIGNIN_HELP)
    if html and not looks_blocked(html):
        return html
    raise RuntimeError(
        f"Could not fetch this Amazon page ({direct_error}). Amazon is blocking "
        "scripted access, or the ASIN doesn't exist. Start the scout browser "
        "(python scripts/browser.py launch) and retry — a real Chrome usually "
        "gets through — or wait a few minutes and try again.")


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------
def _text(node):
    if not node:
        return None
    t = node.get_text(" ", strip=True)
    return t or None


def _first(soup, selectors):
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            return node
    return None


def parse_rating_value(text):
    """'4.6 out of 5 stars' / '4.6 out of 5' -> 4.6 (float) or None."""
    if not text:
        return None
    m = re.search(r"([0-5](?:\.\d)?)\s*out of\s*5", text)
    if not m:
        m = re.search(r"^\s*([0-5](?:\.\d)?)", text)
    return float(m.group(1)) if m else None


def parse_int(text):
    if not text:
        return None
    m = re.search(r"([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else None


def parse_product_meta(soup):
    """Best-effort product-level fields. Works on both /dp/ and review pages;
    every field defaults to None so missing markup never crashes."""
    title = _text(_first(soup, ["#productTitle", "span#productTitle",
                                'a[data-hook="product-link"]',
                                "div.product-title h1", "h1.product-title"]))
    price = _text(_first(soup, ["span.a-price span.a-offscreen",
                                "#corePrice_feature_div span.a-offscreen",
                                "#priceblock_ourprice", "#priceblock_dealprice"]))
    overall = parse_rating_value(_text(_first(soup, [
        'span[data-hook="rating-out-of-text"]',
        'i[data-hook="average-star-rating"] span.a-icon-alt',
        "#acrPopover span.a-icon-alt",
        'span[data-hook="acr-average-stars-rating-text"]'])))
    total = parse_int(_text(_first(soup, [
        "#acrCustomerReviewText",
        'div[data-hook="total-review-count"]',
        'span[data-hook="total-review-count"]'])))

    breakdown = {}
    # Histogram rows carry an aria-label such as "5 stars represent 70% of
    # ratings" or "70 percent of reviews have 5 stars" — order varies, and %
    # may be written "%" or the word "percent".
    for node in soup.select('[aria-label*="star"], [aria-label*="stars"]'):
        label = node.get("aria-label", "")
        if "%" not in label and "percent" not in label.lower():
            continue
        star_m = re.search(r"([1-5])\s*stars?", label)
        pct_m = re.search(r"(\d{1,3})\s*(?:%|percent)", label, re.IGNORECASE)
        if star_m and pct_m:
            breakdown.setdefault(star_m.group(1), f"{pct_m.group(1)}%")

    # Fallback: classic histogram table rows ("5 star" … "70%").
    if not breakdown:
        for row in soup.select("#histogramTable tr, .a-histogram-row, "
                               "tr.a-histogram-row, [data-hook='histogram-row']"):
            row_text = _text(row) or ""
            m = re.search(r"([1-5])\s*star.*?(\d{1,3})\s*%", row_text)
            if m:
                breakdown[m.group(1)] = f"{m.group(2)}%"

    bullets = [t for t in (
        _text(li) for li in soup.select(
            "#feature-bullets li span.a-list-item, #feature-bullets li")) if t]
    # de-dupe while preserving order
    seen, bullet_list = set(), []
    for b in bullets:
        if b not in seen:
            seen.add(b)
            bullet_list.append(b)

    return {
        "product_title": title,
        "price": price,
        "overall_rating": overall,
        "total_ratings": total,
        "rating_breakdown": breakdown or None,
        "feature_bullets": bullet_list or None,
    }


def parse_reviews(soup):
    """Extract review dicts from a product-reviews page."""
    out = []
    for r in soup.select('div[data-hook="review"], div.review'):
        rating = parse_rating_value(_text(_first(r, [
            'i[data-hook="review-star-rating"] span.a-icon-alt',
            'i[data-hook="cmps-review-star-rating"] span.a-icon-alt',
            "span.a-icon-alt"])))
        title = _text(_first(r, ['a[data-hook="review-title"] span:not(.a-letter-space)',
                                 'a[data-hook="review-title"]',
                                 'span[data-hook="review-title"]']))
        date_raw = _text(_first(r, ['span[data-hook="review-date"]']))
        date = date_raw
        if date_raw and " on " in date_raw:  # "Reviewed in X on <date>"
            date = date_raw.split(" on ", 1)[1].strip()
        helpful = parse_int(_text(_first(r, ['span[data-hook="helpful-vote-statement"]'])))
        out.append({
            "author": _text(_first(r, ["span.a-profile-name", ".a-profile-name"])),
            "rating": int(rating) if rating is not None else None,
            "title": title,
            "body": _text(_first(r, ['span[data-hook="review-body"] span',
                                     'span[data-hook="review-body"]',
                                     "div.a-row.review-data span.review-text"])),
            "date": date,
            "verified_purchase": bool(_first(r, ['span[data-hook="avp-badge"]'])),
            "helpful_count": helpful or 0,
        })
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def review_page_url(domain, asin, sort, star, page):
    params = [f"sortBy={'recent' if sort == 'recent' else 'helpful'}",
              f"pageNumber={page}", "reviewerType=all_reviews"]
    if star in STAR_FILTER:
        params.append(f"filterByStar={STAR_FILTER[star]}")
    return (f"https://{domain}/product-reviews/{asin}/"
            f"ref=cm_cr_arp_d_viewopt_srt?" + "&".join(params))


def cmd_reviews(args):
    asin, domain = extract_asin_and_domain(args.url)
    pages = clamp_cap(args.pages, DEFAULT_PAGES, HARD_MAX_PAGES, "pages")
    cap = clamp_cap(args.max_reviews, DEFAULT_MAX_REVIEWS, HARD_MAX_REVIEWS,
                    "max-reviews")
    star = args.stars if args.stars in STAR_FILTER else "all"

    reviews, meta, via = [], None, "direct"
    for page in range(1, pages + 1):
        if page > 1:
            jitter_sleep(2, 6, "pacing Amazon review pages")
        url = review_page_url(domain, asin, args.sort, star, page)
        try:
            html = fetch_html(url)
        except RuntimeError as e:
            if reviews:  # got some pages before the block — return what we have
                warn(f"stopped early on page {page}: {e}")
                break
            die(str(e))
        soup = bs(html)
        if meta is None:
            meta = parse_product_meta(soup)
        batch = parse_reviews(soup)
        if not batch:
            break  # no more reviews (ran past the last page or none exist)
        reviews.extend(batch)
        if len(reviews) >= cap:
            break

    reviews = reviews[:cap]
    payload = {
        "asin": asin,
        "domain": domain,
        "stars_filter": star,
        "sort": "recent" if args.sort == "recent" else "helpful",
        "product": meta or {},
        "review_count": len(reviews),
        "reviews": reviews,
    }
    if not reviews:
        payload["note"] = ("No reviews parsed. Either this product has no "
                           "reviews for that filter, or Amazon changed its "
                           "markup / gated them. Try --stars all, or start the "
                           "scout browser and retry.")
    emit(payload)


def cmd_product(args):
    asin, domain = extract_asin_and_domain(args.url)
    url = f"https://{domain}/dp/{asin}"
    try:
        html = fetch_html(url)
    except RuntimeError as e:
        die(str(e))
    meta = parse_product_meta(bs(html))
    meta.update({"asin": asin, "domain": domain, "url": url})
    if not meta.get("product_title"):
        meta["note"] = ("Could not read the product title — Amazon may have "
                        "blocked the request or changed its markup. Start the "
                        "scout browser and retry.")
    emit(meta)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("reviews", help="pull reviews for an Amazon product")
    r.add_argument("url")
    r.add_argument("--stars", default="all",
                   choices=["1", "2", "3", "4", "5", "all"],
                   help="filter by star rating (default all)")
    r.add_argument("--sort", default="helpful", choices=["helpful", "recent"])
    r.add_argument("--pages", type=int, default=DEFAULT_PAGES,
                   help=f"review pages to fetch (default {DEFAULT_PAGES}, "
                        f"max {HARD_MAX_PAGES})")
    r.add_argument("--max-reviews", type=int, default=DEFAULT_MAX_REVIEWS,
                   help=f"cap total reviews (default {DEFAULT_MAX_REVIEWS}, "
                        f"max {HARD_MAX_REVIEWS})")
    r.set_defaults(func=cmd_reviews)

    pr = sub.add_parser("product", help="product metadata only (no reviews)")
    pr.add_argument("url")
    pr.set_defaults(func=cmd_product)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
