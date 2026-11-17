import pytest

from factory.contracts import ClipSource
from factory.ingestion import canonical_source, format_for, interval_info


def test_three_hour_vod_is_bounded_by_default(monkeypatch):
    monkeypatch.setenv("CLIPS_MAX_SOURCE_BYTES", str(2 * 1024**3))
    info = {"duration": 3 * 3600, "height": 720, "filesize_approx": 6 * 1024**3}
    selected = interval_info(info, "720", 0, 1800)
    assert selected["duration"] == 1800
    assert selected["source_duration"] == 10800
    assert selected["estimated_bytes"] == 1024**3
    with pytest.raises(ValueError, match="limit"):
        interval_info(info, "720", 0, None)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "https://localhost",
        "https://www.twitch.tv/foo",
        "https://www.twitch.tv/videos/1@evil.test",
        "https://www.youtube.com.evil.test/watch?v=abcdefghijk",
    ],
)
def test_remote_sources_reject_unapproved_urls(url):
    with pytest.raises(ValueError):
        canonical_source(url)


def test_completed_twitch_vod_and_quality():
    assert canonical_source(
        "https://www.twitch.tv/videos/123456?tracking=1", "twitch"
    ) == ("twitch", "123456", "https://www.twitch.tv/videos/123456")
    assert "[height<=720]" in format_for("720")
    assert "[width<=1920]" in format_for("1080")
    with pytest.raises(ValueError):
        ClipSource(
            type="youtube",
            url="https://youtu.be/abcdefghijk",
            authorized=True,
            start_seconds=50,
            end_seconds=20,
        )
    with pytest.raises(ValueError):
        interval_info({"duration": 600, "is_live": True}, "720", 0, 1800)
