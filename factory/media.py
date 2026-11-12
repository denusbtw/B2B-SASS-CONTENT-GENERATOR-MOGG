import json
import math
import os
import re
import shutil
import struct
import subprocess
import textwrap
import wave
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

from factory.contracts import Mode, Timeline


def ffmpeg() -> str:
    return (
        os.getenv("FFMPEG_BIN")
        or shutil.which("ffmpeg")
        or imageio_ffmpeg.get_ffmpeg_exe()
    )


def run(args: list[str], work: Path, *, timeout: int = 90) -> str:
    process = subprocess.run(
        args, cwd=work, capture_output=True, timeout=timeout, check=False
    )
    output = process.stdout.decode(errors="replace") + process.stderr.decode(
        errors="replace"
    )
    if process.returncode:
        raise ValueError(output[-900:])
    return output


def mock_audio(timeline: Timeline, path: Path) -> None:
    rate = 16000
    samples = bytearray()
    for i in range(rate * timeline.duration_ms // 1000):
        # Audible timing signal, not synthesized speech.
        frequency = 210 + ((i // (rate * 2)) % 2) * 110
        envelope = 1 if i % 4000 < 2500 else 0.15
        samples.extend(
            struct.pack(
                "<h",
                int(2600 * envelope * math.sin(2 * math.pi * frequency * i / rate)),
            )
        )
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(samples)


def timestamp(ms: int) -> str:
    return (
        f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    )


def subtitles(timeline: Timeline) -> str:
    return "\n".join(
        f"{i}\n{timestamp(cue.start_ms)} --> {timestamp(cue.end_ms)}\n{cue.text}\n"
        for i, cue in enumerate(timeline.cues, 1)
    )


def font(size: int):
    for name in (os.getenv("DEMO_FONT"), "arial.ttf", "DejaVuSans.ttf"):
        if name:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def draw_frames(timeline: Timeline, work: Path) -> None:
    title_font, body_font = font(22), font(17)
    count = timeline.duration_ms * timeline.fps // 1000
    for frame in range(count):
        ms = frame * 1000 // timeline.fps
        index = next(
            (
                n
                for n, cue in enumerate(timeline.cues)
                if cue.start_ms <= ms < cue.end_ms
            ),
            0,
        )
        cue = timeline.cues[index]
        image = Image.new("RGB", (timeline.width, timeline.height), "#102334")
        draw = ImageDraw.Draw(image)
        if timeline.mode == Mode.CARTOON_2D:
            draw.rectangle((0, 395, 360, 640), fill="#2e565b")
            draw.rectangle((25, 95, 155, 235), fill="#c3dbd2")
            for actor, x in enumerate((105, 255)):
                color = ("#f1aa70", "#9bc9e3")[actor]
                y = 310 + round(5 * math.sin(frame / 8 + actor))
                draw.rounded_rectangle(
                    (x - 34, y + 28, x + 34, y + 127), 20, fill=color
                )
                draw.ellipse((x - 43, y - 47, x + 43, y + 40), fill=color)
                for eye in (-15, 15):
                    draw.ellipse(
                        (x + eye - 3, y - 10, x + eye + 3, y - 4), fill="#152431"
                    )
                if actor == index % 2 and frame % 8 < 4:
                    draw.ellipse((x - 10, y + 8, x + 10, y + 22), fill="#152431")
                else:
                    draw.arc(
                        (x - 9, y + 5, x + 9, y + 20), 10, 170, fill="#152431", width=3
                    )
                if actor == 0 and 600 <= ms <= 1500:
                    draw.line(
                        (x - 20, y + 54, x - 56, y + 20 + frame % 10),
                        fill=color,
                        width=10,
                    )
        else:
            draw.rounded_rectangle((23, 108, 337, 418), 28, fill="#213e53")
            draw.text((45, 143), "STORY", font=title_font, fill="#72dfcf")
            for line, content in enumerate(textwrap.wrap(cue.text, width=24)):
                draw.text((45, 209 + line * 29), content, font=body_font, fill="white")
        draw.rounded_rectangle((16, 465, 344, 565), 14, fill="#162638")
        for line, content in enumerate(textwrap.wrap(cue.text, width=26)):
            draw.text((30, 479 + line * 21), content, font=body_font, fill="white")
        image.save(work / f"frame-{frame:04d}.png")


def render(timeline: Timeline, work: Path) -> tuple[bytes, str]:
    duration = str(timeline.duration_ms / 1000)
    args = [
        ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-filter_complex_threads",
        "1",
    ]
    srt = subtitles(timeline)
    (work / "captions.srt").write_text(srt, encoding="utf-8")
    mock_audio(timeline, work / "mock.wav")
    if timeline.mode == Mode.CLIPS:
        source = "testsrc2=size=480x270:rate=24:duration=90"
        args += ["-f", "lavfi", "-i", source]
        source_trim = f"trim=start={timeline.source_start_ms / 1000}:duration={duration},setpts=PTS-STARTPTS"
        crop = "scale=-2:640,crop=360:640"
        if timeline.stacked:
            filters = (
                f"[0:v]{source_trim},scale=-2:320,crop=360:320,split=2[top][base];"
                "[base]hflip[bottom];[top][bottom]vstack,subtitles=captions.srt[video]"
            )
        else:
            filters = f"[0:v]{source_trim},{crop},subtitles=captions.srt[video]"
        args += [
            "-i",
            "mock.wav",
            "-filter_complex",
            filters,
            "-map",
            "[video]",
            "-map",
            "1:a",
        ]
    else:
        draw_frames(timeline, work)
        args += [
            "-framerate",
            str(timeline.fps),
            "-i",
            "frame-%04d.png",
            "-i",
            "mock.wav",
            "-map",
            "0:v",
            "-map",
            "1:a",
        ]
    args += [
        "-t",
        duration,
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        "video.mp4",
    ]
    run(args, work)
    return (work / "video.mp4").read_bytes(), srt


def inspect_video(path: Path, timeline: Timeline) -> dict:
    probe = os.getenv("FFPROBE_BIN") or shutil.which("ffprobe")
    work = path.parent
    if probe:
        data = json.loads(
            run(
                [
                    probe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path.resolve()),
                ],
                work,
            )
        )
        video: dict = next(
            (s for s in data["streams"] if s["codec_type"] == "video"), {}
        )
        audio: dict = next(
            (s for s in data["streams"] if s["codec_type"] == "audio"), {}
        )
        duration = float(data["format"]["duration"])
        good = (
            video.get("codec_name") == "h264"
            and audio.get("codec_name") == "aac"
            and video.get("width") == 360
            and video.get("height") == 640
        )
    else:
        decoded = run(
            [ffmpeg(), "-hide_banner", "-i", str(path.resolve()), "-f", "null", "-"],
            work,
        )
        good = bool(
            re.search(r"Video: h264[^\n]*360x640", decoded)
            and re.search(r"Audio: aac", decoded)
        )
        match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", decoded)
        duration = (
            sum(
                float(value) * factor
                for value, factor in zip(match.groups(), (3600, 60, 1))
            )
            if match
            else 0
        )
    findings = []
    if not good:
        findings.append("stream_mismatch")
    if abs(duration - timeline.duration_ms / 1000) > 0.15:
        findings.append("duration_mismatch")
    if good:
        evidence = run(
            [
                ffmpeg(),
                "-hide_banner",
                "-i",
                str(path.resolve()),
                "-vf",
                "blackdetect=d=0.50:pix_th=0.10",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            work,
        )
        if "black_start:" in evidence:
            findings.append("black_frames")
        volume = re.search(r"mean_volume: (-?[\d.]+) dB", evidence)
        if volume is None or float(volume[1]) < -55:
            findings.append("inaudible_audio")
    return {
        "passed": not findings,
        "findings": findings,
        "duration_seconds": duration,
        "inspection": "ffprobe" if probe else "ffmpeg_decode",
        "width": 360 if good else None,
        "height": 640 if good else None,
    }
