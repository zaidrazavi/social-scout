#!/usr/bin/env python3
"""Shared video transcript pipeline for Instagram/TikTok (same ladder as the
YouTube backend):

  1. Native captions from the platform, via yt-dlp subtitle tracks (best:
     free, instant, exact).
  2. Local Whisper transcription via the faster-whisper package — runs
     entirely on this machine, no API key and no account, ever. The model
     (default "base") downloads itself automatically on first use and is
     cached for every run after. No file-size limit and no external ffmpeg
     needed: faster-whisper decodes audio internally.
  3. Neither works -> the video is still downloaded and reported with a
     clear "no transcript possible" note — never a silent empty result.

Whisper here is CPU-friendly but not instant: on a laptop without a GPU,
expect roughly real-time-ish transcription (a 30 s reel in a handful of
seconds with "base"). Set SOCIAL_SCOUT_WHISPER_MODEL to trade speed for
accuracy ("tiny" fastest, "base" default, "small"/"medium" more accurate).

Used as a library by instagram.py / tiktok.py; also runs standalone:
  python scripts/transcribe.py <video_url> [--cookies FILE] [--lang en]
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid

from common import DOWNLOADS_DIR, die, warn

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Smallest practical default so it stays usable on a normal no-GPU laptop.
WHISPER_MODEL = os.environ.get("SOCIAL_SCOUT_WHISPER_MODEL", "base")


def ytdlp_cmd(cookies=None, impersonate=None):
    """Base yt-dlp invocation. `impersonate` (e.g. "chrome") uses curl_cffi to
    mimic a real browser's TLS fingerprint — TikTok now 403s without it."""
    base = ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]
    if cookies:
        base += ["--cookies", cookies]
    if impersonate:
        base += ["--impersonate", impersonate]
    return base


def run(args, timeout=600):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def ytdlp_info(url, cookies=None, impersonate=None):
    """Fetch a video's full metadata dict via yt-dlp (no download)."""
    proc = run(ytdlp_cmd(cookies, impersonate) + ["-J", "--skip-download", url])
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        raise RuntimeError("yt-dlp could not read the video: "
                           + (tail[-1][:200] if tail else "unknown error"))
    return json.loads(proc.stdout)


def stamp(seconds):
    s = int(seconds)
    return f"[{s // 60:02d}:{s % 60:02d}]"


def parse_vtt(text):
    """WebVTT/SRT-ish -> [MM:SS] lines, deduplicating rolling repeats."""
    lines, last = [], None
    t = None
    for raw in text.splitlines():
        m = re.match(r"(?:(\d+):)?(\d+):(\d+)[.,]\d+\s*-->", raw)
        if m:
            h, mi, se = (int(x) if x else 0 for x in m.groups())
            t = h * 3600 + mi * 60 + se
            continue
        row = re.sub(r"<[^>]+>", "", raw).strip()
        if (not row or row == "WEBVTT" or row.isdigit()
                or row.startswith(("Kind:", "Language:", "NOTE"))):
            continue
        if row != last:
            lines.append(f"{stamp(t or 0)} {row}")
            last = row
    return "\n".join(lines)


def native_captions(url, cookies=None, lang="en", impersonate=None, info=None):
    """Try to pull a platform-provided caption track. Returns (info, track).
    `info` may be passed in to avoid a duplicate metadata fetch."""
    if info is None:
        info = ytdlp_info(url, cookies, impersonate)
    tracks = {**(info.get("automatic_captions") or {}),
              **(info.get("subtitles") or {})}  # manual tracks win on key clash
    code = next((c for c in tracks
                 if c == lang or c.split("-")[0] == lang), None) \
        or (next(iter(tracks)) if tracks else None)
    if not code:
        return info, None
    entry = tracks[code][0]
    req = urllib.request.Request(entry["url"], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", errors="replace")
    if entry.get("ext") == "json3":
        data = json.loads(raw)
        rows = []
        for ev in data.get("events", []):
            text = "".join(s.get("utf8", "") for s in ev.get("segs") or [])
            text = text.replace("\n", " ").strip()
            if text:
                rows.append(f"{stamp(int(ev.get('tStartMs', 0)) / 1000)} {text}")
        return info, ("\n".join(rows), f"native captions ({code})")
    return info, (parse_vtt(raw), f"native captions ({code})")


def download_video(url, cookies=None, impersonate=None):
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    out = DOWNLOADS_DIR / f"%(id)s-{uuid.uuid4().hex[:6]}.%(ext)s"
    proc = run(ytdlp_cmd(cookies, impersonate) + ["--no-playlist", "-o", str(out),
               "--print", "after_move:filepath", "--no-simulate", url])
    if proc.returncode != 0:
        raise RuntimeError("video download failed: "
                           + (proc.stderr or "").strip().splitlines()[-1][:200])
    path = proc.stdout.strip().splitlines()[-1]
    return path


_MODEL = None  # loaded once per process, then reused across calls


def load_model():
    """Load (and on first ever use, auto-download) the faster-whisper model.
    Cached module-level so repeated transcripts in one run don't reload it."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel  # imported lazily: only the
        # transcript fallback needs it, so YouTube/Reddit/web users who never
        # transcribe don't pay the import cost.
        warn(f"loading local Whisper model '{WHISPER_MODEL}' "
             "(first run downloads it once, then it's cached)...")
        # int8 on CPU keeps memory and time reasonable on a normal laptop;
        # faster-whisper falls back gracefully if a GPU isn't present.
        _MODEL = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _MODEL


def whisper_local(path):
    """Transcribe a media file entirely on this machine. Returns (text,
    detected_language). Language is auto-detected — forcing a language on a
    reel in another tongue would garble it."""
    model = load_model()
    segments, info = model.transcribe(path, vad_filter=True)
    rows = [f"{stamp(seg.start)} {seg.text.strip()}"
            for seg in segments if seg.text.strip()]
    return "\n".join(rows), getattr(info, "language", None)


def transcribe(url, cookies=None, lang="en", impersonate=None):
    """Full ladder. Returns dict with keys: transcript (or None), source,
    video_path (when downloaded), note (when transcript impossible)."""
    info, caption = native_captions(url, cookies, lang, impersonate)
    if caption and caption[0].strip():
        return {"title": info.get("title") or info.get("description", "")[:80],
                "transcript": caption[0], "source": caption[1]}

    path = download_video(url, cookies, impersonate)
    try:
        text, detected = whisper_local(path)
    except ImportError:
        return {"transcript": None, "video_path": path, "note":
                "No native captions, and the local Whisper package isn't "
                "installed. Run: pip install -r requirements.txt "
                "(it downloads the model automatically on first use). The "
                "video file was still saved to the path above."}
    except Exception as e:
        return {"transcript": None, "video_path": path, "note":
                f"No native captions and local Whisper transcription failed: "
                f"{e}. The video file was saved to the path above."}
    if not text.strip():
        return {"transcript": None, "video_path": path, "note":
                "No native captions, and local Whisper found no speech in the "
                "audio (music-only or silent video). Saved to the path above."}
    lang_note = f", detected {detected}" if detected else ""
    return {"transcript": text, "video_path": path,
            "source": f"local Whisper '{WHISPER_MODEL}'{lang_note} "
                      "(auto-transcribed, may contain errors)"}


def emit_result(result, header):
    print(f"# {header}")
    if result.get("title"):
        print(f"# {result['title']}")
    if result.get("video_path"):
        print(f"# downloaded: {result['video_path']}")
    if result.get("transcript"):
        print(f"# source: {result['source']}\n")
        print(result["transcript"])
    else:
        print(f"\nNO TRANSCRIPT: {result['note']}")


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url")
    p.add_argument("--cookies", help="Netscape cookies.txt (yt-dlp format)")
    p.add_argument("--lang", default="en")
    p.add_argument("--impersonate", help="yt-dlp impersonate target, e.g. chrome")
    args = p.parse_args()
    try:
        result = transcribe(args.url, args.cookies, args.lang, args.impersonate)
    except RuntimeError as e:
        die(str(e))
    emit_result(result, args.url)


if __name__ == "__main__":
    main()
