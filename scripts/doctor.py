#!/usr/bin/env python3
"""Health check for the social-scout skill.

Verifies that every backend both platforms depend on is reachable and working:
  1. Python version
  2. yt-dlp installed (binary or module)
  3. YouTube search (live query via yt-dlp)
  4. YouTube captions (live metadata fetch, checks caption tracks exist)
  5. Arctic Shift API (primary Reddit backend)
  6. Pullpush API (fallback Reddit backend)

Exit code 0 = both platforms usable, 1 = something core is broken.
Run:  python scripts/doctor.py
"""

import json
import shutil
import subprocess
import sys
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


def check_meta_ads():
    """Load the public Ad Library in the scout browser and confirm it renders.
    Needs the scout browser running (no Facebook login required)."""
    import browser
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
    ("Chrome installed", check_chrome_installed, "browser"),
    ("Scout browser (CDP)", check_scout_browser, "browser"),
    ("Instagram login", check_instagram_login, "instagram"),
    ("TikTok login", check_tiktok_login, "tiktok"),
    ("TikTok impersonation (yt-dlp)", check_tiktok_impersonation, "tiktok"),
    ("TikTok Creative Center (trending)", check_creative_center, "tiktok_cc"),
    ("Web reader", check_webread, "web"),
    ("Quora discovery (best-effort)", check_quora_discovery, "quora"),
    ("Amazon product pages", check_amazon, "amazon"),
    ("Meta Ad Library", check_meta_ads, "meta"),
    ("Google Trends (pytrends)", check_google_trends, "gtrends"),
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
    if all(reddit):
        lines.append(("Reddit", "READY"))
    elif any(reddit):
        lines.append(("Reddit", "READY (one backend down — fallback covers you, "
                                "but global keyword search needs Pullpush)"))
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
        lines.append(("Meta Ad Library", "READY (needs scout browser, no login)"))
    else:
        lines.append(("Meta Ad Library", "NOT READY — start the scout browser "
                                         "(public, no Facebook login needed)"))

    gt = r.get("gtrends", ["FAIL"])
    if all(s == "OK" for s in gt):
        lines.append(("Google Trends", "READY"))
    elif any(s == "FAIL" for s in gt):
        lines.append(("Google Trends", "NOT READY — pip install -r requirements.txt"))
    else:
        lines.append(("Google Trends", "RATE-LIMITED right now — use sparingly, retry later"))
    return lines, ok("youtube") and any(reddit) and ok("web")


def main():
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
