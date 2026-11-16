"""Deterministic CLIPS stages executed through existing fenced worker claims."""

import hashlib
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from factory.clips_media import boundaries, probe, render_part, validate_output
from factory.contracts import JobSpec
from factory.highlights import analyze
from factory.ingestion import download, youtube_id, youtube_resolution_ok

STAGES = (
    "validate_source",
    "ingest_source",
    "probe_source",
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
    if name == "validate_source":
        if source.type == "local_file":
            return request(client, "GET", f"/clips/sources/{source.artifact_id}").json()
        return {"type": "youtube", "url": source.url, "authorized": True}
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
                return outputs["validate_source"]
            cached = request(
                client, "GET", f"/clips/youtube/{youtube_id(source.url or '')}"
            ).json()
            if cached:
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

            path, info = download(source.url, work, progress=progress)
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
            required = outputs["probe_source"]["size"] + estimated_output + 64 * 1024**2
            if shutil.disk_usage(work).free < required:
                raise OSError("Insufficient disk space for rendering")
            fetch(client, key, source_path)
            for part in parts[len(completed) :]:
                path = work / part["name"]
                info = render_part(
                    source_path, path, part, spec.output, outputs["probe_source"]
                )
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
