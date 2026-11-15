"""Real CLIPS media primitives; bounded CPU encoding and frame-grid splits."""

import hashlib
import json
import math
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from factory.contracts import ClipOutput
from factory.media import ffmpeg


def probe(path: Path) -> dict:
    binary = os.getenv("FFPROBE_BIN") or shutil.which("ffprobe")
    if not binary or not Path(binary).is_file():
        raise ValueError("FFprobe is required; install FFmpeg and FFprobe")
    result = subprocess.run(
        [
            binary,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("Corrupted or unsupported video")
    data = json.loads(result.stdout)
    videos = [
        s
        for s in data["streams"]
        if s["codec_type"] == "video"
        and not s.get("disposition", {}).get("attached_pic")
    ]
    if not videos or not set(data["format"]["format_name"].split(",")) & {
        "mov",
        "mp4",
        "matroska",
        "webm",
    }:
        raise ValueError("Supported source containers: MP4, MOV, MKV, WebM")
    video = videos[0]
    duration = float(video.get("duration") or data["format"].get("duration", 0))
    if (
        not math.isfinite(duration)
        or duration <= 0
        or duration > float(os.getenv("CLIPS_MAX_DURATION", "7200"))
    ):
        raise ValueError("Invalid or excessive source duration")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    try:
        rate = Fraction(video.get("avg_frame_rate") or "0/1")
    except (ValueError, ZeroDivisionError):
        rate = Fraction(video.get("r_frame_rate") or "0/1")
    return {
        "duration": duration,
        "width": video["width"],
        "height": video["height"],
        "fps": str(rate),
        "sample_aspect_ratio": video.get("sample_aspect_ratio"),
        "time_base": video.get("time_base"),
        "frame_count": int(video["nb_frames"])
        if str(video.get("nb_frames", "")).isdigit()
        else None,
        "video_codec": video["codec_name"],
        "pixel_format": video.get("pix_fmt"),
        "audio_codec": audio["codec_name"] if audio else None,
        "video_start": float(video.get("start_time", 0)),
        "audio_start": float(audio.get("start_time", 0)) if audio else None,
        "audio_duration": float(audio.get("duration", duration)) if audio else None,
        "size": path.stat().st_size,
    }


def boundaries(duration: float, parts: int, fps: int) -> list[dict]:
    frames = round(duration * fps)
    points = [round(frames * i / parts) for i in range(parts + 1)]
    if min(b - a for a, b in zip(points, points[1:])) < fps * float(
        os.getenv("CLIPS_MIN_PART_SECONDS", "1")
    ):
        raise ValueError("Parts would be shorter than the minimum segment duration")
    return [
        {
            "part": i + 1,
            "name": f"part_{i + 1:02}.mp4",
            "start": a / fps,
            "end": b / fps,
            "frames": b - a,
        }
        for i, (a, b) in enumerate(zip(points, points[1:]))
    ]


def render_part(
    source: Path, output: Path, part: dict, profile: ClipOutput, metadata: dict
):
    w, h = map(int, profile.resolution.split("x"))
    # Normalize sample aspect ratio before framing; never stretch the displayed frame.
    prefix = "scale=trunc(iw*sar/2)*2:ih,setsar=1"
    fill = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if profile.framing == "center_crop":
        filters = f"[0:v:0]{prefix},{fill},fps={profile.fps},format=yuv420p[v]"
    else:
        filters = (
            f"[0:v:0]{prefix},split=2[bg][fg];[bg]{fill},boxblur=20:2[b];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[f];"
            f"[b][f]overlay=(W-w)/2:(H-h)/2,fps={profile.fps},format=yuv420p[v]"
        )
    duration = part["end"] - part["start"]
    # Output seeking with accurate decoding; trim audio/video from the same source clock.
    args = [
        ffmpeg(),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-threads",
        "2",
        "-protocol_whitelist",
        "file,pipe",
        "-ss",
        str(part["start"]),
        "-i",
        str(source),
        "-filter_complex_threads",
        "1",
        "-filter_complex",
        filters,
        "-map",
        "[v]",
        "-map",
        "0:a:0?",
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-preset",
        profile.preset,
        "-crf",
        str(profile.crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-af",
        "aresample=async=1:first_pts=0,alimiter=limit=0.95:level=false,apad",
        "-movflags",
        "+faststart",
        str(output),
    ]
    if not Path(args[0]).is_file():
        raise ValueError("FFmpeg executable is unavailable")
    result = subprocess.run(
        args,
        capture_output=True,
        timeout=int(os.getenv("CLIPS_RENDER_TIMEOUT", "3600")),
    )
    if result.returncode:
        output.unlink(missing_ok=True)
        raise ValueError(
            "FFmpeg render failed; verify source integrity and FFmpeg installation"
        )
    return validate_output(output, part, profile, bool(metadata["audio_codec"]))


def validate_output(path, part, profile, audio):
    data = probe(path)
    w, h = map(int, profile.resolution.split("x"))
    tolerance = max(0.12, 2 / profile.fps)
    if (
        (data["width"], data["height"], data["video_codec"], data["pixel_format"])
        != (w, h, "h264", "yuv420p")
        or abs(data["duration"] - (part["end"] - part["start"])) > tolerance
        or abs(float(Fraction(data["fps"])) - profile.fps) > 0.01
    ):
        raise ValueError(
            "Output dimensions, codec, frame rate or duration failed validation"
        )
    if audio and (
        data["audio_codec"] != "aac"
        or abs(data["audio_start"] - data["video_start"]) > tolerance
        or abs(data["audio_duration"] - data["duration"]) > tolerance
    ):
        raise ValueError("Output audio synchronization failed validation")
    with path.open("rb") as handle:
        key = hashlib.file_digest(handle, "sha256").hexdigest()
    return {
        **part,
        **data,
        "sha256": key,
        "status": "VALIDATED",
        "drive_status": "PENDING",
    }
