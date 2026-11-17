"""Deterministic CLIPS stages executed through existing fenced worker claims."""

import hashlib
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from factory.clips_media import boundaries, probe, render_part, validate_output
from factory.contracts import JobSpec
from factory.highlights import analyze
from factory.captions import export_transcript, clip_captions
from factory.ingestion import download, youtube_id, youtube_resolution_ok

STAGES = (
    "validate_source",
    "ingest_source",
    "probe_source",
    "transcribe_audio",
    "plan_splits",
    "render_parts",
    "validate_outputs",
    "package",
    "archive_to_drive",
)


def fetch(client, key, destination):
    with client.stream("GET", f"/artifacts/{key}/download") as remote:
        remote.raise_for_status()
        with destination.open("wb") as output:
            for chunk in remote.iter_bytes():
                output.write(chunk)
    with destination.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != key:
            raise ValueError("Source checksum mismatch")


def execute(client, claim):
    from factory.generator import request, upload_file

    spec = JobSpec.model_validate(claim["spec"])
    source = spec.source
    assert source is not None
    name, outputs = claim["name"], claim["outputs"]
    last_progress = [0.0]

    def report(done, total):
        from time import monotonic

        if monotonic() - last_progress[0] < 5 and done < total:
            return
        request(
            client,
            "POST",
            f"/workers/stages/{claim['id']}/checkpoint",
            json={
                "lease": {"worker": claim["worker"], "token": claim["token"]},
                "result": {
                    "progress": f"{done:.0f}/{total:.0f}",
                    "completed": done,
                    "total": total,
                },
            },
        )
        last_progress[0] = monotonic()

    if name == "validate_source":
        if source.type == "local_file":
            return request(client, "GET", f"/clips/sources/{source.artifact_id}").json()
        return {"type": source.type, "url": source.url, "authorized": True}
    if (
        name == "transcribe_audio"
        and spec.split_strategy != "semantic"
        and not (spec.long_intervals and spec.captions)
    ):
        return {"status": "NOT_REQUESTED"}
    if name == "plan_splits" and spec.split_strategy == "manual":
        duration = outputs["probe_source"]["duration"]
        intervals = [s.model_dump() for s in spec.long_intervals]
        if any(s["end"] > duration for s in intervals):
            raise ValueError("Long interval is outside the processed source")
        frames = sum(
            round((s["end"] - s["start"]) * spec.output.fps) for s in intervals
        )
        return {
            "parts": [
                {
                    "part": 1,
                    "name": "part_01.mp4",
                    "start": 0,
                    "end": frames / spec.output.fps,
                    "frames": frames,
                    "intervals": intervals,
                }
            ],
            "analyzer": "manual_intervals",
        }
    if name == "plan_splits" and spec.split_strategy == "equal_duration":
        return {
            "parts": boundaries(
                outputs["probe_source"]["duration"],
                spec.parts,
                spec.output.fps,
            )
        }
    if name in {"package", "archive_to_drive"}:
        return {}
    scratch = Path(os.getenv("SCRATCH_ROOT", "data/scratch"))
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=scratch) as directory:
        work = Path(directory)
        if name == "ingest_source":
            if source.type == "local_file":
                original = outputs["validate_source"]
                duration = original["metadata"]["duration"]
                stop = (
                    min(duration, source.end_seconds)
                    if source.end_seconds is not None
                    else duration
                )
                if not source.start_seconds < stop:
                    raise ValueError(
                        "Selected local interval is outside source duration"
                    )
                if source.start_seconds == 0 and stop >= duration:
                    return original
                from factory.media import ffmpeg, run

                local = work / "original"
                path = work / "source.mp4"
                fetch(client, original["sha256"], local)
                run(
                    [
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
                        str(source.start_seconds),
                        "-i",
                        str(local),
                        "-t",
                        str(stop - source.start_seconds),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "23",
                        "-c:a",
                        "aac",
                        str(path),
                    ],
                    work,
                    timeout=int(os.getenv("CLIPS_RENDER_TIMEOUT", "3600")),
                )
                return {
                    "sha256": upload_file(client, path),
                    "metadata": {
                        **original["metadata"],
                        **probe(path),
                        "interval_start": source.start_seconds,
                        "interval_end": source.end_seconds,
                    },
                }

            cached = (
                request(
                    client, "GET", f"/clips/youtube/{youtube_id(source.url or '')}"
                ).json()
                if source.type == "youtube"
                else None
            )
            if cached and all(
                cached["metadata"].get(k) == v
                for k, v in {
                    "requested_quality": source.quality,
                    "interval_start": source.start_seconds,
                    "interval_end": source.end_seconds,
                }.items()
            ):
                return cached

            def progress(message):
                request(
                    client,
                    "POST",
                    f"/workers/stages/{claim['id']}/checkpoint",
                    json={
                        "lease": {"worker": claim["worker"], "token": claim["token"]},
                        "result": {"progress": message},
                    },
                )

            path, info = download(
                source.url,
                work,
                progress=progress,
                quality=source.quality,
                start=source.start_seconds,
                end=source.end_seconds,
            )
            metadata = probe(path)
            if not youtube_resolution_ok(metadata):
                raise ValueError("YouTube source exceeds the 1920×1080 download limit")
            return {
                "sha256": upload_file(client, path),
                "metadata": {**info, **metadata},
            }
        key = outputs["ingest_source"]["sha256"]
        if name == "probe_source":
            path = work / "source"
            fetch(client, key, path)
            return {**outputs["ingest_source"]["metadata"], **probe(path)}
        if name == "transcribe_audio":
            from factory.transcription import transcribe

            path = work / "source"
            fetch(client, key, path)
            transcript = transcribe(
                path,
                outputs["probe_source"]["duration"],
                spec.language_override,
                progress=report,
            )
            exports = {
                "json": upload_file(
                    client, export_transcript(work, transcript, "json")
                ),
                "srt": upload_file(client, export_transcript(work, transcript, "srt")),
                "vtt": upload_file(client, export_transcript(work, transcript, "vtt")),
            }
            return {**transcript, "exports": exports, "status": "TRANSCRIBED"}
        if name == "plan_splits" and spec.split_strategy == "semantic":
            from factory.semantic_highlights import rank

            transcript = outputs["transcribe_audio"]
            plan = rank(
                transcript,
                outputs["probe_source"]["duration"],
                spec.parts,
                spec.output.fps,
                spec.clip_min_seconds,
                spec.clip_max_seconds,
                progress=report,
            )
            if not plan["parts"]:
                raise ValueError(
                    "No coherent speech highlights found in the selected interval"
                )
            return plan
        if name == "plan_splits":
            path = work / "source"
            fetch(client, key, path)
            return {
                "parts": analyze(
                    path, outputs["probe_source"], spec.parts, spec.output.fps
                )
            }
        parts = outputs["plan_splits"]["parts"]
        if name == "render_parts":
            # Persist each finished part through a fenced checkpoint. A resumed claim
            # receives it and avoids re-rendering already validated binaries.
            completed = claim.get("checkpoint", {}).get("parts", [])
            source_path = work / "source"
            width, height = map(int, spec.output.resolution.split("x"))
            estimated_output = (
                sum(part["end"] - part["start"] for part in parts)
                * width
                * height
                * spec.output.fps
                * 0.2
                / 8
            )
            reserve = int(os.getenv("CLIPS_DISK_RESERVE_BYTES", str(4 * 1024**3)))
            budget = int(os.getenv("CLIPS_SCRATCH_BUDGET_BYTES", str(8 * 1024**3)))
            required = outputs["probe_source"]["size"] + estimated_output
            if required > budget:
                raise OSError(
                    "Rendering exceeds scratch budget; reduce clip count or resolution"
                )
            required += reserve
            if shutil.disk_usage(work).free < required:
                raise OSError("Insufficient disk space for rendering")
            fetch(client, key, source_path)
            background = None
            if spec.output.framing == "gameplay_split":
                background = work / "background.mp4"
                fetch(client, spec.output.background_artifact_id, background)
                probe(background)
            for part in parts[len(completed) :]:
                path = work / part["name"]
                transcript = outputs.get("transcribe_audio", {})
                if part.get("intervals") and transcript.get("words"):
                    cursor = 0.0
                    words = []
                    for interval in part["intervals"]:
                        for word in transcript["words"]:
                            start, end = (
                                max(interval["start"], word["start"]),
                                min(interval["end"], word["end"]),
                            )
                            if end > start:
                                words.append(
                                    {
                                        **word,
                                        "start": cursor + start - interval["start"],
                                        "end": cursor + end - interval["start"],
                                    }
                                )
                        cursor += interval["end"] - interval["start"]
                    transcript = {**transcript, "words": words}
                subtitle = (
                    clip_captions(work, transcript, part) if spec.captions else None
                )
                info = render_part(
                    source_path,
                    path,
                    part,
                    spec.output,
                    outputs["probe_source"],
                    subtitle,
                    background,
                )
                if subtitle:
                    info["caption_ass"] = upload_file(client, subtitle)
                    srt_path = subtitle.with_suffix(".srt")
                    if srt_path.is_file():
                        info["caption_srt"] = upload_file(client, srt_path)
                info.update(job_id=claim["job"], source_identifier=key)
                info["sha256"] = upload_file(client, path)
                completed.append(info)
                request(
                    client,
                    "POST",
                    f"/workers/stages/{claim['id']}/checkpoint",
                    json={
                        "lease": {"worker": claim["worker"], "token": claim["token"]},
                        "result": {"parts": completed},
                    },
                )
                path.unlink()
            return {"parts": completed}
        checked = []
        for part in outputs["render_parts"]["parts"]:
            path = work / part["name"]
            fetch(client, part["sha256"], path)
            checked.append(
                validate_output(
                    path,
                    part,
                    spec.output,
                    bool(outputs["probe_source"]["audio_codec"]),
                )
            )
            path.unlink()
        return {"parts": checked, "decision": "pass"}
