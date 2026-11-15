"""Bounded, credential-free YouTube ingestion. No fallback to synthetic media."""

import json
import os
import re
import shutil
import subprocess
import sys
from time import monotonic, sleep
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from factory.media import ffmpeg


def youtube_id(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ValueError("Use an HTTPS YouTube video URL")
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        key = parsed.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        key = (
            parse_qs(parsed.query).get("v", [""])[0]
            if parsed.path == "/watch"
            else parsed.path.removeprefix("/shorts/").removeprefix("/embed/")
        )
    else:
        raise ValueError("Only YouTube video URLs are supported")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", key):
        raise ValueError("Invalid YouTube video ID")
    return key


def max_bytes() -> int:
    return int(os.getenv("CLIPS_MAX_SOURCE_BYTES", str(2 * 1024**3)))


def download(url: str, work: Path, progress=None) -> tuple[Path, dict]:
    key = youtube_id(url)
    canonical = "https://www.youtube.com/watch?v=" + key
    base = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-cache-dir",
        "--socket-timeout",
        "20",
        "--retries",
        "2",
    ]

    def invoke(args, timeout):
        result = subprocess.run(
            base + args + [canonical], capture_output=True, text=True, timeout=timeout
        )
        if result.returncode:
            # Do not persist remote URLs or potentially sensitive response bodies.
            raise ValueError(
                "YouTube unavailable, restricted, rate limited or interrupted; use a local authorized file"
            )
        return result.stdout

    info = json.loads(invoke(["--dump-single-json", "--skip-download"], 90))
    duration = info.get("duration")
    if (
        info.get("is_live")
        or not duration
        or duration > float(os.getenv("CLIPS_MAX_DURATION", "7200"))
    ):
        raise ValueError("YouTube duration exceeds limit or source is live")
    if (info.get("filesize") or info.get("filesize_approx") or 0) > max_bytes():
        raise ValueError("YouTube source exceeds size limit")
    if shutil.disk_usage(work).free < max_bytes() * 2:
        raise OSError("Insufficient disk space for source download")
    # Stage progress contains byte counts only, without remote URLs or credentials.
    args = base + [
        "--newline",
        "--progress",
        "--max-filesize",
        str(max_bytes()),
        "--match-filter",
        f"duration <= {float(os.getenv('CLIPS_MAX_DURATION', '7200'))}",
        "-f",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format",
        "mp4",
        "--ffmpeg-location",
        str(Path(ffmpeg()).parent),
        "--output",
        str(work / "source.%(ext)s"),
        canonical,
    ]
    started = monotonic()
    last_progress = 0.0
    with (work / "download.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(args, stdout=log, stderr=log)
        try:
            while process.poll() is None:
                if monotonic() - started > int(
                    os.getenv("CLIPS_DOWNLOAD_TIMEOUT", "1800")
                ):
                    raise ValueError(
                        "YouTube download timed out; retry or use local upload"
                    )
                size = sum(p.stat().st_size for p in work.iterdir() if p.is_file())
                if size > max_bytes() or shutil.disk_usage(work).free < 64 * 1024**2:
                    raise ValueError("YouTube download exceeds storage limit")
                if progress and monotonic() - last_progress > 5:
                    progress(f"Downloaded {size} bytes")
                    last_progress = monotonic()
                sleep(0.5)
        except BaseException:
            process.kill()
            process.wait()
            raise
    if process.returncode:
        raise ValueError("YouTube download failed; use a local authorized file")
    files = [
        p
        for p in work.glob("source.*")
        if p.suffix in {".mp4", ".mkv", ".webm", ".mov"}
    ]
    if len(files) != 1 or files[0].stat().st_size > max_bytes():
        raise ValueError("Download missing or exceeds size limit")
    return files[0], {
        "title": info.get("title", key),
        "youtube_id": key,
        "duration": duration,
    }
