from factory.highlights import analyze, plan_windows
from factory.media import ffmpeg, run


def test_highlights_select_distinct_events_and_skip_quiet_spans():
    audio = dict.fromkeys(range(180), -45.0)
    audio.update(dict.fromkeys([18, 19, 20, 155, 156, 157], -14.0))
    parts = plan_windows(180, 3, 30, audio, {})
    assert len(parts) == 2
    assert parts[0]["start"] < 19 < parts[0]["end"]
    assert parts[1]["start"] < 156 < parts[1]["end"]
    assert parts[0]["end"] <= parts[1]["start"]
    assert all(p["end"] - p["start"] == 40 for p in parts)


def test_real_ffmpeg_highlight_analysis(tmp_path):
    source = tmp_path / "test.mp4"
    run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=5:d=90",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.3*sin(2*PI*440*t)*between(t\\,68\\,74):s=8000:d=90",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-c:a",
            "aac",
            source.name,
        ],
        tmp_path,
    )
    [clip] = analyze(source, {"audio_codec": "aac", "duration": 90}, 3, 30)
    assert clip["start"] <= 68 < 74 <= clip["end"]
    assert clip["start"] > 0
