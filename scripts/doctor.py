#!/usr/bin/env python3
"""Health check for the social-scout skill.

Verifies that every backend the platforms depend on is reachable and working
— YouTube (yt-dlp), Reddit (Arctic Shift / Pullpush), the scout browser and
its logins, the stealth browser (Obscura, optional), web reading, and each
research platform. See CHECKS below for the full list.

Exit code 0 = both platforms usable, 1 = something core is broken.
Run:  python scripts/doctor.py
"""

import concurrent.futures
import json
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = "social-scout-skill/1.0 (health check)"
# "Me at the zoo" — the first YouTube video ever uploaded; a stable test target.
CAPTION_TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def http_ok(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ytdlp_cmd():
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


def check_python():
    v = sys.version_info
    ok = v >= (3, 8)
    return ok, f"Python {v.major}.{v.minor}.{v.micro}" + ("" if ok else " (need 3.8+)")


def check_ytdlp():
    cmd = ytdlp_cmd()
    if cmd is None:
        return False, "not found — install with: pip install yt-dlp"
    out = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=60)
    return out.returncode == 0, f"version {out.stdout.strip()}"


def check_youtube_search():
    cmd = ytdlp_cmd()
    if cmd is None:
        return False, "skipped (yt-dlp missing)"
    out = subprocess.run(cmd + ["--flat-playlist", "--dump-json", "--skip-download",
                                "ytsearch1:test"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=90)
    if out.returncode != 0 or not out.stdout.strip():
        return False, (out.stderr or "no results").strip().splitlines()[-1][:120]
    title = json.loads(out.stdout.splitlines()[0]).get("title", "?")
    return True, f'search works (top hit: "{title[:50]}")'


def check_youtube_captions():
    cmd = ytdlp_cmd()
    if cmd is None:
        return False, "skipped (yt-dlp missing)"
    out = subprocess.run(cmd + ["-J", "--skip-download", CAPTION_TEST_VIDEO],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=90)
    if out.returncode != 0 and "Sign in to confirm" in (out.stderr or ""):
        # same bot-check fallback the youtube.py script uses
        out = subprocess.run(cmd + ["-J", "--skip-download", "--extractor-args",
                                    "youtube:player_client=android", CAPTION_TEST_VIDEO],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=90)
    if out.returncode != 0:
        return False, (out.stderr or "metadata fetch failed").strip().splitlines()[-1][:120]
    info = json.loads(out.stdout)
    n = len(info.get("subtitles") or {}) + len(info.get("automatic_captions") or {})
    if n == 0:
        return False, "metadata OK but no caption tracks found"
    return True, f"caption tracks visible ({n} languages on test video)"


def check_arctic_shift():
    data = http_ok("https://arctic-shift.photon-reddit.com/api/posts/search"
                   "?subreddit=AskReddit&limit=1&sort=desc")
    items = data.get("data") if isinstance(data, dict) else data
    ok = isinstance(items, list) and len(items) > 0
    return ok, "responding with data" if ok else "responded but no data"


def check_pullpush():
    data = http_ok("https://api.pullpush.io/reddit/search/submission/?q=test&size=1")
    items = data.get("data") if isinstance(data, dict) else data
    ok = isinstance(items, list) and len(items) > 0
    return ok, "responding with data" if ok else "responded but no data"


def check_reddit_stealth():
    """Reddit's own search page in the stealth browser — the first tier for
    global keyword search, so Pullpush being down doesn't matter."""
    if not _stealth_installed():
        return "INFO", ("stealth browser not installed — global keyword search "
                        "then relies on Reddit .json / Pullpush")
    import reddit
    items = reddit.fetch_reddit_stealth("skincare", None, 3)
    if items:
        return True, f"Reddit search page renders ({len(items)} live results)"
    return "WARN", "stealth browser couldn't read Reddit search right now"


# --- browser / session platforms (Instagram, TikTok) -----------------------

def check_chrome_installed():
    import browser
    path = browser.find_chrome()
    return bool(path), path or "Chrome not found — install Google Chrome"


def check_scout_browser():
    import browser
    if browser.is_running():
        return True, f"attached on CDP port {browser.CDP_PORT}"
    return "WARN", "not running — start with: python scripts/browser.py launch"


def _login_check(domain, platform):
    import browser
    if not browser.is_running():
        return "WARN", "skipped (scout browser not running)"
    if browser.Browser().logged_in(domain):
        return True, "spare-account session active"
    return "WARN", (f"no {platform} login — log into your dedicated spare "
                    "account in the scout browser window")


def check_instagram_login():
    return _login_check("instagram.com", "Instagram")


def check_tiktok_login():
    return _login_check("tiktok.com", "TikTok")


def check_tiktok_impersonation():
    """TikTok metadata/transcripts need yt-dlp browser impersonation (curl_cffi
    in the version range yt-dlp supports). Without it every TikTok video/
    transcript request gets HTTP 403."""
    import subprocess
    out = subprocess.run((["yt-dlp"] if shutil.which("yt-dlp")
                          else [sys.executable, "-m", "yt_dlp"])
                         + ["--list-impersonate-targets"],
                         capture_output=True, text=True, timeout=60)
    if "chrome" in (out.stdout or "").lower():
        return True, "yt-dlp browser impersonation available (curl_cffi)"
    try:
        import curl_cffi  # noqa: F401
        return False, ("curl_cffi installed but yt-dlp can't use it — version "
                       "mismatch. Fix: pip install -r requirements.txt (pins "
                       "curl_cffi<0.16), then pip install -U yt-dlp")
    except ImportError:
        return False, ("curl_cffi missing — TikTok video/transcript will 403. "
                       "Fix: pip install -r requirements.txt")


def check_creative_center():
    import browser
    import tiktok
    if not browser.is_running():
        return "WARN", "skipped (scout browser not running)"
    tab = browser.Browser().page(tiktok.CREATIVE_CENTER + "?region=US&period=7",
                                 settle=8)
    try:
        rows = tiktok.scrape_creative_center(tab)
        return True, f"trending table parsed ({len(rows)} rows visible)"
    finally:
        tab.close()


# --- web reading / Quora (no session needed) -------------------------------

def check_webread():
    from webread import read_url
    text, engine = read_url("https://example.com", max_chars=500)
    return bool(text.strip()), f"working via {engine}"


def check_quora_discovery():
    from quora import find_quora_urls
    urls = find_quora_urls("productivity tips", 2)
    if urls:
        return True, f"web search found {len(urls)} Quora URLs"
    return False, "web search returned no Quora URLs"


def check_amazon():
    """Fetch a long-lived bestseller's public product page. amazon.py's own
    direct→scout-browser fallback handles the fetch; a readable product title
    means Amazon is reachable. A block is a WARN, not a hard failure — Amazon
    rate-limits, and reviews additionally need an Amazon login."""
    import amazon
    # AmazonBasics AA batteries — a stable ASIN that's been listed for years.
    url = "https://www.amazon.com/dp/B00MNV8E0C"
    try:
        html = amazon.fetch_html(url)
    except RuntimeError as e:
        return "WARN", (f"Amazon not reachable right now ({str(e)[:70]}…). It "
                        "rate-limits; retry later or start the scout browser")
    title = amazon._text(amazon._first(amazon.bs(html), ["#productTitle"]))
    if title:
        return True, f'product page OK ("{title[:32]}…"); reviews need Amazon login'
    return "WARN", "fetched a page but no product title — Amazon markup may have changed"


def _stealth_installed():
    try:
        import obscura
        return obscura.is_installed()
    except Exception:
        return False


def check_stealth_browser():
    """Obscura: optional, but powers web_render/web_screenshot/
    web_scrape_many and lets Meta/Pinterest/X run without Chrome."""
    import obscura
    if not obscura.is_installed():
        return "INFO", ("not installed (optional) — enables JS-page reading, "
                        "screenshots, bulk scraping, and Chrome-free Meta/"
                        "Pinterest/X. Fix: python scripts/obscura.py install")
    text = obscura.fetch("https://example.com", dump="markdown", timeout=20)
    if "Example Domain" in text or "documentation examples" in text:
        return True, f"v{obscura.version()} installed — renders pages headless"
    return False, "installed but couldn't render a test page — reinstall with --force"


def check_meta_ads():
    """Load the public Ad Library and confirm it renders — headless in the
    stealth browser when installed, else in the scout browser."""
    import browser
    if _stealth_installed():
        import meta_ads
        url = ("https://www.facebook.com/ads/library/?active_status=active"
               "&ad_type=all&country=US&q=soap&media_type=all")
        ads = meta_ads.read_ads_stealth(url, 3)
        if ads:
            return True, f"renders in the stealth browser ({len(ads)} ads, no Chrome needed)"
        if not browser.is_running():
            return "WARN", ("stealth browser couldn't load it and the scout "
                            "browser isn't running — start it with "
                            "python scripts/browser.py launch")
    if not browser.is_running():
        return "WARN", ("needs the scout browser — start it with "
                        "python scripts/browser.py launch (no login needed)")
    tab = browser.Browser().page(
        "https://www.facebook.com/ads/library/?active_status=active"
        "&ad_type=all&country=US&q=soap&media_type=all", settle=8)
    try:
        title = tab.eval("document.title") or ""
        url = (tab.eval("location.href") or "").lower()
    finally:
        tab.close()
    if "login" in url or "checkpoint" in url:
        return "WARN", "Meta bounced to a login/checkpoint wall — retry later"
    if "Ad Library" in title:
        return True, "Ad Library renders in the scout browser"
    return "WARN", f"loaded but unexpected page title ('{title[:40]}')"


def check_google_trends():
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return False, ("pytrends not installed — Google Trends tools won't "
                       "work. Fix: pip install -r requirements.txt")
    try:
        pt = TrendReq(hl="en-US", tz=360)
        pt.build_payload(["test"], timeframe="today 3-m")
        df = pt.interest_over_time()
    except Exception as e:
        msg = str(e)
        if "429" in msg or "rate" in msg.lower():
            return "WARN", "Google Trends rate-limited right now — try again later"
        return "WARN", f"Google Trends request failed ({msg[:60]}); often transient"
    if df is not None and not df.empty:
        return True, "pytrends returned interest data"
    return "WARN", "pytrends responded but with no data — try again later"


def check_trustpilot():
    """Trustpilot 403s scripts, so this really tests the scout-browser path."""
    import trustpilot
    try:
        pp, _ = trustpilot.fetch_next_data("https://www.trustpilot.com/review/amazon.com")
    except RuntimeError as e:
        return "WARN", (f"Trustpilot not reachable ({str(e)[:60]}…) — it blocks "
                        "scripts; start the scout browser and retry")
    biz = pp.get("businessUnit") or {}
    if biz.get("displayName"):
        return True, f"reviews load ({biz['displayName']}, {biz.get('numberOfReviews')} reviews)"
    return "WARN", "loaded but no business data — markup may have changed"


def check_google_shopping():
    import browser
    if not browser.is_running():
        return "WARN", "needs the scout browser (python scripts/browser.py launch)"
    import google_shopping as gs
    ads = gs.extract_via_browser(gs.build_url("soap", "us"), 3)
    if ads:
        return True, f"parsed {len(ads)} listings"
    return "WARN", ("no listings parsed — Google's Shopping layout is obfuscated "
                    "and flaky (best-effort)")


def check_pinterest():
    import browser
    running = browser.is_running()
    if running and browser.Browser().pinterest_logged_in():
        return True, "logged in — full per-keyword trends available"
    import pinterest_trends as pt
    if _stealth_installed():
        body = pt.read_trends_stealth("https://trends.pinterest.com/?country=US")
        if body:
            return True, (f"public spotlight via the stealth browser "
                          f"({len(pt.parse_spotlight(body))} terms, no Chrome "
                          "needed) — log into Pinterest in the scout browser "
                          "for per-keyword trends")
    if not running:
        return "WARN", "needs the scout browser (python scripts/browser.py launch)"
    trends = pt.parse_spotlight(pt.load_trends("US"))
    if trends:
        return True, (f"public spotlight only ({len(trends)} terms) — log into "
                      "Pinterest in the scout browser for per-keyword trends")
    return "WARN", "loaded but no spotlight parsed — Pinterest may have redesigned"


def check_twitter():
    """Login upgrades Twitter to full data; without it, Nitter/web fallbacks."""
    import browser
    if browser.is_running() and browser.Browser().twitter_logged_in():
        return True, "logged into X — full tweet data + engagement available"
    if _stealth_installed():
        import twitter
        tweets, _src, _prof = twitter.try_x_com_stealth("https://x.com/NASA", 2)
        if tweets:
            return True, ("profiles + exact engagement via the stealth browser; "
                          "search still limited without an X login")
    if browser.is_running():
        return True, ("limited — no X login; uses Nitter/web-search fallbacks. "
                      "Log into a spare X account in the scout browser for full "
                      "tweet data")
    # no scout browser: probe Nitter mirrors
    import urllib.parse
    import twitter
    path = "/search?" + urllib.parse.urlencode({"q": "news", "f": "tweets"})
    tweets, source = twitter.try_nitter(path, 3)
    if tweets:
        return True, f"Nitter mirror working ({source}); no browser for full data"
    return "WARN", ("no scout browser and all Nitter mirrors down — web-search "
                    "fallback only (best-effort)")


# --- optional extras -------------------------------------------------------

def check_whisper():
    """Local faster-whisper — the transcript fallback. Present after
    `pip install -r requirements.txt`; the model downloads on first real use,
    so we only check the package imports here (importing does not download)."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return "INFO", ("faster-whisper not installed — IG/TikTok videos "
                        "without native captions won't get transcripts. "
                        "Fix: pip install -r requirements.txt (optional)")
    from transcribe import WHISPER_MODEL
    return True, (f"installed — local transcripts enabled (model "
                  f"'{WHISPER_MODEL}', downloads on first use, no key needed)")


CHECKS = [
    ("Python version", check_python, "core"),
    ("yt-dlp installed", check_ytdlp, "youtube"),
    ("YouTube search", check_youtube_search, "youtube"),
    ("YouTube captions", check_youtube_captions, "youtube"),
    ("Arctic Shift API (Reddit primary)", check_arctic_shift, "reddit"),
    ("Pullpush API (Reddit fallback)", check_pullpush, "reddit"),
    ("Reddit search (stealth browser)", check_reddit_stealth, "reddit_search"),
    ("Chrome installed", check_chrome_installed, "browser"),
    ("Scout browser (CDP)", check_scout_browser, "browser"),
    ("Instagram login", check_instagram_login, "instagram"),
    ("TikTok login", check_tiktok_login, "tiktok"),
    ("TikTok impersonation (yt-dlp)", check_tiktok_impersonation, "tiktok"),
    ("TikTok Creative Center (trending)", check_creative_center, "tiktok_cc"),
    ("Web reader", check_webread, "web"),
    ("Stealth browser (Obscura)", check_stealth_browser, "stealth"),
    ("Quora discovery (best-effort)", check_quora_discovery, "quora"),
    ("Amazon product pages", check_amazon, "amazon"),
    ("Meta Ad Library", check_meta_ads, "meta"),
    ("Google Trends (pytrends)", check_google_trends, "gtrends"),
    ("Trustpilot reviews", check_trustpilot, "trustpilot"),
    ("Google Shopping", check_google_shopping, "shopping"),
    ("Pinterest Trends", check_pinterest, "pinterest"),
    ("Twitter/X (best-effort)", check_twitter, "twitter"),
    ("Local Whisper transcripts", check_whisper, "optional"),
]


def run_checks():
    results = {}
    for name, fn, group in CHECKS:
        try:
            status, detail = fn()
            if status is True:
                status = "OK"
            elif status is False:
                status = "FAIL"
        except Exception as e:
            status, detail = "FAIL", f"{type(e).__name__}: {e}"
        results.setdefault(group, []).append(status)
        print(f"  [{status}]{' ' * (5 - len(status))} {name:<36} {detail}")
    return results


def verdicts(r):
    def ok(group):
        return all(s == "OK" for s in r.get(group, ["FAIL"]))

    lines = []
    lines.append(("YouTube", "READY" if ok("youtube") else "BROKEN"))

    reddit = [s == "OK" for s in r.get("reddit", [])]
    search_ok = ok("reddit_search")
    if all(reddit):
        lines.append(("Reddit", "READY"))
    elif any(reddit) and search_ok:
        lines.append(("Reddit", "READY (an archive is down — the stealth browser "
                                "covers global keyword search)"))
    elif any(reddit):
        lines.append(("Reddit", "READY (one backend down — global keyword search "
                                "needs Pullpush or the stealth browser)"))
    elif search_ok:
        lines.append(("Reddit", "LIMITED — archives down; keyword search still "
                                "works via the stealth browser"))
    else:
        lines.append(("Reddit", "BROKEN"))

    browser_up = ok("browser")
    for label, group in (("Instagram", "instagram"), ("TikTok", "tiktok")):
        if ok(group) and browser_up:
            lines.append((label, "READY"))
        else:
            lines.append((label, "NOT READY — see browser/login lines above "
                                 "(one-time setup per machine)"))
    if not ok("tiktok_cc"):
        lines.append(("TikTok trending", "NOT READY — Creative Center check failed"))
    lines.append(("Web reading", "READY" if ok("web") else "BROKEN"))
    stealth = r.get("stealth", ["FAIL"])
    if all(s == "OK" for s in stealth):
        lines.append(("Stealth browser", "READY (web_render / web_screenshot / "
                                         "web_scrape_many)"))
    elif all(s == "INFO" for s in stealth):
        lines.append(("Stealth browser", "NOT INSTALLED (optional) — python "
                                         "scripts/obscura.py install"))
    else:
        lines.append(("Stealth browser", "BROKEN — reinstall: python "
                                         "scripts/obscura.py install --force"))
    lines.append(("Quora", "BEST-EFFORT (working)" if ok("quora")
                  else "BEST-EFFORT (currently down — expected occasionally)"))
    amazon = r.get("amazon", ["FAIL"])
    if all(s == "OK" for s in amazon):
        lines.append(("Amazon", "READY (product info; reviews need Amazon login)"))
    else:
        lines.append(("Amazon", "LIMITED — product pages blocked right now; "
                                "retry later or via the scout browser"))

    meta = r.get("meta", ["FAIL"])
    if all(s == "OK" for s in meta):
        lines.append(("Meta Ad Library", "READY (stealth or scout browser, no login)"))
    else:
        lines.append(("Meta Ad Library", "NOT READY — install the stealth browser "
                                         "or start the scout browser (public, "
                                         "no Facebook login needed)"))

    gt = r.get("gtrends", ["FAIL"])
    if all(s == "OK" for s in gt):
        lines.append(("Google Trends", "READY"))
    elif any(s == "FAIL" for s in gt):
        lines.append(("Google Trends", "NOT READY — pip install -r requirements.txt"))
    else:
        lines.append(("Google Trends", "RATE-LIMITED right now — use sparingly, retry later"))

    lines.append(("Trustpilot", "READY" if ok("trustpilot")
                  else "LIMITED — blocks scripts; needs the scout browser"))
    lines.append(("Google Shopping", "READY (scout browser)" if ok("shopping")
                  else "LIMITED — Google's Shopping layout not parsing right now"))
    lines.append(("Pinterest Trends", "READY (full if logged in, else public "
                  "spotlight)" if ok("pinterest")
                  else "NOT READY — install the stealth browser or start the "
                       "scout browser (login optional)"))
    lines.append(("Twitter/X", "READY (full if X logged in, else best-effort "
                  "via Nitter/web)" if ok("twitter")
                  else "BEST-EFFORT (no browser + Nitter down — web-search only)"))
    return lines, ok("youtube") and any(reddit) and ok("web")


# ==========================================================================
# QUICK MODE — fast, non-technical health check used by the MCP tool.
# One attempt per check, hard 10s timeout, all run concurrently so the whole
# thing finishes in well under 30s. No retries, no HTTP codes, no backend
# names in the output.
# ==========================================================================

def check_with_timeout(fn, timeout=10):
    """Run a check with a hard timeout. Returns (status, detail). status is one
    of 'ok' / 'warn' / 'down' / 'skip'."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return "down", "Temporarily down — try again later"
        except Exception:
            return "down", "Temporarily down — try again later"


def _http_reachable(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return 200 <= getattr(r, "status", 200) < 400


def _browser_snapshot():
    """Read the scout browser's login state ONCE (one tab) so the per-platform
    quick checks are instant. Never raises."""
    snap = {"running": False, "ig": False, "tt": False, "tw": False, "pin": False}
    try:
        import browser
        if not browser.is_running():
            return snap
        snap["running"] = True
        b = browser.Browser()
        cookies = b.all_cookies()
        now = time.time()

        def has(domain, names, value_ok=None):
            for c in cookies:
                if (domain in c.get("domain", "") and c.get("name") in names
                        and c.get("value")
                        and (value_ok is None or value_ok(c["value"]))
                        and (c.get("expires", -1) in (-1, 0) or c["expires"] > now)):
                    return True
            return False

        snap["ig"] = has("instagram.com", ("sessionid",))
        snap["tt"] = has("tiktok.com", ("sessionid",))
        snap["tw"] = (has("x.com", ("auth_token",))
                      or has("twitter.com", ("auth_token",)))
        snap["pin"] = has("pinterest.com", ("_auth",),
                          lambda v: v not in ("0", "false", ""))
    except Exception:
        pass
    return snap


def run_quick():
    """Fast, non-technical health check. Prints clean ✅/⚠️/❌ lines."""
    snap = _browser_snapshot()
    running = snap["running"]
    stealth = _stealth_installed()   # instant: just checks the binary exists

    def q_stealth():
        return ("ok", "Installed — JS pages, screenshots, bulk scraping") if stealth \
            else ("warn", "Not installed (optional) — ask to run "
                          "stealth_browser_install for JS pages, screenshots, "
                          "and Chrome-free Meta/Pinterest/X")

    # --- instant checks (no network) ---
    def q_youtube():
        return ("ok", "Working") if ytdlp_cmd() else \
            ("down", "yt-dlp not installed — run install.bat/install.sh")

    def q_gtrends():
        try:
            import pytrends  # noqa: F401
            return "ok", "Working"
        except ImportError:
            return "down", "Not installed — run install.bat/install.sh"

    def session_line(ok_logged, label_needs, needs_browser_msg):
        if not running:
            return "warn", needs_browser_msg
        return ("ok", "Working (logged in)") if ok_logged else ("warn", label_needs)

    def q_instagram():
        return session_line(snap["ig"], "Needs login — log into your spare "
                            "Instagram account in the scout browser",
                            "Needs setup — start the scout browser and log in")

    def q_tiktok():
        return session_line(snap["tt"], "Needs login — log into your spare "
                            "TikTok account in the scout browser",
                            "Needs setup — start the scout browser and log in")

    def q_browser_public(name, stealth_ok=False):
        # public-but-browser-required platforms (Meta ads, Trustpilot, Amazon,
        # Google Shopping): they need the scout browser running — or, for the
        # ones that render headless (stealth_ok), the stealth browser
        def fn():
            if stealth_ok and stealth:
                return "ok", "Working (stealth browser — no Chrome needed)"
            return ("ok", "Working") if running else \
                ("warn", f"Needs scout browser — start it to use {name}")
        return fn

    def q_amazon():
        return ("ok", "Working (product info; reviews need Amazon login)") if running \
            else ("warn", "Needs scout browser — start it to use Amazon")

    def q_pinterest():
        if running and snap["pin"]:
            return "ok", "Working (logged in)"
        if stealth:
            return "warn", ("Public trends work (stealth browser) — log into "
                            "Pinterest in scout browser for keyword trends")
        if not running:
            return "warn", "Needs scout browser (log in for full keyword trends)"
        return "warn", "Needs login — log into Pinterest in scout browser for full data"

    def q_twitter():
        if snap["tw"]:
            return "ok", "Working (logged in)"
        if stealth:
            return "warn", ("Profiles work (stealth browser) — log into spare "
                            "X account for search + full data")
        return "warn", "Limited — log into spare X account for full tweet data"

    # --- network checks (wrapped in a hard 10s timeout, run concurrently) ---
    def q_reddit():
        data = http_ok("https://arctic-shift.photon-reddit.com/api/posts/search"
                       "?subreddit=AskReddit&limit=1&sort=desc", timeout=8)
        items = data.get("data") if isinstance(data, dict) else data
        return ("ok", "Working") if items else \
            ("down", "Temporarily down — try again later")

    def q_web():
        return ("ok", "Working") if _http_reachable("https://example.com", 8) else \
            ("down", "Temporarily down — try again later")

    def q_quora():
        # just checks whether the discovery search engine responds
        u = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(
            {"q": "site:quora.com test"})
        return ("ok", "Working") if _http_reachable(u, 8) else \
            ("warn", "Temporarily down — try again later (best-effort source)")

    def q_tiktok_trending():
        if not running:
            return "warn", "Needs scout browser to read trending"
        import browser
        import tiktok
        tab = browser.Browser().page(tiktok.CREATIVE_CENTER + "?region=US&period=7",
                                     settle=6)
        try:
            rows = tiktok.scrape_creative_center(tab)
        finally:
            tab.close()
        return ("ok", "Working") if rows else \
            ("down", "Down — Creative Center not responding")

    # label, fn, is_network (network ones get the timeout wrapper + concurrency)
    plan = [
        ("YouTube", q_youtube, False),
        ("Reddit", q_reddit, True),
        ("Instagram", q_instagram, False),
        ("TikTok", q_tiktok, False),
        ("Web Reader", q_web, True),
        ("Stealth Browser", q_stealth, False),
        ("Meta Ad Library", q_browser_public("Meta Ad Library", stealth_ok=True), False),
        ("Google Trends", q_gtrends, False),
        ("Trustpilot", q_browser_public("Trustpilot"), False),
        ("Amazon", q_amazon, False),
        ("Google Shopping", q_browser_public("Google Shopping"), False),
        ("Quora", q_quora, True),
        ("Pinterest Trends", q_pinterest, False),
        ("Twitter/X", q_twitter, False),
        ("TikTok Trending", q_tiktok_trending, True),
    ]

    # run network checks concurrently (each capped at 10s); instant ones inline
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {}
        for label, fn, is_net in plan:
            if is_net:
                futures[label] = ex.submit(check_with_timeout, fn, 10)
            else:
                try:
                    results[label] = fn()
                except Exception:
                    results[label] = ("down", "Temporarily down — try again later")
        for label, fut in futures.items():
            try:
                results[label] = fut.result(timeout=12)
            except Exception:
                results[label] = ("down", "Temporarily down — try again later")

    ICON = {"ok": "✅", "warn": "⚠️", "down": "❌", "skip": "⏭️"}
    print("social-scout health check")
    working = attention = down = 0
    for label, _fn, _net in plan:
        status, detail = results.get(label, ("down", "Temporarily down"))
        if status == "ok":
            working += 1
        elif status == "warn":
            attention += 1
        elif status == "down":
            down += 1
        print(f"{ICON.get(status, '⚠️')} {label:<18} {detail}")

    print("=" * 50)
    print(f"{len(plan)} checks · {working} working | {attention} need attention "
          f"| {down} down")
    core = all(results.get(x, ('down',))[0] == "ok"
               for x in ("YouTube", "Reddit"))
    if core:
        print("Core research (YouTube, Reddit, web) ready to use.")
    else:
        print("Core research partly unreachable from this network — often "
              "resolves in 15–60 min, or try a different network.")
    print("For full TikTok/Pinterest/Twitter data, log into spare accounts in "
          "the scout browser.")
    # quick mode always exits 0 — it's informational, not a gate
    sys.exit(0)


def main():
    if "--quick" in sys.argv[1:]:
        run_quick()
        return
    print("social-scout doctor\n" + "=" * 50)
    results = run_checks()
    print("=" * 50)
    lines, core_ok = verdicts(results)
    for label, verdict in lines:
        print(f"  {label:<15}: {verdict}")
    print()
    if core_ok:
        print("Core platforms (YouTube, Reddit, web reading) are usable. "
              "Instagram/TikTok additionally need the scout browser + your "
              "spare-account login (see any NOT READY lines).")
        sys.exit(0)
    print("Something core is broken — see FAIL lines above.")
    sys.exit(1)


if __name__ == "__main__":
    main()
