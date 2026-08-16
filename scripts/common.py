#!/usr/bin/env python3
"""Shared utilities for the social-scout skill: throttling, backoff, caps, paths.

Design intent (see README "Rate limiting"): the login-walled platforms
(Instagram, TikTok) must never see machine-gun request patterns from a real
user session. Every scraper that talks to them routes its pacing through
these helpers so behavior stays uniform:

- randomized jitter between requests (never a fixed interval),
- exponential backoff when the platform pushes back,
- conservative per-invocation caps, overridable per call but with a ceiling,
- visible "[throttle]" messages so the user always knows when and why we wait.

This reduces ban risk; it does not eliminate it — which is why dedicated
spare accounts are mandatory for the session-based platforms.
"""

import json
import os
import random
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252, which crashes on emoji-laden content.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- caps ------------------------------------------------------------------
# Defaults are deliberately conservative. IG/TikTok serve ~10-20 comments per
# paginated response, so a 30-comment default means only 2-3 extra requests
# per post — indistinguishable from a human scrolling a comment section for a
# few seconds. The hard ceiling of 100 exists so a typo like --max-comments
# 5000 can't turn one invocation into an obvious bot signature.
DEFAULT_MAX_COMMENTS = int(os.environ.get("SOCIAL_SCOUT_MAX_COMMENTS", 30))
HARD_MAX_COMMENTS = 100
DEFAULT_MAX_POSTS = int(os.environ.get("SOCIAL_SCOUT_MAX_POSTS", 5))
HARD_MAX_POSTS = 20

# --- paths -----------------------------------------------------------------
SKILL_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = SKILL_DIR / "downloads"          # gitignored

if sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "social-scout"
else:
    DATA_DIR = Path.home() / ".social-scout"
CHROME_PROFILE_DIR = DATA_DIR / "chrome-profile"  # per-machine, never in repo


def clamp_cap(value, default, ceiling, what):
    """Clamp a user-supplied cap to the hard ceiling, telling them if we did."""
    if value is None:
        return default
    if value > ceiling:
        warn(f"--{what} {value} exceeds the safety ceiling; using {ceiling} "
             "(see README: rate limiting)")
        return ceiling
    return max(value, 1)


# --- pacing ----------------------------------------------------------------
def jitter_sleep(lo=2.0, hi=6.0, reason="pacing requests"):
    """Random human-ish pause between successive platform requests."""
    delay = random.uniform(lo, hi)
    sys.stderr.write(f"[throttle] waiting {delay:.1f}s ({reason})\n")
    time.sleep(delay)


def backoff_retry(fn, what, attempts=4, base=10.0):
    """Run fn(); on rate-limit/server-error style failures wait 10s, 20s, 40s
    between retries, always announcing the wait. Raises the last error with
    context if every attempt fails — never returns a silent empty result."""
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt == attempts - 1:
                break
            wait = base * (2 ** attempt) * random.uniform(0.8, 1.2)
            sys.stderr.write(f"[throttle] {what} failed ({e}); backing off "
                             f"{wait:.0f}s before retry {attempt + 2}/{attempts}\n")
            time.sleep(wait)
    raise RuntimeError(f"{what} still failing after {attempts} attempts "
                       f"with exponential backoff: {last}")


# --- output ----------------------------------------------------------------
def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def warn(msg):
    sys.stderr.write(f"[social-scout] {msg}\n")


def die(msg, code=1):
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.exit(code)
