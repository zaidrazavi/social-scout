#!/usr/bin/env python3
"""Google Shopping search + product details — price/competitive intel.

Google fights scraping of Shopping hard and rotates its markup constantly, so
this is a resilient best-effort scraper: it tries a direct fetch first, then
the scout browser (real Chrome), scrolls to load tiles, and extracts products
by scanning for price-bearing cards rather than brittle CSS classes. When
Google serves its fully-obfuscated JS layout it may return few or no rows —
that's reported honestly, never as a crash. Prices are region-dependent (tied
to the IP / --country).

Subcommands:
  search <query> [--limit N] [--min-price X] [--max-price X] [--country us]
  product <url>              detailed listing (scout browser; JS-rendered)
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

DEFAULT_LIMIT = 20
HARD_MAX = 50

# In-page extractor: scroll, then collect product tiles by finding links whose
# card text carries a price. Class-agnostic on purpose (Google rotates classes).
EXTRACTOR = r"""
(() => {
  const MAX = __MAX__;
  // real product prices carry cents ($20.00, £12.99); round "$9" tokens are
  // usually price-filter chips / nav chrome, so require the decimals.
  const PRICE = /[$£€₹]\s?\d[\d,]*\.\d{2}/;
  const NAV = /skip to main|continuous scrolling|accessibility|sign in|small business|get it fast|on sale|nearby|turn off|refine results|search results/i;
  const isProdLink = h => /^https?:/.test(h) &&
    !/google\.[a-z.]+\/(search|preferences|advanced_search|intl|policies|support|setprefs)/.test(h);
  const out = [], seen = new Set();
  // Anchor on the PRICE leaf and climb to a BOUNDED card (so we get one tile,
  // not the whole results region), then read the product fields from it.
  const priceLeaves = [...document.querySelectorAll("span,div,b,strong")]
    .filter(e => e.children.length === 0 && PRICE.test((e.textContent || "").trim()));
  for (const pn of priceLeaves) {
    if (out.length >= MAX) break;
    let card = pn;
    for (let i = 0; i < 9 && card.parentElement; i++) {
      card = card.parentElement;
      const t = card.innerText || "";
      if ([...card.querySelectorAll("a[href]")].some(a => isProdLink(a.href))
          && t.length >= 30 && t.length <= 500) break;
    }
    const txt = (card.innerText || "").trim();
    if (txt.length > 600) continue;  // overshot into a big container
    const a = [...card.querySelectorAll("a[href]")].find(x => isProdLink(x.href));
    if (!a) continue;
    const href = a.href;
    const img0 = card.querySelector("img");
    const pm = txt.match(PRICE);
    if (!pm) continue;
    const lines = txt.split("\n").map(s => s.trim()).filter(Boolean);
    const title = lines.find(l => !PRICE.test(l) && l.length > 12
                                  && !NAV.test(l)
                                  && !/^\d(\.\d)?\s*(star|out of)/i.test(l));
    if (!title || seen.has(title) || NAV.test(title)) continue;
    seen.add(title);
    const rating = (txt.match(/(\d(?:\.\d)?)\s*(?:star|out of 5)/i) || [])[1] || null;
    const reviews = (txt.match(/([\d,]+)\s*(?:reviews?|ratings?)/i) || [])[1] || null;
    // seller = a short line after the price that isn't the title
    let seller = null;
    for (const l of lines) {
      if (l !== title && !PRICE.test(l) && l.length <= 40
          && !/star|review|rating|shipping|free|delivery/i.test(l)) { seller = l; break; }
    }
    const shipping = (txt.match(/(free (?:delivery|shipping)|[$£€₹][\d.,]+ (?:delivery|shipping))/i) || [])[0] || null;
    out.push({
      title: title,
      price: pm[0],
      seller: seller,
      rating: rating ? parseFloat(rating) : null,
      review_count: reviews ? parseInt(reviews.replace(/,/g, "")) : null,
      url: href,
      image_url: (img0 && /^https?:/.test(img0.src || "")) ? img0.src : null,
      shipping: shipping,
    });
  }
  return out;
})()
"""


def build_url(query, country, min_price=None, max_price=None):
    params = {"q": query, "tbm": "shop", "hl": "en", "gl": country}
    tbs = []
    if min_price is not None:
        tbs.append(f"price:1,ppr_min:{min_price}")
    if max_price is not None:
        tbs.append(f"ppr_max:{max_price}")
    if tbs:
        params["tbs"] = "mr:1," + ",".join(tbs)
    return "https://www.google.com/search?" + urllib.parse.urlencode(params)


def open_scout():
    try:
        import browser
    except Exception as e:
        die(f"Could not load the scout browser module: {e}")
    if not browser.is_running():
        warn("scout browser not running — launching it...")
        browser.launch()
    return browser.attach()


def extract_via_browser(url, limit):
    tab = open_scout().page(url, settle=6)
    try:
        if "consent" in (tab.eval("location.href") or "").lower():
            die("Google showed a consent wall. Open the scout browser window, "
                "accept/decline the consent prompt once, then retry.")
        ads = []
        for _ in range(5):
            tab.eval("window.scrollTo(0, document.body.scrollHeight)")
            jitter_sleep(2, 4, "loading Google Shopping tiles")
            ads = tab.eval(EXTRACTOR.replace("__MAX__", str(limit))) or []
            if len(ads) >= limit:
                break
        return ads[:limit]
    finally:
        tab.close()


def cmd_search(args):
    limit = clamp_cap(args.limit, DEFAULT_LIMIT, HARD_MAX, "limit")
    url = build_url(args.query, args.country, args.min_price, args.max_price)
    products = extract_via_browser(url, limit)
    payload = {"query": args.query, "country": args.country,
               "count": len(products), "products": products}
    if args.min_price is not None or args.max_price is not None:
        payload["price_filter"] = {"min": args.min_price, "max": args.max_price}
    if not products:
        payload["note"] = ("No products parsed. Google Shopping serves a "
                           "heavily-obfuscated JS layout that rotates often and "
                           "varies by region — try again, adjust --country, or "
                           "open the scout browser window to view results.")
    emit(payload)


def cmd_product(args):
    tab = open_scout().page(args.url, settle=6)
    try:
        title = tab.eval("document.querySelector('h1')?.innerText "
                         "|| document.title") or None
        body = (tab.eval("document.body.innerText") or "")
        prices = re.findall(r"[$£€₹]\s?\d[\d.,]*", body)
        rating = (re.search(r"(\d(?:\.\d)?)\s*(?:star|out of 5)", body, re.I) or [None, None])[1]
        reviews = (re.search(r"([\d,]+)\s*(?:reviews?|ratings?)", body, re.I) or [None, None])[1]
    finally:
        tab.close()
    payload = {
        "url": args.url,
        "title": (title or "").strip()[:200] or None,
        "prices_seen": sorted(set(prices))[:15] or None,
        "rating": float(rating) if rating else None,
        "review_count": int(reviews.replace(",", "")) if reviews else None,
    }
    if not payload["title"] and not payload["prices_seen"]:
        payload["note"] = ("Couldn't parse this Google Shopping product page — "
                           "Google's product pages are heavily JS-rendered and "
                           "may need you to view them in the scout browser.")
    emit(payload)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="search Google Shopping")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    s.add_argument("--min-price", type=float)
    s.add_argument("--max-price", type=float)
    s.add_argument("--country", default="us")
    s.set_defaults(func=cmd_search)

    pr = sub.add_parser("product", help="details for a Shopping product URL")
    pr.add_argument("url")
    pr.set_defaults(func=cmd_product)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
