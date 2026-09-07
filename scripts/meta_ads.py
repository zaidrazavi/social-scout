#!/usr/bin/env python3
"""Meta (Facebook/Instagram) Ad Library scraper — competitor ad intel.

The Ad Library (https://www.facebook.com/ads/library/) is PUBLIC — no Facebook
login needed — but it renders entirely with JavaScript, so this drives the
scout browser (real Chrome) exactly like the Instagram/TikTok scrapers. If the
scout browser isn't running it is auto-launched. Ads are read from the
rendered DOM, keyed on each card's "Library ID".

Tip for the reader: an ad that has been running a long time is usually a
winner (advertisers kill losers fast).

Subcommands:
  search <query>              ads matching a keyword. --country, --max-ads
  page <name|url|page_id>     active ads from one advertiser. --country, --max-ads

--country: US (default), GB, IN, AU, … (the Ad Library "country" filter).
--max-ads: default 20, max 50.
"""

import argparse
import json
import re
import sys
import urllib.parse

from common import clamp_cap, die, emit, jitter_sleep, warn

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MAX_ADS = 20
HARD_MAX_ADS = 50
LIB = "https://www.facebook.com/ads/library/"

# Extracted in-page. Keyed on each card's "Library ID:" node, climbing to the
# card container (the nearest ancestor holding both "Sponsored" and "Started
# running"), then reading fields from that subtree. Platforms/media are icons,
# so they come from aria-labels / element presence, not visible text.
EXTRACTOR = r"""
(() => {
  const MAX = __MAX__;
  const CTA = new Set(["Shop now","Shop Now","Learn more","Learn More","Sign up",
    "Sign Up","Book now","Book Now","Download","Get offer","Get Offer","Send message",
    "Contact us","Subscribe","Order now","Apply now","Watch more","Get quote","Play game"]);
  const PLATS = ["Facebook","Instagram","Messenger","Audience Network","Threads"];
  const seen = new Set(), out = [];
  const nodes = [];
  document.querySelectorAll("span,div").forEach(el => {
    const m = (el.textContent || "").match(/^Library ID:\s*(\d+)\s*$/);
    if (m && !seen.has(m[1])) { seen.add(m[1]); nodes.push([el, m[1]]); }
  });
  for (const [node, id] of nodes) {
    if (out.length >= MAX) break;
    let card = node;
    for (let i = 0; i < 14 && card.parentElement; i++) {
      card = card.parentElement;
      if (/Sponsored/.test(card.textContent) && /Started running/.test(card.textContent)) break;
    }
    const txt = card.innerText || "";
    const started = (txt.match(/Started running on ([^\n]+)/) || [])[1] || null;
    const plats = [];
    card.querySelectorAll("[aria-label]").forEach(e => {
      const l = e.getAttribute("aria-label");
      if (PLATS.includes(l) && !plats.includes(l)) plats.push(l);
    });
    let landing = null;
    card.querySelectorAll("a[href]").forEach(a => {
      const h = a.href || "";
      if (!landing && h && !/facebook\.com|fb\.com|l\.facebook|\/ads\/library/.test(h))
        landing = h.split("?")[0];
    });
    const media = card.querySelector("video") ? "video"
                : (card.querySelectorAll('img[src*="scontent"]').length > 1 ? "carousel"
                : (card.querySelector('img[src*="scontent"]') ? "image" : null));
    const lines = txt.split("\n").map(s => s.trim()).filter(Boolean);
    const si = lines.indexOf("Sponsored");
    const page = si > 0 ? lines[si - 1] : null;
    // ad text: lines after "Sponsored" up to a CTA / uppercase landing domain
    let body = [];
    if (si >= 0) {
      for (let j = si + 1; j < lines.length; j++) {
        const ln = lines[j];
        if (CTA.has(ln) || /^[A-Z0-9][A-Z0-9.\-]+\.[A-Z]{2,}$/.test(ln)) break;
        if (/^Library ID:/.test(ln) || ln === "Sponsored") break;
        body.push(ln);
      }
    }
    out.push({
      ad_id: id,
      page_name: page,
      ad_text: body.join("\n").trim() || null,
      started_running: started,
      platforms: plats,
      media_type: media,
      landing_url: landing,
    });
  }
  return out;
})()
"""


def open_scout():
    """Attach to the scout browser, launching it first if needed (no login
    required — the Ad Library is public)."""
    try:
        import browser
    except Exception as e:
        die(f"Could not load the scout browser module: {e}")
    if not browser.is_running():
        warn("scout browser not running — launching it (no login needed for "
             "the Ad Library)...")
        browser.launch()
    return browser.attach()


def is_walled(tab):
    """Meta sometimes bounces automated hits to a login/checkpoint wall."""
    url = (tab.eval("location.href") or "").lower()
    return "login" in url or "checkpoint" in url or "/gettingstarted" in url


def collect_ads(tab, max_ads):
    """Scroll to load cards (jittered), then run the DOM extractor."""
    ads = []
    for scroll in range(6):
        ads = tab.eval(EXTRACTOR.replace("__MAX__", str(max_ads))) or []
        if len(ads) >= max_ads:
            break
        # load more
        before = tab.eval("document.querySelectorAll('a[href]').length")
        tab.eval("window.scrollTo(0, document.body.scrollHeight)")
        jitter_sleep(2, 5, "loading more Ad Library results")
        after = tab.eval("document.querySelectorAll('a[href]').length")
        if after == before and scroll > 0:
            break  # no more results loading
    return ads[:max_ads]


def run(url, max_ads, label):
    tab = open_scout().page(url, settle=8)
    try:
        title = tab.eval("document.title") or ""
        if is_walled(tab):
            die("Meta bounced the Ad Library to a login/checkpoint wall. Open "
                "the scout browser window (python scripts/browser.py launch), "
                "load facebook.com once to clear it, then retry.")
        if "Ad Library" not in title:
            die(f"The Ad Library didn't load as expected (page title: "
                f"'{title[:60]}'). Meta may be blocking — open the scout "
                "browser window to check, then retry.")
        ads = collect_ads(tab, max_ads)
    finally:
        tab.close()
    for a in ads:  # the permalink to this exact ad in the Ad Library
        a["ad_library_url"] = f"{LIB}?id={a['ad_id']}"
    return ads


def platforms_note(ads):
    """Meta renders the platform (FB/IG/…) icons as image sprites with no text
    or aria-label on the summary card, so they usually can't be read here.
    Say so rather than implying the ad runs nowhere."""
    if ads and not any(a.get("platforms") for a in ads):
        return ("platforms unavailable — Meta shows them only as icons on the "
                "summary card; open an ad's Ad Library URL to see them")
    return None


def cmd_search(args):
    cap = clamp_cap(args.max_ads, DEFAULT_MAX_ADS, HARD_MAX_ADS, "max-ads")
    q = urllib.parse.quote(args.query)
    url = (f"{LIB}?active_status=active&ad_type=all&country={args.country}"
           f"&q={q}&media_type=all")
    ads = run(url, cap, args.query)
    notes = [n for n in (platforms_note(ads),
                         None if ads else "No active ads matched. Try a "
                         "broader keyword or a different --country.") if n]
    emit({"query": args.query, "country": args.country,
          "count": len(ads), "ads": ads,
          **({"notes": notes} if notes else {})})


PAGE_ID_RE = re.compile(r"view_all_page_id=(\d+)")


def cmd_page(args):
    cap = clamp_cap(args.max_ads, DEFAULT_MAX_ADS, HARD_MAX_ADS, "max-ads")
    val = args.page.strip()
    base = f"{LIB}?active_status=active&ad_type=all&country={args.country}"
    if val.isdigit():
        url = f"{base}&view_all_page_id={val}&media_type=all"
        who = f"page_id {val}"
    elif PAGE_ID_RE.search(val):
        pid = PAGE_ID_RE.search(val).group(1)
        url = f"{base}&view_all_page_id={pid}&media_type=all"
        who = f"page_id {pid}"
    else:
        # A bare name (or facebook.com/name URL): the Ad Library has no exact
        # name→ads filter without the numeric page id, so search by keyword and
        # say so. For an exact page, pass its numeric ID or a view_all_page_id URL.
        name = re.sub(r"^https?://(www\.)?facebook\.com/", "", val).strip("/")
        name = name.split("/")[0].replace("-", " ")
        url = f"{base}&q={urllib.parse.quote(name)}&media_type=all"
        who = f"name '{name}'"
    ads = run(url, cap, who)
    by_id = val.isdigit() or bool(PAGE_ID_RE.search(val))
    notes = [n for n in (
        platforms_note(ads),
        None if by_id else ("Matched by name via keyword search — for an exact "
                            "advertiser pass its numeric page ID or a "
                            "view_all_page_id URL."),
        None if ads else "No active ads found for this advertiser.") if n]
    emit({"page": args.page, "resolved": who, "country": args.country,
          "count": len(ads), "ads": ads,
          **({"notes": notes} if notes else {})})


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="search ads by keyword")
    s.add_argument("query")
    s.add_argument("--country", default="US")
    s.add_argument("--max-ads", type=int, default=DEFAULT_MAX_ADS)
    s.set_defaults(func=cmd_search)

    pg = sub.add_parser("page", help="active ads from one advertiser")
    pg.add_argument("page", help="page name, facebook.com URL, or numeric page id")
    pg.add_argument("--country", default="US")
    pg.add_argument("--max-ads", type=int, default=DEFAULT_MAX_ADS)
    pg.set_defaults(func=cmd_page)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
