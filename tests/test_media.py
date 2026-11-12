from pathlib import Path

import pytest

from factory.contracts import Cue, Mode, Timeline
from factory.media import ffmpeg, inspect_video, render, run


def timeline(mode=Mode.STORY, start=0):
    return Timeline(
        mode=mode,
        part=1,
        duration_ms=2000,
        source_start_ms=start,
        cues=[Cue(start_ms=0, end_ms=2000, text="Оригінал · Привет")],
    )


@pytest.mark.parametrize("mode", list(Mode))
def test_playable_with_captions(tmp_path: Path, mode: Mode):
    item = timeline(mode, start=70_000 if mode == Mode.CLIPS else 0)
    payload, captions = render(item, tmp_path)
    assert b"ftyp" in payload[:32]
    assert "Оригінал · Привет" in captions
    assert inspect_video(tmp_path / "video.mp4", item)["passed"]


@pytest.mark.parametrize("audio", [True, False])
def test_reject_black_or_missing_audio(tmp_path: Path, audio: bool):
    args = [
        ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=black:size=360x640:rate=24",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440", "-c:a", "aac"]
    args += ["-t", "2", "-c:v", "libx264", "-threads", "2", "invalid.mp4"]
    run(args, tmp_path)
    evidence = inspect_video(tmp_path / "invalid.mp4", timeline())
    assert not evidence["passed"]
    assert ("black_frames" if audio else "stream_mismatch") in evidence["findings"]
