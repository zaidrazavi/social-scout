#!/usr/bin/env python3
"""Stealth browser: Obscura, a lightweight headless browser (Rust + V8).

Why a second browser next to the scout browser: the scout browser is a real
Chrome window that holds the spare-account logins (Instagram, TikTok, ...).
Obscura is the opposite — a ~30 MB headless engine with no window and no
login, built for public pages: it runs the page's JavaScript, randomizes its
fingerprint, blocks trackers, and can turn the page into Markdown or a PNG.
It lets the toolkit read JavaScript-heavy sites that plain fetching returns
empty, and run the public-but-JS-rendered scrapers (Meta Ad Library,
Pinterest Trends, logged-out x.com) without opening Chrome.

What it can NOT do: get past login walls, CAPTCHAs, or "verify you are human"
pages. Logins stay in the scout browser (see browser.py).

The binary lives per-machine under the user's local app data (never in the
repo). `install` downloads the pinned release from GitHub and verifies its
SHA-256 against the digest GitHub publishes.

CLI:
  python scripts/obscura.py install              one-time download (~73 MB)
  python scripts/obscura.py status               installed? which version?
  python scripts/obscura.py render <url>         JS-rendered page as markdown
        [--format markdown|text|html|links] [--extract CSS] [--js EXPR]
        [--wait-for CSS] [--wait SECS] [--max-chars N] [--use-scout-login]
  python scripts/obscura.py screenshot <url>     PNG of the rendered page
        [--full-page] [--width 1280] [--height 800] [--wait SECS]

  render / screenshot relay: if the page is a bot check, they reopen it in
  the scout browser (real Chrome), which hands the check to YOU and carries
  on once you've completed it. --no-chrome-fallback stops instead.
  python scripts/obscura.py scrape <url> ...     many pages in parallel -> JSON
        [--file urls.txt] [--extract CSS] [--js EXPR] [--max-chars 1500]

Proxy (optional, off by default): set SOCIAL_SCOUT_PROXY, e.g.
  socks5://127.0.0.1:1080  or  http://user:pass@host:port
and every Obscura request goes through it.

Importable API (used by webread.py / meta_ads.py / pinterest_trends.py /
twitter.py):
  is_installed() -> bool
  fetch(url, dump="markdown", ...) -> str          one page, CLI engine
  Session() -> .page(url, settle) -> Tab           CDP engine; Tab has the
                                                   same eval/navigate/close
                                                   interface as browser.Tab
  looks_blocked(text) -> reason | None             bot-check page detector
"""

import argparse
import atexit
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from common import DATA_DIR, DOWNLOADS_DIR, clamp_cap, die, emit, warn

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Pinned to the release this integration was tested against. Override with
# `install --version latest` (or a tag) to try a newer one.
PINNED_VERSION = "v0.2.6"
REPO = "h4ckf0r0day/obscura"

BIN_DIR = DATA_DIR / "bin"                       # per-machine, never in repo
EXE = "obscura.exe" if sys.platform == "win32" else "obscura"
SCREENSHOT_DIR = DOWNLOADS_DIR / "screenshots"   # gitignored

DEFAULT_MAX_CHARS = 20000   # same context-window guard as webread.py
DEFAULT_SCRAPE_CHARS = 1500
DEFAULT_SCRAPE_URLS = 30
HARD_MAX_SCRAPE_URLS = 100
MAX_SCREENSHOT_HEIGHT = 8000   # Claude rejects images taller than this
MAX_IMAGE_BYTES = 4_500_000    # keep MCP image payloads under ~5 MB

INSTALL_HELP = ("The stealth browser (Obscura) isn't installed on this machine "
                "yet. Install it once with:\n"
                "    python scripts/obscura.py install\n"
                "(a ~73 MB download from GitHub, checked against its published "
                "fingerprint).")

# The platform tools own these logins; copying their session cookies into a
# different browser fingerprint risks the spare accounts getting flagged, so
# --use-scout-login refuses them and points at the dedicated tool instead.
PROTECTED_LOGIN_DOMAINS = ("instagram.com", "tiktok.com", "facebook.com",
                           "x.com", "twitter.com", "pinterest.com", "amazon.")

# Bot-check / interstitial pages. Obscura can't (and shouldn't) get past these;
# spotting them lets callers fall back instead of returning the wall as content.
BLOCK_MARKERS = (
    ("unusual traffic from your computer network", "Google bot check"),
    ("performing security verification", "security check"),
    ("verifying your connection", "security check"),
    ("enable javascript and cookies to continue", "security check"),
    ("checking your browser", "security check"),
    ("just a moment...", "security check"),
    ("verify you are human", "human verification"),
    ("you've been blocked by network security", "network block"),
    ("click the button below to continue shopping", "Amazon bot check"),
    ("attention required! | cloudflare", "security check"),
    ("access denied", "access denied"),
    ("support your browser", "browser not supported"),   # Trustpilot's wall
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# --------------------------------------------------------------------------
# locating / installing the binary
# --------------------------------------------------------------------------
def find_obscura():
    """OBSCURA_BIN override -> our per-machine bin dir -> PATH."""
    env = os.environ.get("OBSCURA_BIN")
    if env and Path(env).is_file():
        return env
    local = BIN_DIR / EXE
    if local.is_file():
        return str(local)
    return shutil.which("obscura")


def is_installed():
    return find_obscura() is not None


def require_bin():
    path = find_obscura()
    if not path:
        die(INSTALL_HELP)
    return path


def version():
    path = find_obscura()
    if not path:
        return None
    try:
        proc = subprocess.run([path, "--version"], capture_output=True,
                              text=True, timeout=15, creationflags=_NO_WINDOW)
        return (proc.stdout or "").strip().replace("obscura ", "") or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _asset_name():
    import platform
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    if sys.platform == "win32":
        if arch != "x86_64":
            die("The stealth browser has no Windows-on-ARM build yet.")
        return "obscura-x86_64-windows-stealth.zip"
    if sys.platform == "darwin":
        return f"obscura-{arch}-macos-stealth.tar.gz"
    return f"obscura-{arch}-linux-stealth.tar.gz"


def _github_json(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "social-scout-installer"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def install(tag=PINNED_VERSION, force=False):
    """Download the render+stealth build for this OS, verify its SHA-256
    against GitHub's published digest, and unpack obscura + obscura-worker
    into the per-machine bin dir."""
    if is_installed() and not force:
        return {"installed": True, "already": True, "path": find_obscura(),
                "version": version()}
    api = (f"https://api.github.com/repos/{REPO}/releases/latest" if tag == "latest"
           else f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
    try:
        release = _github_json(api)
    except (urllib.error.URLError, OSError, ValueError) as e:
        die(f"Couldn't reach GitHub to download the stealth browser ({e}). "
            "Check your internet connection and try again.")
    want = _asset_name()
    asset = next((a for a in release.get("assets", []) if a["name"] == want), None)
    if not asset:
        die(f"The stealth browser release {release.get('tag_name')} has no "
            f"download for this computer ({want}).")
    digest = asset.get("digest") or ""
    digest = digest[len("sha256:"):] if digest.startswith("sha256:") else ""
    if not digest:
        die("GitHub didn't publish a fingerprint for this download, so it "
            "can't be verified. Not installing.")

    sys.stderr.write(f"[social-scout] downloading {want} "
                     f"({asset['size'] / 1e6:.0f} MB) from GitHub...\n")
    tmp = Path(tempfile.mkdtemp(prefix="obscura-install-"))
    try:
        archive = tmp / want
        req = urllib.request.Request(asset["browser_download_url"],
                                     headers={"User-Agent": "social-scout-installer"})
        sha = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=120) as r, open(archive, "wb") as f:
            while chunk := r.read(1 << 20):
                sha.update(chunk)
                f.write(chunk)
        if sha.hexdigest() != digest:
            die("The downloaded file didn't match GitHub's published fingerprint "
                "— it may be corrupted. Nothing was installed; try again.")

        out = tmp / "unpacked"
        if want.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(out)
        else:
            with tarfile.open(archive) as t:
                try:
                    t.extractall(out, filter="data")
                except TypeError:   # Python < 3.11.4 has no extraction filters
                    t.extractall(out)
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        names = {EXE, EXE.replace("obscura", "obscura-worker")}
        found = [p for p in out.rglob("*") if p.is_file() and p.name in names]
        if len(found) < 2:
            die("The download didn't contain the expected program files. "
                "Nothing was installed.")
        for p in found:
            dest = BIN_DIR / p.name
            shutil.copy2(p, dest)
            if sys.platform != "win32":
                dest.chmod(0o755)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"installed": True, "already": False, "path": str(BIN_DIR / EXE),
            "version": version(), "release": release.get("tag_name"),
            "sha256": digest}


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def _global_args(stealth=True, proxy=None):
    """Flags that go BEFORE the subcommand (Obscura's global options)."""
    args = ["--stealth"] if stealth else []
    proxy = proxy or os.environ.get("SOCIAL_SCOUT_PROXY")
    if proxy:
        args += ["--proxy", proxy]
    return args


def _check_url(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise RuntimeError(f"'{url}' isn't a web address — it needs to start "
                           "with http:// or https://")
    return url


def friendly_error(raw, url):
    """Turn Obscura's network error text into plain language (no codes)."""
    msg = _friendly_error(raw, url)
    if os.environ.get("SOCIAL_SCOUT_PROXY") and "find the website" not in msg:
        msg += (" A proxy is set (SOCIAL_SCOUT_PROXY) — check it's running "
                "and reachable, or unset it to connect directly.")
    return msg


def _friendly_error(raw, url):
    low = (raw or "").lower()
    if "dns error" in low or "no such host" in low or "name or service not known" in low:
        return f"Couldn't find the website {url} — check the address is right."
    if "private/internal ip" in low or "ssrf" in low:
        return (f"{url} points at a local or private network address, which "
                "the stealth browser blocks for safety.")
    if "timed out" in low or "timeout" in low or "deadline" in low:
        return f"{url} took too long to load. The site may be slow or down — try again."
    if "certificate" in low or "tls" in low or "handshake" in low:
        return f"{url} has a security-certificate problem, so it couldn't be opened safely."
    if "connection refused" in low or "connect" in low or "reset" in low:
        return f"{url} refused the connection or isn't responding right now."
    return f"Couldn't load {url} — the site didn't respond normally."


def looks_blocked(text):
    """Return a short reason if `text` is a bot-check / interstitial page
    rather than real content, else None. Only short pages count: a long
    article that merely mentions 'access denied' is still an article."""
    t = (text or "").strip()
    if not t or len(t) > 4000:
        return None
    low = t.lower()
    for marker, reason in BLOCK_MARKERS:
        if marker in low:
            return reason
    return None


def _cap(text, max_chars):
    if len(text) <= max_chars:
        return text
    return (text[:max_chars]
            + f"\n\n[truncated at {max_chars} chars of {len(text)}; "
              "rerun with --max-chars 0 for everything]")


# --------------------------------------------------------------------------
# CLI engine: one-shot fetch
# --------------------------------------------------------------------------
def fetch(url, dump="markdown", wait_for=None, wait=None, wait_until="load",
          timeout=30, stealth=True, proxy=None):
    """Render one page and return Obscura's dump (markdown/text/html/links).
    Raises RuntimeError with a plain-language message on failure."""
    _check_url(url)
    cmd = ([require_bin()] + _global_args(stealth, proxy)
           + ["fetch", url, "--dump", dump, "--quiet",
              "--timeout", str(timeout), "--wait-until", wait_until])
    if wait_for:
        cmd += ["--selector", wait_for]
    if wait is not None:
        cmd += ["--wait", str(wait)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout + (wait or 5) + 30,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{url} took too long to load. The site may be slow "
                           "or down — try again.")
    if proc.returncode != 0:
        raise RuntimeError(friendly_error(proc.stderr or proc.stdout, url))
    return proc.stdout


# --------------------------------------------------------------------------
# CDP engine: a short-lived `obscura serve` driven like the scout browser
# --------------------------------------------------------------------------
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _windows_fonts():
    if sys.platform != "win32":
        return None
    d = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    return str(d) if d.is_dir() else None


class Tab:
    """One Obscura page. Same surface as browser.Tab (send / eval / navigate
    / close) so existing in-page extractors run on either engine unchanged.
    Obscura only speaks the flattened-session flavor of CDP, so commands go
    over the shared browser socket tagged with this page's sessionId."""

    def __init__(self, session, target_id, session_id):
        self._s = session
        self.id = target_id
        self.sid = session_id

    def send(self, method, params=None, timeout=60):
        return self._s._send(method, params, self.sid, timeout)

    def eval(self, js, timeout=60):
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
        _check_url(url)
        try:
            res = self.send("Page.navigate", {"url": url}, timeout=timeout)
        except TimeoutError:
            raise RuntimeError(f"{url} took too long to load. The site may be "
                               "slow or down — try again.")
        except RuntimeError as e:
            raise RuntimeError(friendly_error(str(e), url))
        if res.get("errorText"):
            raise RuntimeError(friendly_error(res["errorText"], url))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval("document.readyState") == "complete":
                    break
            except RuntimeError:
                pass
            time.sleep(0.5)
        time.sleep(settle)   # let SPAs hydrate, same as the scout browser

    def set_viewport(self, width, height):
        set_viewport(self, width, height)

    def screenshot(self, full_page=False):
        return capture(self, full_page)

    def set_cookies(self, cookies):
        if cookies:
            self.send("Network.setCookies", {"cookies": cookies})

    def close(self):
        try:
            self._s._send("Target.closeTarget", {"targetId": self.id}, None, 10)
        except Exception:
            pass


class Session:
    """A private `obscura serve` on a free localhost port for the life of one
    script run. Starts in well under a second; killed on close/exit."""

    def __init__(self, stealth=True, proxy=None, fonts=False, ready_timeout=20):
        import websocket  # websocket-client, already a requirement
        self._websocket = websocket
        self.port = _free_port()
        cmd = ([require_bin()] + _global_args(stealth, proxy)
               + ["serve", "--port", str(self.port), "--quiet"])
        font_dir = _windows_fonts() if fonts else None
        if font_dir:   # without these, CJK/Arabic render as boxes in PNGs
            cmd += ["--font-dir", font_dir]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,
                                      creationflags=_NO_WINDOW)
        atexit.register(self.close)
        self._msg_id = 0
        self._ws = None
        deadline = time.time() + ready_timeout
        ws_url = None
        while time.time() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=2) as r:
                    ws_url = json.loads(r.read().decode("utf-8"))["webSocketDebuggerUrl"]
                break
            except (OSError, ValueError, KeyError):
                time.sleep(0.2)
        if not ws_url:
            self.close()
            raise RuntimeError("The stealth browser didn't start. Try "
                               "`python scripts/obscura.py install --force` "
                               "to reinstall it.")
        self._ws = websocket.create_connection(ws_url, timeout=90,
                                               suppress_origin=True)

    def _send(self, method, params=None, session_id=None, timeout=60):
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._ws.settimeout(max(1.0, deadline - time.time()))
            try:
                reply = json.loads(self._ws.recv())
            except self._websocket.WebSocketTimeoutException:
                break
            if reply.get("id") == self._msg_id:
                if "error" in reply:
                    raise RuntimeError(f"CDP {method}: {reply['error'].get('message')}")
                return reply.get("result", {})
            # events and other sessions' traffic are ignored
        raise TimeoutError(f"CDP {method} timed out")

    def page(self, url="about:blank", settle=4.0, timeout=45):
        target = self._send("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = self._send("Target.attachToTarget",
                         {"targetId": target, "flatten": True})["sessionId"]
        tab = Tab(self, target, sid)
        if url != "about:blank":
            try:
                tab.navigate(url, settle=settle, timeout=timeout)
            except Exception:
                tab.close()
                raise
        return tab

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except OSError:
                pass
            self._ws = None
        proc, self._proc = getattr(self, "_proc", None), None
        if proc is not None and proc.poll() is None:
            if sys.platform == "win32":   # take the worker child down too
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, creationflags=_NO_WINDOW)
            else:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def set_viewport(tab, width, height):
    """Works on a stealth-browser or scout-browser tab."""
    tab.send("Emulation.setDeviceMetricsOverride",
             {"width": width, "height": height, "deviceScaleFactor": 1,
              "mobile": False})


def capture(tab, full_page=False):
    """PNG of a stealth-browser or scout-browser tab. Full-page captures are
    clipped at MAX_SCREENSHOT_HEIGHT. Returns (png, (width, height), clipped)."""
    params = {"format": "png"}
    clipped = False
    if full_page:
        metrics = tab.send("Page.getLayoutMetrics")
        size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        width = int(size.get("width") or 0) or None
        height = int(size.get("height") or 0) or None
        params["captureBeyondViewport"] = True
        if width and height and height > MAX_SCREENSHOT_HEIGHT:
            params["clip"] = {"x": 0, "y": 0, "width": width,
                              "height": MAX_SCREENSHOT_HEIGHT, "scale": 1}
            clipped = True
    data = base64.b64decode(tab.send("Page.captureScreenshot", params,
                                     timeout=90)["data"])
    return data, _png_size(data), clipped


def _png_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None, None


# --------------------------------------------------------------------------
# scout-browser cookies (opt-in): read pages on sites YOU are logged into
# --------------------------------------------------------------------------
def scout_cookies_for(url):
    """Cookies the scout browser holds for `url`'s site, in CDP setCookies
    form. Refuses the platform logins the dedicated tools manage."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if any(host == d or host.endswith("." + d) or (d.endswith(".") and d in host)
           for d in PROTECTED_LOGIN_DOMAINS):
        raise RuntimeError(
            f"Copying your {host} login into the stealth browser is blocked to "
            "keep that spare account safe. Use the dedicated tool for this "
            "platform instead (it uses the scout browser directly).")
    import browser
    if not browser.is_running():
        raise RuntimeError("use_scout_login needs the scout browser running "
                           "with you logged into this site. Start it with: "
                           "python scripts/browser.py launch")
    # A browser sends a cookie to its own domain and that domain's subdomains
    # — copy exactly those (www.example.com gets .example.com cookies), never
    # every cookie sharing a suffix like .co.uk.
    out = []
    for c in browser.Browser().all_cookies():
        dom = c.get("domain", "").lstrip(".").lower()
        if not dom or not (host == dom or host.endswith("." + dom)):
            continue
        ck = {k: c[k] for k in ("name", "value", "domain", "path", "secure",
                                "httpOnly", "sameSite") if k in c}
        if c.get("expires", -1) > 0:
            ck["expires"] = c["expires"]
        out.append(ck)
    return out


# --------------------------------------------------------------------------
# in-page extractors
# --------------------------------------------------------------------------
# Synchronous on purpose: Obscura's `scrape` --eval doesn't await promises.
SUMMARY_JS = r"""
(() => {
  const meta = n => { const e = document.querySelector(
      `meta[name="${n}"],meta[property="${n}"]`); return e ? e.getAttribute("content") : null; };
  const text = ((document.body && document.body.innerText) || "")
      .replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  return JSON.stringify({
    title: document.title || null,
    description: meta("description") || meta("og:description"),
    h1: Array.from(document.querySelectorAll("h1")).map(e => e.innerText.trim())
          .filter(Boolean).slice(0, 3),
    canonical: (document.querySelector("link[rel=canonical]") || {}).href || null,
    image: meta("og:image"),
    word_count: text ? text.split(/\s+/).length : 0,
    text: text.slice(0, __MAX__),
  });
})()
"""

EXTRACT_JS = r"""
(() => JSON.stringify(Array.from(document.querySelectorAll(__SEL__)).slice(0, 200)
  .map(e => { const o = {text: (e.innerText || e.textContent || "").trim()};
              if (e.href) o.href = e.href; if (e.src) o.src = e.src; return o; })
  .filter(o => o.text || o.href || o.src)))()
"""


def _extract_js(selector):
    return EXTRACT_JS.replace("__SEL__", json.dumps(selector))


def _user_js(expr):
    """For `scrape`: Obscura's batch --eval is synchronous, so a promise here
    would serialize as {} — callers reject async expressions first."""
    return f"JSON.stringify(({expr}))"


def _user_js_awaited(expr):
    """For the CDP engine: awaits promises, so async expressions work."""
    return f"(async () => JSON.stringify(await ({expr})))()"


def _is_async(expr):
    return any(k in expr for k in ("async", "await", "fetch(", ".then(", "Promise"))


def _tidy(text):
    """Obscura's markdown keeps the page's indentation whitespace; collapse
    whitespace-only lines so the reader (and Claude's context) gets content."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]*(\n[ \t]*)+", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_install(args):
    result = install(args.version, args.force)
    if result.get("already"):
        result["note"] = "Already installed — pass --force to reinstall."
    emit(result)


def cmd_status(args):
    path = find_obscura()
    emit({"installed": bool(path), "path": path, "version": version(),
          "proxy": bool(os.environ.get("SOCIAL_SCOUT_PROXY")),
          **({} if path else {"how_to_install": "python scripts/obscura.py install"})})
    if not path:
        sys.exit(1)


BODY_TEXT_JS = "(document.body && document.body.innerText) || ''"
LINKS_JS = r"""
(() => JSON.stringify(Array.from(document.querySelectorAll('a[href]')).slice(0, 500)
  .map(a => ({url: a.href, text: (a.innerText || '').trim() || null}))))()
"""


# --------------------------------------------------------------------------
# relay: stealth browser hit a bot check -> scout browser + the human
# --------------------------------------------------------------------------
def _blocked_msg(url, reason):
    return (f"{url} showed a {reason} page instead of its content. The stealth "
            "browser can't get past those — try web_read, or this platform's "
            "dedicated tool if there is one.")


def _scout_tab(url, settle, timeout, before_nav=None):
    """Open `url` in the scout browser (real Chrome, launched if needed).
    Its navigate() hands any verification check to the person at the
    keyboard and raises a plain-language RuntimeError if nobody completes it
    in time. Nothing here interacts with the check itself."""
    import browser
    if not browser.is_running():
        warn("opening the scout browser (Chrome) for this page...")
        browser.launch()
    tab = browser.Browser().page()
    try:
        if before_nav:
            before_nav(tab)
        tab.navigate(url, settle=settle, timeout=timeout)
    except Exception:
        tab.close()   # leaves the tab open if a check is waiting for the person
        raise
    return tab


def _relay_note(reason):
    warn(f"the stealth browser got a {reason} page — opening it in the scout "
         "browser (Chrome) instead, where you can complete any check...")


def _page_payload(tab, args, max_chars, engine, cookies=None):
    """Extraction / JS / text result for one loaded tab, on either engine."""
    body = tab.eval(BODY_TEXT_JS) or ""
    payload = {"url": args.url, "landed_on": tab.eval("location.href"),
               "title": tab.eval("document.title"), "engine": engine}
    if args.use_scout_login and engine == "stealth browser":
        payload.update({"used_scout_login": True, "cookies_copied": len(cookies or [])})
        if not cookies:
            payload["note"] = ("The scout browser has no login saved for this "
                               "site, so the page was read logged out. Log "
                               "into the site in the scout browser window "
                               "first, then retry.")
    if args.extract:
        items = json.loads(tab.eval(_extract_js(args.extract)) or "[]")
        payload.update({"selector": args.extract, "count": len(items), "items": items})
    if args.js:
        raw = tab.eval(_user_js_awaited(args.js))
        payload["result"] = json.loads(raw) if isinstance(raw, str) else raw
    if not args.extract and not args.js:
        payload["text"] = _cap(_tidy(body), max_chars)
    return payload


def _render_via_scout(args, reason, max_chars):
    if args.no_chrome_fallback:
        die(_blocked_msg(args.url, reason))
    _relay_note(reason)
    tab = _scout_tab(args.url, settle=args.wait if args.wait is not None else 3,
                     timeout=args.timeout)
    try:
        body = tab.eval(BODY_TEXT_JS) or ""
        still = looks_blocked(body)
        if still:
            die(f"{args.url} shows a {still} page in the scout browser too. "
                "Open the Chrome window, complete the check there, then retry.")
        if args.extract or args.js or args.use_scout_login:
            emit(_page_payload(tab, args, max_chars, "scout browser"))
        elif args.format == "links":
            links = json.loads(tab.eval(LINKS_JS) or "[]")
            emit({"url": args.url, "engine": "scout browser",
                  "count": len(links), "links": links})
        else:
            out = (tab.eval("document.documentElement.outerHTML") or ""
                   if args.format == "html" else _tidy(body))
            print(f"# {args.url}")
            print("# via: scout browser (Chrome)"
                  + ("" if args.format != "markdown" else
                     " — plain text; markdown needs the stealth browser"))
            print()
            print(_cap(out, max_chars))
    finally:
        tab.close()


def cmd_render(args):
    max_chars = args.max_chars if args.max_chars > 0 else 10 ** 9
    proxy = args.proxy
    needs_cdp = bool(args.extract or args.js or args.use_scout_login)
    if not needs_cdp:
        out = fetch(args.url, dump=args.format, wait_for=args.wait_for,
                    wait=args.wait, wait_until=args.wait_until,
                    timeout=args.timeout, proxy=proxy)
        if args.format == "links":
            links = []
            for line in out.splitlines():
                href, _, text = line.partition("\t")
                if href.strip():
                    links.append({"url": href.strip(), "text": text.strip() or None})
            emit({"url": args.url, "engine": "stealth browser",
                  "count": len(links), "links": links[:500]})
            return
        reason = looks_blocked(out)
        if reason:
            return _render_via_scout(args, reason, max_chars)
        if not out.strip():
            die(f"{args.url} loaded but had no readable content.")
        print(f"# {args.url}")
        print("# via: stealth browser (obscura)")
        print()
        print(_cap(_tidy(out) if args.format != "html" else out.strip(), max_chars))
        return

    # extraction / custom JS / logged-in reads need the CDP engine
    cookies = scout_cookies_for(args.url) if args.use_scout_login else None
    payload = reason = None
    with Session(proxy=proxy) as s:
        tab = s.page()
        try:
            if cookies:
                tab.set_cookies(cookies)
            tab.navigate(args.url, settle=args.wait if args.wait is not None else 2,
                         timeout=args.timeout)
            if args.wait_for:
                for _ in range(int(args.timeout * 2)):
                    if tab.eval(f"!!document.querySelector({json.dumps(args.wait_for)})"):
                        break
                    time.sleep(0.5)
            reason = looks_blocked(tab.eval(BODY_TEXT_JS) or "")
            if not reason:
                payload = _page_payload(tab, args, max_chars, "stealth browser", cookies)
        finally:
            tab.close()
    if reason:
        return _render_via_scout(args, reason, max_chars)
    emit(payload)


def _shoot_scout(args, width, height):
    """Screenshot relay: same capture, in the scout browser."""
    tab = _scout_tab(args.url, settle=args.wait, timeout=args.timeout,
                     before_nav=lambda t: set_viewport(t, width, height))
    try:
        title = tab.eval("document.title")
        body = tab.eval(BODY_TEXT_JS) or ""
        png, size, clipped = capture(tab, full_page=args.full_page)
        tab.send("Emulation.clearDeviceMetricsOverride")
    finally:
        tab.close()
    return title, body, png, size, clipped


def cmd_screenshot(args):
    width = max(320, min(args.width, 2560))
    height = max(240, min(args.height, 2000))
    engine = "stealth browser"
    with Session(proxy=args.proxy, fonts=True) as s:
        tab = s.page()
        try:
            tab.set_viewport(width, height)
            tab.navigate(args.url, settle=args.wait, timeout=args.timeout)
            title = tab.eval("document.title")
            body = tab.eval(BODY_TEXT_JS) or ""
            png, (w, h), clipped = tab.screenshot(full_page=args.full_page)
        finally:
            tab.close()
    reason = looks_blocked(body)
    if reason and not args.no_chrome_fallback:
        _relay_note(reason)
        title, body, png, (w, h), clipped = _shoot_scout(args, width, height)
        engine = "scout browser"
    if args.out:
        path = Path(args.out)
    else:
        host = (urllib.parse.urlparse(args.url).hostname or "page").replace("www.", "")
        path = SCREENSHOT_DIR / f"{host}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    notes = []
    if clipped:
        notes.append(f"full page was taller than {MAX_SCREENSHOT_HEIGHT}px — "
                     "captured the top part only")
    reason = looks_blocked(body)
    if reason:
        notes.append(f"the site showed a {reason} page — the screenshot is of that page")
    emit({"url": args.url, "title": title, "path": str(path),
          "width": w, "height": h, "bytes": len(png),
          "full_page": bool(args.full_page), "engine": engine,
          **({"notes": notes} if notes else {})})


def cmd_scrape(args):
    urls = list(args.urls)
    if args.file:
        lines = Path(args.file).read_text(encoding="utf-8").splitlines()
        urls += [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    urls = list(dict.fromkeys(urls))   # de-dupe, keep order
    if not urls:
        die("Give at least one URL to scrape.")
    cap = clamp_cap(len(urls), DEFAULT_SCRAPE_URLS, HARD_MAX_SCRAPE_URLS, "urls")
    if len(urls) > cap:
        urls = urls[:cap]
    bad = [u for u in urls if urllib.parse.urlparse(u).scheme not in ("http", "https")]
    if bad:
        die(f"These aren't web addresses (need http:// or https://): {', '.join(bad[:5])}")

    if args.js and _is_async(args.js):
        die("scrape runs JavaScript synchronously, so async code (await / "
            "fetch / promises) would come back empty. Use a synchronous "
            "expression here, or web_render for one page with async code.")
    if args.js:
        js, mode = _user_js(args.js), "js"
    elif args.extract:
        js, mode = _extract_js(args.extract), "extract"
    else:
        js, mode = SUMMARY_JS.replace("__MAX__", str(max(100, args.max_chars))), "summary"
    concurrency = max(1, min(args.concurrency, 10))
    cmd = ([require_bin()] + _global_args(True, args.proxy)
           + ["scrape", *urls, "--eval", " ".join(js.split()),
              "--concurrency", str(concurrency), "--format", "json",
              "--timeout", str(args.timeout), "--quiet"])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=args.timeout * (len(urls) // concurrency + 2) + 60,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        die("Scraping took too long overall. Try fewer URLs at once.")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        die("The stealth browser didn't return results. "
            + (friendly_error(proc.stderr, urls[0]) if proc.stderr else ""))

    results = []
    for r in data.get("results", []):
        row = {"url": r.get("url")}
        err = r.get("error")
        raw = r.get("eval")
        if err or raw is None:
            row.update({"ok": False,
                        "error": friendly_error(err or "", r.get("url")) if err
                        else "loaded, but the page returned nothing to extract"})
            if r.get("title"):
                row["title"] = r["title"]
            results.append(row)
            continue
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            value = raw
        if mode == "summary" and isinstance(value, dict):
            reason = looks_blocked(value.get("text"))
            row.update({"ok": not reason, **value})
            if reason:
                row["blocked"] = f"the site showed a {reason} page"
        else:
            row.update({"ok": True, "title": r.get("title"),
                        ("items" if mode == "extract" else "result"): value})
        results.append(row)
    emit({"count": len(results), "ok": sum(1 for r in results if r.get("ok")),
          "engine": "stealth browser", "mode": mode,
          "total_time_ms": data.get("total_time_ms"), "results": results})


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("install", help="download + verify the stealth browser")
    i.add_argument("--version", default=PINNED_VERSION,
                   help=f"release tag or 'latest' (default {PINNED_VERSION})")
    i.add_argument("--force", action="store_true", help="reinstall even if present")
    i.set_defaults(func=cmd_install)

    st = sub.add_parser("status", help="is the stealth browser installed?")
    st.set_defaults(func=cmd_status)

    def common_page_args(sp):
        sp.add_argument("url")
        sp.add_argument("--timeout", type=int, default=30)
        sp.add_argument("--proxy", help="override SOCIAL_SCOUT_PROXY for this run")
        sp.add_argument("--no-chrome-fallback", action="store_true",
                        help="on a bot-check page, stop instead of reopening "
                             "the page in the scout browser for you to pass")

    r = sub.add_parser("render", help="render one page (markdown by default)")
    common_page_args(r)
    r.add_argument("--format", choices=["markdown", "text", "html", "links"],
                   default="markdown")
    r.add_argument("--extract", metavar="CSS", help="return the text/links of "
                   "every element matching this CSS selector")
    r.add_argument("--js", metavar="EXPR", help="evaluate a JavaScript expression "
                   "(promises are awaited) and return its JSON value")
    r.add_argument("--wait-for", metavar="CSS", help="wait for this selector to appear")
    r.add_argument("--wait", type=float, default=None,
                   help="extra settle seconds after load")
    r.add_argument("--wait-until", choices=["load", "domcontentloaded", "networkidle0"],
                   default="load")
    r.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="0 = no cap")
    r.add_argument("--use-scout-login", action="store_true",
                   help="copy this site's cookies from the scout browser first "
                        "(for pages you're logged into there)")
    r.set_defaults(func=cmd_render)

    sh = sub.add_parser("screenshot", help="PNG of the rendered page")
    common_page_args(sh)
    sh.add_argument("--full-page", action="store_true")
    sh.add_argument("--width", type=int, default=1280)
    sh.add_argument("--height", type=int, default=800)
    sh.add_argument("--wait", type=float, default=2.0, help="settle seconds after load")
    sh.add_argument("--out", help="output .png path (default: downloads/screenshots/)")
    sh.set_defaults(func=cmd_screenshot)

    sc = sub.add_parser("scrape", help="many URLs in parallel -> JSON")
    sc.add_argument("urls", nargs="*")
    sc.add_argument("--file", help="newline-separated URL list")
    sc.add_argument("--extract", metavar="CSS")
    sc.add_argument("--js", metavar="EXPR", help="synchronous JavaScript expression")
    sc.add_argument("--max-chars", type=int, default=DEFAULT_SCRAPE_CHARS,
                    help="page-text excerpt length per URL (summary mode)")
    sc.add_argument("--concurrency", type=int, default=5, help="1-10 (default 5)")
    sc.add_argument("--timeout", type=int, default=45, help="per-page seconds")
    sc.add_argument("--proxy")
    sc.set_defaults(func=cmd_scrape)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
