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

    def navigate(self, url, settle=4.0, timeout=45):
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

    def close(self):
        try:
            self.ws.close()
        except OSError:
            pass
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

    def logged_in(self, domain):
        """A live session cookie for the domain (IG and TikTok both call
        theirs 'sessionid') is the login marker — no page scrape needed."""
        now = time.time()
        for c in self.all_cookies():
            if (domain in c.get("domain", "") and c.get("name") == "sessionid"
                    and c.get("value")
                    and (c.get("expires", -1) in (-1, 0) or c["expires"] > now)):
                return True
        return False

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
