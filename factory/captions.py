"""Unicode-safe transcript exports and per-clip subtitles."""

import json
from pathlib import Path

from factory.media import timestamp


def srt(segments: list[dict], offset: float = 0, end: float = float("inf")) -> str:
    cues: list[str] = []
    for segment in segments:
        left = max(offset, segment["start"])
        right = min(end, segment["end"])
        text = " ".join(segment["text"].replace("-->", "→").split())
        if right > left and text:
            cues.append(
                f"{len(cues) + 1}\n{timestamp(round((left - offset) * 1000))} --> "
                f"{timestamp(round((right - offset) * 1000))}\n{text}\n"
            )
    return "\n".join(cues)


def export_transcript(work: Path, transcript: dict, kind: str) -> Path:
    path = work / ("transcript." + kind)
    body = (
        json.dumps(transcript, ensure_ascii=False)
        if kind == "json"
        else srt(transcript["segments"])
    )
    if kind == "vtt":
        import re

        body = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", body)
    path.write_text(body, encoding="utf-8")
    return path


def clip_captions(work: Path, transcript: dict, part: dict) -> Path | None:
    words = [
        word
        for word in transcript.get("words", [])
        if word["start"] < part["end"] and word["end"] > part["start"]
    ]
    if not words:
        return None
    # Short groups preserve source word timing without claiming forced alignment.
    segments = [
        {
            "start": group[0]["start"],
            "end": group[-1]["end"],
            "text": " ".join(word["word"] for word in group),
        }
        for group in (words[i : i + 6] for i in range(0, len(words), 6))
    ]
    path = work / f"caption-{part['part']:02}.srt"
    path.write_text(srt(segments, part["start"], part["end"]), encoding="utf-8")
    ass = work / f"caption-{part['part']:02}.ass"
    ass.write_text(karaoke(words, part["start"], part["end"]), encoding="utf-8")
    return ass


def karaoke(words: list[dict], offset: float, end: float) -> str:
    """ASS karaoke cues, clamped to clip time; transcript is never ASS markup."""
    import re

    def stamp(value):
        centiseconds = max(0, round(value * 100))
        hours, rest = divmod(centiseconds, 360000)
        minutes, rest = divmod(rest, 6000)
        seconds, cs = divmod(rest, 100)
        return f"{hours}:{minutes:02}:{seconds:02}.{cs:02}"

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,62,&H0054EFFF,&H00FFFFFF,&H00101010,&H90000000,-1,0,0,0,100,100,0,0,1,4,1,2,90,90,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for i in range(0, len(words), 6):
        group = words[i : i + 6]
        left, right = max(offset, group[0]["start"]), min(end, group[-1]["end"])
        if right <= left:
            continue
        body = ""
        cursor = left
        for word in group:
            start = max(left, word["start"])
            finish = min(right, word["end"])
            text = re.sub(r"[{}\\\r\n]", "", word["word"]).strip()
            if finish <= start or not text:
                continue
            # Include a silent timing tag before a word when there is a pause.
            gap = max(0, round((start - cursor) * 100))
            if gap:
                body += "{\\kf" + str(gap) + "} "
            body += (
                "{\\kf" + str(max(1, round((finish - start) * 100))) + "}" + text + " "
            )
            cursor = finish
        if body:
            lines.append(
                f"Dialogue: 0,{stamp(left - offset)},{stamp(right - offset)},Default,,0,0,0,,{body.strip()}"
            )
    return header + "\n".join(lines) + "\n"
