"""Verify exported demo media using FFprobe without starting the coordinator."""

import json
import os
import shutil
from pathlib import Path

from factory.contracts import Mode
from factory.media import run


def main():
    ffprobe = os.getenv("FFPROBE_BIN") or shutil.which("ffprobe")
    if not ffprobe:
        raise SystemExit("Set FFPROBE_BIN or install FFprobe on PATH.")
    root = Path("data/demo").resolve()
    evidence = {}
    for mode in Mode:
        data = json.loads(
            run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(root / f"{mode.value}.mp4"),
                ],
                root,
            )
        )
        video = next(
            stream for stream in data["streams"] if stream["codec_type"] == "video"
        )
        audio = next(
            stream for stream in data["streams"] if stream["codec_type"] == "audio"
        )
        duration = float(data["format"]["duration"])
        if (
            video["codec_name"],
            audio["codec_name"],
            video["width"],
            video["height"],
        ) != ("h264", "aac", 360, 640) or not 2 <= duration <= 12.15:
            raise ValueError(f"Unexpected media properties for {mode.value}")
        evidence[mode.value] = {
            "width": video["width"],
            "height": video["height"],
            "video": video["codec_name"],
            "audio": audio["codec_name"],
            "duration_seconds": duration,
            "frame_rate": video["r_frame_rate"],
        }
    text = json.dumps(evidence, indent=2)
    (root / "ffprobe.json").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
