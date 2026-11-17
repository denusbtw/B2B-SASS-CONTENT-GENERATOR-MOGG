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


def canonical_source(url: str, platform: str | None = None) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ValueError("Use a public HTTPS video URL")
    if parsed.hostname in {"twitch.tv", "www.twitch.tv"} and platform in {
        None,
        "twitch",
    }:
        match = re.fullmatch(r"/videos/([0-9]{1,20})/?", parsed.path)
        if not match:
            raise ValueError(
                "Use a completed Twitch VOD URL: https://www.twitch.tv/videos/ID"
            )
        return "twitch", match[1], "https://www.twitch.tv/videos/" + match[1]
    if platform == "twitch":
        raise ValueError("Twitch VOD URL required")
    key = youtube_id(url)
    return "youtube", key, "https://www.youtube.com/watch?v=" + key


def max_bytes() -> int:
    return int(os.getenv("CLIPS_MAX_SOURCE_BYTES", str(2 * 1024**3)))


MAX_YOUTUBE_HEIGHT = 1080
MAX_YOUTUBE_WIDTH = 1920
VIDEO_BOUNDS = f"[height<={MAX_YOUTUBE_HEIGHT}][width<={MAX_YOUTUBE_WIDTH}]"
YOUTUBE_FORMAT = (
    f"bv*[ext=mp4]{VIDEO_BOUNDS}+ba[ext=m4a]/b[ext=mp4]{VIDEO_BOUNDS}/b{VIDEO_BOUNDS}"
)


def youtube_resolution_ok(metadata: dict) -> bool:
    width, height = metadata.get("width"), metadata.get("height")
    return (
        isinstance(width, int)
        and isinstance(height, int)
        and 0 < width <= MAX_YOUTUBE_WIDTH
        and 0 < height <= MAX_YOUTUBE_HEIGHT
    )


def format_for(quality: str) -> str:
    if quality not in {"480", "720", "1080"}:
        raise ValueError("Unsupported source quality")
    bounds = f"[height<={quality}][width<=1920]"
    return f"bv*[ext=mp4]{bounds}+ba[ext=m4a]/b[ext=mp4]{bounds}/b{bounds}"


def interval_info(info: dict, quality: str, start: float, end: float | None) -> dict:
    import math

    duration = info.get("duration")
    if info.get("is_live") or not duration or not math.isfinite(duration):
        raise ValueError("Completed video/VOD required; live sources are unsupported")
    stop = min(end, duration) if end is not None else duration
    limit = float(os.getenv("CLIPS_MAX_DURATION", "7200"))
    if not 0 <= start < stop <= duration or stop - start > limit:
        raise ValueError(
            "Selected interval is invalid or exceeds processing duration limit"
        )
    selected_size = sum(
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in info.get("requested_formats", [info])
    )
    if selected_size and selected_size * (stop - start) / duration > max_bytes():
        raise ValueError(
            "Selected source interval exceeds size limit; reduce quality or interval"
        )
    return {
        "requested_quality": quality,
        "interval_start": start,
        "interval_end": end,
        "processed_end": stop,
        "source_duration": duration,
        "duration": stop - start,
        "actual_height": info.get("height"),
        "estimated_bytes": round(selected_size * (stop - start) / duration)
        if selected_size
        else None,
        "retrieval": "bounded_video_segment",
    }


def probe_remote(url: str, quality="720", start=0, end=1800) -> dict:
    platform, key, canonical = canonical_source(url)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-cache-dir",
            "--socket-timeout",
            "20",
            "--retries",
            "1",
            "-f",
            format_for(quality),
            "--dump-single-json",
            "--skip-download",
            canonical,
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode:
        raise ValueError(
            "Source metadata unavailable; check URL/access or use a local video"
        )
    info = json.loads(result.stdout)
    return {
        "title": info.get("title", key),
        "platform": platform,
        "youtube_id": key,
        **interval_info(info, quality, start, end),
        "available_heights": sorted(
            {
                f["height"]
                for f in info.get("formats", [])
                if isinstance(f.get("height"), int) and f["height"] <= 1080
            }
        ),
    }


def download(
    url: str, work: Path, progress=None, *, quality="720", start=0, end=1800
) -> tuple[Path, dict]:
    platform, key, canonical = canonical_source(url)
    base = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "-f",
        format_for(quality),
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
    interval = interval_info(info, quality, start, end)
    if shutil.disk_usage(work).free < max_bytes() + int(
        os.getenv("CLIPS_DISK_RESERVE_BYTES", str(4 * 1024**3))
    ):
        raise OSError("Insufficient disk space for source download")
    # Stage progress contains byte counts only, without remote URLs or credentials.
    args = base + [
        "--newline",
        "--progress",
        "--max-filesize",
        str(max_bytes()),
        "--download-sections",
        f"*{interval['interval_start']}-{interval['processed_end']}",
        "--force-keyframes-at-cuts",
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
                if size > max_bytes() or shutil.disk_usage(work).free < int(
                    os.getenv("CLIPS_DISK_RESERVE_BYTES", str(4 * 1024**3))
                ):
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
        "youtube_id": key if platform == "youtube" else None,
        "platform": platform,
        "source_identifier": key,
        **interval,
    }
