#!/usr/bin/env python3
"""General web page reading: one URL in, clean readable text out.

PRIMARY:  Jina Reader (https://r.jina.ai/<url>) — free tier, no API key,
          ~20 requests/minute per IP; strips nav/ads/scripts and returns
          markdown. Set JINA_API_KEY to raise the rate limit (optional).
SECOND:   the stealth browser (scripts/obscura.py) when it's installed —
          renders the page's JavaScript locally and returns markdown, so
          JS-heavy pages and Jina rate-limit hits still read. Skipped if not
          installed.
FALLBACK: direct fetch + stdlib HTML-to-text stripping (noisier but local).

Bot-check pages ("verify you are human", Cloudflare, ...) are never returned
as if they were the page's content — each tier treats them as a miss.

This is SINGLE-URL reading, not crawling: it does not follow links, index
sites, or search the web. No login or session is needed.

CLI:
  python scripts/webread.py read <url> [--max-chars 20000] [--local]
"""

import argparse
import html
import html.parser
import re
import sys
import urllib.error
import urllib.request

from common import die

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEFAULT_MAX_CHARS = 20000  # protects Claude's context window on huge pages


def _get(url, headers, timeout=90):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def read_jina(url):
    """Jina Reader sits behind Cloudflare, which challenges Python's urllib
    TLS fingerprint but passes curl — so prefer curl (bundled with Windows
    10+, macOS, and Linux) and fall back to urllib where curl is missing."""
    import os
    import shutil
    import subprocess
    key = os.environ.get("JINA_API_KEY")
    target = f"https://r.jina.ai/{url}"
    if shutil.which("curl"):
        cmd = ["curl", "-sS", "--fail", "-m", "90", target]
        if key:
            cmd[1:1] = ["-H", f"Authorization: Bearer {key}"]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        if proc.returncode != 0:
            raise OSError(f"curl to Jina failed: {(proc.stderr or '').strip()[:200]}")
        return proc.stdout
    headers = {"User-Agent": BROWSER_UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return _get(target, headers)


class _TextExtractor(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer",
            "aside", "form", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "article", "section", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.chunks.append(data)


def read_local(url):
    raw = _get(url, {"User-Agent": BROWSER_UA})
    p = _TextExtractor()
    p.feed(raw)
    text = "".join(p.chunks)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


JINA_PARTIAL = "This page maybe not yet fully loaded"


def _jina_body(text):
    """Jina prefixes Title/URL Source/Warning headers; compare content only."""
    _, sep, body = text.partition("Markdown Content:")
    return body if sep else text


def _blocked(text):
    """Bot-check page detector, shared with the stealth browser module."""
    try:
        from obscura import looks_blocked
    except Exception:
        return None
    return looks_blocked(text)


def read_obscura(url):
    """Stealth-browser tier. Returns markdown, or None when it isn't
    installed, fails, or lands on a bot-check page (the next tier runs)."""
    try:
        import obscura
        if not obscura.is_installed():
            return None
        text = obscura._tidy(obscura.fetch(url, dump="markdown"))
    except Exception:
        return None
    if not text.strip() or obscura.looks_blocked(text):
        return None
    return text


def read_url(url, max_chars=DEFAULT_MAX_CHARS, force_local=False):
    """Returns (text, engine). Raises on total failure — never silent-empty."""
    jina_error = None
    if not force_local:
        try:
            text = read_jina(url)
            if text.strip() and not _blocked(text):
                # Jina flags pages whose JavaScript hadn't finished; those
                # often miss the actual content, so let the stealth browser
                # render it too and keep whichever read got more.
                if JINA_PARTIAL in text:
                    rendered = read_obscura(url)
                    if rendered and len(rendered) > len(_jina_body(text)):
                        return _cap(rendered, max_chars), "stealth-browser"
                return _cap(text, max_chars), "jina-reader"
            jina_error = "bot-check page" if text.strip() else "empty response"
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            jina_error = str(e)
        text = read_obscura(url)
        if text:
            return _cap(text, max_chars), "stealth-browser"
    try:
        text = read_local(url)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise RuntimeError(
            f"Could not read {url}. "
            + (f"Jina Reader: {jina_error}. " if jina_error else "")
            + f"Direct fetch: {e}")
    reason = _blocked(text)
    if reason:
        raise RuntimeError(
            f"{url} only showed a {reason} page, not its content — the site "
            "is blocking automated reading right now.")
    if not text.strip():
        raise RuntimeError(
            f"{url} fetched but yielded no readable text "
            "(page may be fully JavaScript-rendered or blocked"
            + ("" if _obscura_installed() else
               " — installing the stealth browser with `python scripts/"
               "obscura.py install` lets JavaScript pages render")
            + ")")
    return _cap(text, max_chars), "local-fallback"


def _obscura_installed():
    try:
        import obscura
        return obscura.is_installed()
    except Exception:
        return False


def _cap(text, max_chars):
    if len(text) <= max_chars:
        return text
    return (text[:max_chars]
            + f"\n\n[truncated at {max_chars} chars of {len(text)}; "
              "rerun with --max-chars 0 for everything]")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("read", help="read one URL as clean text")
    r.add_argument("url")
    r.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                   help="0 = no cap")
    r.add_argument("--local", action="store_true",
                   help="skip Jina Reader, use direct fetch only")
    args = p.parse_args()

    max_chars = args.max_chars if args.max_chars > 0 else 10 ** 9
    try:
        text, engine = read_url(args.url, max_chars, args.local)
    except RuntimeError as e:
        die(str(e))
    print(f"# {args.url}")
    print(f"# via: {engine}")
    print()
    print(text)


if __name__ == "__main__":
    main()
