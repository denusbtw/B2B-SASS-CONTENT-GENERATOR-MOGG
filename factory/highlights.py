"""Rank short source windows by unusual audio and visual activity."""

import math
from pathlib import Path

from factory.media import ffmpeg, run

MIN_SECONDS = 30
WINDOW_SECONDS = 40
STEP_SECONDS = 5


def activity(path: Path, key: str) -> dict[int, float]:
    series: dict[int, float] = {}
    second = -1
    for line in path.read_text(encoding="utf-8").splitlines():
        if "pts_time:" in line:
            stamp = float(line.split("pts_time:", 1)[1].split()[0])
            second = max(0, int(stamp)) if math.isfinite(stamp) else -1
        elif second >= 0 and line.startswith(key + "="):
            value = float(line.split("=", 1)[1])
            if value == -math.inf:
                value = -96.0
            if math.isfinite(value):
                series[second] = max(series.get(second, -math.inf), value)
    return series


def plan_windows(
    duration: float, parts: int, fps: int, audio: dict, cuts: dict
) -> list[dict]:
    total = int(duration * fps)
    length = min(total, WINDOW_SECONDS * fps)
    if length < MIN_SECONDS * fps:
        raise ValueError("Top moments require at least 30 seconds of source video")
    levels = sorted(audio.values())
    floor = levels[len(levels) // 2] if levels else -60.0
    energy = {
        second: min(1.0, max(0.0, (value - max(-45.0, floor + 4)) / 12))
        for second, value in audio.items()
    }
    starts = set(range(0, total - length + 1, STEP_SECONDS * fps))
    starts.add(total - length)
    candidates = []
    for start in starts:
        end = start + length
        # Prefer events inside the clip, with room for a lead-in and a payoff.
        seconds = range(start // fps + 3, end // fps - 3)
        samples = [0.7 * energy.get(t, 0) + 0.3 * cuts.get(t, 0) for t in seconds]
        strongest = sorted(samples, reverse=True)[:5]
        score = sum(strongest) / 5
        if score >= 0.08:
            candidates.append((score, start, end))
    selected: list[tuple[float, int, int]] = []
    for score, start, end in sorted(candidates, key=lambda c: (-c[0], c[1])):
        if all(end <= left or start >= right for _, left, right in selected):
            selected.append((score, start, end))
            if len(selected) == parts:
                break
    if not selected:
        raise ValueError("No distinct audio or visual highlights detected")
    return [
        {
            "part": index,
            "name": f"part_{index:02}.mp4",
            "start": start / fps,
            "end": end / fps,
            "frames": end - start,
            "highlight_score": round(score, 4),
        }
        for index, (score, start, end) in enumerate(
            sorted(selected, key=lambda c: c[1]), 1
        )
    ]


def analyze(source: Path, metadata: dict, parts: int, fps: int) -> list[dict]:
    work = source.parent
    video_key = "lavfi.scene_score"
    audio_key = "lavfi.astats.Overall.RMS_level"
    video_file = work / "highlight-cuts.txt"
    audio_file = work / "highlight-audio.txt"
    common = [
        ffmpeg(),
        "-nostdin",
        "-v",
        "error",
        "-threads",
        "2",
        "-filter_threads",
        "1",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        source.name,
    ]
    run(
        [
            *common,
            "-an",
            "-vf",
            f"scale=160:-2,fps=2,select='gte(scene,0)',metadata=print:key={video_key}:file={video_file.name}",
            "-f",
            "null",
            "-",
        ],
        work,
        timeout=1800,
    )
    audio = {}
    if metadata.get("audio_codec"):
        run(
            [
                *common,
                "-vn",
                "-af",
                f"aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,ametadata=print:key={audio_key}:file={audio_file.name}",
                "-f",
                "null",
                "-",
            ],
            work,
            timeout=1800,
        )
        audio = activity(audio_file, audio_key)
    return plan_windows(
        metadata["duration"], parts, fps, audio, activity(video_file, video_key)
    )
