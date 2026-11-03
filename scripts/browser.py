#!/usr/bin/env python3
"""Scout browser: a dedicated Chrome profile driven over the DevTools protocol.

Why a dedicated profile instead of the user's normal Chrome: since Chrome 136
(2025) the --remote-debugging-port flag is ignored on the default profile for
security reasons, so attaching to a normal everyday Chrome is not possible.
Instead this module manages its own Chrome profile ("scout browser") stored
per-machine under the user's local app data. The user logs into their
dedicated SPARE Instagram/TikTok accounts in that window once; the session
persists in the profile like any normal Chrome, and scrapers attach to it
over CDP on localhost. Nothing here ever sees, stores, or transmits
credentials — login happens by the human, in the browser, exactly once.

CLI:
  python scripts/browser.py launch   Open (or reuse) the scout browser window
  python scripts/browser.py status   Show attach + per-platform login state
  python scripts/browser.py stop     Close the scout browser

Importable API (used by instagram.py / tiktok.py):
  attach() -> Browser          raises with remediation text if not running
  Browser.page(url)            open tab, wait for load + JS hydration
  Browser.require_login(...)   hard-fail with clear message if logged out
  Browser.cookies_txt(...)     temp Netscape cookies.txt for yt-dlp
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import CHROME_PROFILE_DIR, DATA_DIR, die, warn

try:
    import websocket
except ImportError:
    die("The 'websocket-client' package is missing. "
        "Run: pip install -r requirements.txt")

CDP_PORT = int(os.environ.get("SOCIAL_SCOUT_CDP_PORT", 9223))

# Human-check handoff: when a page lands on a CAPTCHA / "verify you are
# human" check, bring the scout window forward and wait for the PERSON to
# complete it — nothing here ever interacts with the check itself. Set
# SOCIAL_SCOUT_CHECK_WAIT=0 to skip waiting and fail fast instead.
CHECK_WAIT = int(os.environ.get("SOCIAL_SCOUT_CHECK_WAIT", 90))
# First path segment only (Instagram /challenge/, Facebook /checkpoint/,
# Google /sorry/) — "/tag/challenge" or ".../30_day_challenge/" on a normal
# page must not look like a check.
CHECK_PATH_SEGMENTS = {"challenge", "checkpoint", "sorry", "captcha",
                       "validatecaptcha"}
CHECK_URL_SUBSTRINGS = ("cdn-cgi/challenge-platform", "/errors/validatecaptcha")
# Two kinds of widget, both only when actually visible:
#  - GATE widgets are full-page challenges by definition (TikTok's slider,
#    PerimeterX, Amazon's captcha form, Cloudflare's challenge form).
#  - Generic CAPTCHA iframes (reCAPTCHA / hCaptcha / Turnstile) sit on plenty
#    of normal pages — Google's corner badge, contact forms — so they only
#    count when they aren't the badge/invisible kind AND the page is short,
#    i.e. the check IS the page.
CHECK_JS = r"""
(() => {
  const shown = e => { const r = e.getBoundingClientRect(); return r.width > 30 && r.height > 30; };
  const gate = Array.from(document.querySelectorAll(
      '#captcha-verify-image, .captcha_verify_container, #px-captcha, '
    + 'form[action*="validateCaptcha"], #challenge-form')).some(shown);
  const text = (document.body && document.body.innerText) || '';
  const generic = text.length < 2500 && Array.from(document.querySelectorAll(
      'iframe[src*="captcha"], iframe[src*="challenges.cloudflare.com"], iframe[title*="reCAPTCHA"]'))
    .some(e => shown(e) && !e.closest('.grecaptcha-badge, .grecaptcha-logo')
               && !/size=invisible/.test(e.src || ''));
  return {widget: gate || generic, url: location.href, text: text.slice(0, 4001)};
})()
"""
CHECK_PENDING_MSG = ("A security check is waiting in the scout browser (the "
                     "Chrome window). Please complete it there, then ask me "
                     "to try again — the browser usually stays trusted for a "
                     "while afterwards.")


class CheckPending(RuntimeError):
    """A verification check is still waiting for the person. Its tab is left
    OPEN on purpose so they can complete it — callers must not close it."""
LAUNCH_HELP = ("Scout browser is not running. Start it with:\n"
               "    python scripts/browser.py launch\n"
               "then log into your dedicated spare account(s) if you haven't "
               "on this machine yet.")

CHROME_CANDIDATES = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) /
            "Google/Chrome/Application/chrome.exe"),
    ],
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "linux": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
              "/usr/bin/chromium", "/usr/bin/chromium-browser"],
}


def find_chrome():
    for path in CHROME_CANDIDATES.get(sys.platform, CHROME_CANDIDATES["linux"]):
        if path and Path(path).exists():
            return path
    return None


def _http(path, method="GET", timeout=5):
    req = urllib.request.Request(f"http://127.0.0.1:{CDP_PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
    try:
        return json.loads(body)
    except ValueError:
        return body  # some endpoints (/json/close, /json/new errors) return text


def is_running():
    try:
        return "webSocketDebuggerUrl" in _http("/json/version")
    except OSError:
        return False


def launch(wait_seconds=25):
    """Start the scout Chrome if it isn't already running; return when CDP is up."""
    if is_running():
        return
    chrome = find_chrome()
    if not chrome:
        die("Google Chrome not found. Install Chrome, or set the path by "
            "editing CHROME_CANDIDATES in scripts/browser.py")
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.DETACHED_PROCESS |
                 subprocess.CREATE_NEW_PROCESS_GROUP)
    subprocess.Popen(
        [chrome,
         f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={CHROME_PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags if sys.platform == "win32" else 0,
        start_new_session=(sys.platform != "win32"))
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_running():
            return
        time.sleep(0.5)
    die(f"Chrome started but CDP on port {CDP_PORT} never came up. "
        "Close any stray scout Chrome windows and retry.")


class Tab:
    """One DevTools page target: navigate, evaluate JS, then close."""

    def __init__(self, target):
        self.id = target["id"]
        self.ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=60,
            suppress_origin=True)
        self._msg_id = 0
        self.keep_open = False   # set when a check is left for the person

    def send(self, method, params=None, timeout=60):
        self._msg_id += 1
        self.ws.send(json.dumps({"id": self._msg_id, "method": method,
                                 "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._msg_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            # interleaved events from the page are ignored
        raise TimeoutError(f"CDP {method} timed out")

    def eval(self, js, timeout=60):
        """Evaluate an expression in page context; returns its JSON value.
        Async expressions (await/fetch) are supported via awaitPromise."""
        res = self.send("Runtime.evaluate",
                        {"expression": js, "returnByValue": True,
                         "awaitPromise": True}, timeout=timeout)
        details = res.get("exceptionDetails")
        if details:
            text = (details.get("exception") or {}).get("description") \
                   or details.get("text", "JS error")
            raise RuntimeError(f"in-page JS failed: {text[:300]}")
        return res.get("result", {}).get("value")

    def navigate(self, url, settle=4.0, timeout=45, human_check=True):
        self.send("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval("document.readyState") == "complete":
                    break
            except RuntimeError:
                pass
            time.sleep(0.5)
        # modern SPAs render after readyState completes; give hydration a beat
        time.sleep(settle)
        if human_check:
            reason = self.check_reason()
            if reason and not self.wait_for_human(reason):
                self.keep_open = True
                raise CheckPending(CHECK_PENDING_MSG)

    def check_reason(self):
        """Why this page looks like a human-verification check, or None."""
        try:
            info = self.eval(CHECK_JS, timeout=15) or {}
        except (RuntimeError, TimeoutError, OSError):
            return None
        url = (info.get("url") or "").lower()
        segments = [s for s in urllib.parse.urlparse(url).path.split("/") if s]
        if ((segments and segments[0] in CHECK_PATH_SEGMENTS)
                or any(m in url for m in CHECK_URL_SUBSTRINGS)):
            return "verification page"
        if info.get("widget"):
            return "CAPTCHA"
        try:
            from obscura import looks_blocked   # shared bot-check text detector
        except Exception:
            return None
        return looks_blocked(info.get("text"))

    def bring_to_front(self):
        """Best effort: focus this tab and restore/raise its window. Windows
        may refuse to let a background app steal focus — the waiting message
        and the timeout cover that case."""
        try:
            self.send("Page.bringToFront", timeout=10)
            win = self.send("Browser.getWindowForTarget", {"targetId": self.id},
                            timeout=10)["windowId"]
            for state in ("minimized", "normal"):   # restore = raise on Windows
                self.send("Browser.setWindowBounds",
                          {"windowId": win, "bounds": {"windowState": state}},
                          timeout=10)
        except Exception:
            pass

    def wait_for_human(self, reason, wait=None):
        """Hand a verification check to the person at the keyboard: raise the
        scout window, then poll until the check is gone (True) or `wait`
        seconds pass (False). Never touches the check itself."""
        wait = CHECK_WAIT if wait is None else wait
        if wait <= 0:
            return False
        self.bring_to_front()
        warn(f"A security check ({reason}) appeared in the scout browser — "
             f"please complete it in the Chrome window. Waiting up to {wait}s...")
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(2)
            if not self.check_reason():
                warn("Check completed — thanks! Continuing.")
                time.sleep(2)   # let the real page finish loading
                return True
        return False

    def close(self):
        try:
            self.ws.close()
        except OSError:
            pass
        if self.keep_open:   # a check is waiting here for the person
            return
        try:
            _http(f"/json/close/{self.id}")
        except OSError:
            pass


class Browser:
    def page(self, url="about:blank", settle=4.0):
        target = _http(f"/json/new?{urllib.parse.urlencode({'url': 'about:blank'})}",
                       method="PUT")
        tab = Tab(target)
        if url != "about:blank":
            tab.navigate(url, settle=settle)
        return tab

    def all_cookies(self):
        tab = self.page()
        try:
            try:
                return tab.send("Network.getAllCookies")["cookies"]
            except RuntimeError:
                return tab.send("Storage.getCookies")["cookies"]
        finally:
            tab.close()

    def logged_in(self, domain, cookie_names=("sessionid",), value_ok=None):
        """A live session cookie for the domain is the login marker — no page
        scrape needed. IG and TikTok both name theirs 'sessionid' (the default);
        other platforms differ, so callers can pass the cookie name(s) and an
        optional value check (e.g. Pinterest sets '_auth=0' when logged out, so
        it needs value_ok=lambda v: v != '0')."""
        now = time.time()
        for c in self.all_cookies():
            if (domain in c.get("domain", "") and c.get("name") in cookie_names
                    and c.get("value")
                    and (value_ok is None or value_ok(c["value"]))
                    and (c.get("expires", -1) in (-1, 0) or c["expires"] > now)):
                return True
        return False

    def twitter_logged_in(self):
        """X's session token 'auth_token' is absent when logged out."""
        return (self.logged_in("x.com", ("auth_token",))
                or self.logged_in("twitter.com", ("auth_token",)))

    def pinterest_logged_in(self):
        """Pinterest sets '_auth=1' when logged in, '_auth=0' anonymously."""
        return self.logged_in("pinterest.com", ("_auth",),
                              value_ok=lambda v: v not in ("0", "false", ""))

    def require_login(self, domain, platform):
        if not self.logged_in(domain):
            die(f"No active {platform} login in the scout browser.\n"
                f"Open it with:  python scripts/browser.py launch\n"
                f"then log into your DEDICATED SPARE {platform} account "
                f"(never your personal one) and rerun this command.\n"
                f"This is a one-time step per machine.")

    def cookies_txt(self, domains):
        """Write cookies for the given domains to a temp Netscape-format file
        (what yt-dlp --cookies expects). Caller must delete it afterwards —
        it never lives inside the repo."""
        rows = ["# Netscape HTTP Cookie File"]
        for c in self.all_cookies():
            if not any(d in c.get("domain", "") for d in domains):
                continue
            rows.append("\t".join([
                c["domain"],
                "TRUE" if c["domain"].startswith(".") else "FALSE",
                c.get("path", "/"),
                "TRUE" if c.get("secure") else "FALSE",
                str(int(c["expires"]) if c.get("expires", -1) > 0 else 0),
                c["name"], c["value"]]))
        fd, path = tempfile.mkstemp(prefix="scout-cookies-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        return path


def attach():
    """Attach to the running scout browser or exit with remediation text."""
    if not is_running():
        die(LAUNCH_HELP)
    return Browser()


def cmd_status():
    if not is_running():
        print("scout browser : NOT RUNNING")
        print(LAUNCH_HELP)
        sys.exit(1)
    b = Browser()
    print(f"scout browser : RUNNING (CDP port {CDP_PORT})")
    print(f"profile       : {CHROME_PROFILE_DIR}")
    for domain, name in (("instagram.com", "Instagram"), ("tiktok.com", "TikTok")):
        state = "LOGGED IN" if b.logged_in(domain) else \
            "NOT LOGGED IN — log into your spare account in the scout window"
        print(f"{name:<13} : {state}")
    # Twitter/X and Pinterest work WITHOUT login (degraded); login just upgrades
    # them, so their "not logged in" message is worded as optional.
    tw = "LOGGED IN" if b.twitter_logged_in() else \
        "NOT LOGGED IN — optional; log into a spare X account for full tweet data"
    print(f"{'Twitter/X':<13} : {tw}")
    pin = "LOGGED IN" if b.pinterest_logged_in() else \
        "NOT LOGGED IN — optional; log in for full per-keyword trend data"
    print(f"{'Pinterest':<13} : {pin}")


def cmd_stop():
    if not is_running():
        print("scout browser was not running")
        return
    try:
        _http("/json/version")  # confirm reachable, then close via Browser target
        tab_list = _http("/json")
        for t in tab_list:
            _http(f"/json/close/{t['id']}")
    except OSError:
        pass
    print("scout browser closed (profile and logins are preserved)")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "launch":
        launch()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Scout browser running on CDP port {CDP_PORT}.")
        print(f"Profile: {CHROME_PROFILE_DIR}")
        print("If this is your first run on this machine: in that Chrome "
              "window, log into your DEDICATED SPARE Instagram and/or TikTok "
              "account now. The login persists for future runs.")
        cmd_status()
    elif cmd == "status":
        cmd_status()
    elif cmd == "stop":
        cmd_stop()
    else:
        die(f"unknown subcommand '{cmd}' (use launch | status | stop)")


if __name__ == "__main__":
    main()
