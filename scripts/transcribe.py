#!/usr/bin/env python3
"""Shared video transcript pipeline for Instagram/TikTok (same ladder as the
YouTube backend):

  1. Native captions from the platform, via yt-dlp subtitle tracks (best:
     free, instant, exact).
  2. Whisper transcription through the OpenAI API — only if OPENAI_API_KEY
     is set. The downloaded mp4 is sent directly when it's under Whisper's
     25 MB limit (typical reels/TikToks are); bigger files need ffmpeg on
     PATH to extract a small audio track first.
  3. Neither available -> the video is still downloaded and reported with a
     clear "no transcript possible" note — never a silent empty result.

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

WHISPER_LIMIT = 25 * 1024 * 1024


def ytdlp_cmd():
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


def run(args, timeout=600):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


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


def native_captions(url, cookies=None, lang="en"):
    """Try to pull a platform-provided caption track. Returns text or None."""
    base = ytdlp_cmd() + (["--cookies", cookies] if cookies else [])
    info_proc = run(base + ["-J", "--skip-download", url])
    if info_proc.returncode != 0:
        raise RuntimeError(f"yt-dlp could not read the video page: "
                           f"{(info_proc.stderr or '').strip().splitlines()[-1:] or 'unknown'}")
    info = json.loads(info_proc.stdout)
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


def download_video(url, cookies=None):
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    out = DOWNLOADS_DIR / f"%(id)s-{uuid.uuid4().hex[:6]}.%(ext)s"
    base = ytdlp_cmd() + (["--cookies", cookies] if cookies else [])
    proc = run(base + ["--no-playlist", "-o", str(out),
                       "--print", "after_move:filepath", "--no-simulate", url])
    if proc.returncode != 0:
        raise RuntimeError("video download failed: "
                           + (proc.stderr or "").strip().splitlines()[-1][:200])
    path = proc.stdout.strip().splitlines()[-1]
    return path


def whisper(path, lang="en"):
    """POST the file to OpenAI's transcription endpoint (no SDK needed)."""
    key = os.environ["OPENAI_API_KEY"]
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        blob = f.read()

    def part(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{name}\"\r\n\r\n{value}\r\n").encode()

    body = (part("model", "whisper-1")
            + part("response_format", "verbose_json")
            + part("language", lang)
            + (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
               f"filename=\"{os.path.basename(path)}\"\r\n"
               f"Content-Type: application/octet-stream\r\n\r\n").encode()
            + blob + f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read().decode("utf-8"))
    rows = [f"{stamp(seg['start'])} {seg['text'].strip()}"
            for seg in data.get("segments", [])]
    return "\n".join(rows) if rows else data.get("text", "").strip()


def shrink_audio(path):
    """Extract a small mono audio track so big videos fit Whisper's 25 MB cap."""
    if not shutil.which("ffmpeg"):
        return None
    audio = path.rsplit(".", 1)[0] + ".m4a"
    proc = run(["ffmpeg", "-y", "-i", path, "-vn", "-ac", "1",
                "-b:a", "48k", audio], timeout=900)
    return audio if proc.returncode == 0 else None


def transcribe(url, cookies=None, lang="en"):
    """Full ladder. Returns dict with keys: transcript (or None), source,
    video_path (when downloaded), note (when transcript impossible)."""
    info, caption = native_captions(url, cookies, lang)
    if caption and caption[0].strip():
        return {"title": info.get("title") or info.get("description", "")[:80],
                "transcript": caption[0], "source": caption[1]}

    path = download_video(url, cookies)
    if os.environ.get("OPENAI_API_KEY"):
        target = path
        if os.path.getsize(path) > WHISPER_LIMIT:
            target = shrink_audio(path)
            if not target:
                return {"transcript": None, "video_path": path, "note":
                        "No native captions; file exceeds Whisper's 25 MB limit "
                        "and ffmpeg is not installed to shrink it. Install ffmpeg "
                        "or transcribe manually."}
            if os.path.getsize(target) > WHISPER_LIMIT:
                return {"transcript": None, "video_path": path, "note":
                        "No native captions; even the extracted audio exceeds "
                        "Whisper's 25 MB limit."}
        try:
            text = whisper(target, lang)
        except Exception as e:
            return {"transcript": None, "video_path": path, "note":
                    f"No native captions and Whisper transcription failed: {e}"}
        return {"transcript": text, "video_path": path,
                "source": "Whisper (auto-transcribed; may contain errors)"}

    return {"transcript": None, "video_path": path, "note":
            "No native captions on this video and no OPENAI_API_KEY set, so "
            "no transcript is possible. The video file was downloaded to the "
            "path above — set OPENAI_API_KEY to enable Whisper transcription."}


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
    args = p.parse_args()
    try:
        result = transcribe(args.url, args.cookies, args.lang)
    except RuntimeError as e:
        die(str(e))
    emit_result(result, args.url)


if __name__ == "__main__":
    main()
