"""Authenticated CLIPS routes mounted on the existing API router."""

import hashlib
import errno
import anyio
import json
import os
import shutil
from threading import BoundedSemaphore
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import insert, select, update

from factory import storage
from factory.clips_db import archives, sources
from factory.clips_media import probe
from factory.contracts import JobSpec, Lease, Mode
from factory.db import now, stages
from factory.ingestion import max_bytes, youtube_resolution_ok


UPLOAD_SLOTS = BoundedSemaphore(int(os.getenv("CLIPS_MAX_CONCURRENT_UPLOADS", "2")))


class LedgerProxy:
    def __getattr__(self, name):
        from factory.api import ledger

        return getattr(ledger, name)


def register(router, ledger, operator, reader, worker):
    ledger = LedgerProxy()

    def source_info(key):
        try:
            storage.verified(key)
        except ValueError as exc:
            raise HTTPException(404, "Source missing or corrupt") from exc
        rows = ledger.list_rows(sources, sources.c.sha256 == key)
        if not rows:
            raise HTTPException(404, "Source is not a validated upload")
        return rows[0]

    @router.post(
        "/clips/sources/upload", dependencies=[Depends(operator)], status_code=201
    )
    async def upload_source(request: Request):
        from urllib.parse import unquote

        filename = unquote(request.headers.get("X-Filename", "source.mp4"))
        if Path(filename).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
            raise HTTPException(415, "Supported containers: MP4, MOV, MKV, WebM")
        storage.ROOT.mkdir(parents=True, exist_ok=True)
        limit = max_bytes()
        try:
            declared = int(request.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise HTTPException(400, "Invalid Content-Length") from exc
        if declared > limit:
            raise HTTPException(413, "Source exceeds size limit")
        if not UPLOAD_SLOTS.acquire(blocking=False):
            raise HTTPException(429, "Too many concurrent source uploads")
        temporary = None
        try:
            with NamedTemporaryFile(dir=storage.ROOT, delete=False) as output:
                temporary = Path(output.name)
                digest, size = hashlib.sha256(), 0
                with anyio.fail_after(int(os.getenv("CLIPS_UPLOAD_TIMEOUT", "1800"))):
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > limit:
                            raise HTTPException(413, "Source exceeds size limit")
                        if (
                            shutil.disk_usage(storage.ROOT).free
                            < len(chunk) + 64 * 1024**2
                        ):
                            raise HTTPException(507, "Insufficient disk space")
                        digest.update(chunk)
                        output.write(chunk)
            # Probe outside the event loop; FFprobe receives only a server-owned file.
            from starlette.concurrency import run_in_threadpool

            info = await run_in_threadpool(probe, temporary)
            info.update(
                title=Path(filename.replace("\\", "/")).name[:180],
                original_filename=Path(filename.replace("\\", "/")).name[:180],
            )
            key = digest.hexdigest()
            temporary.replace(storage.path_for(key))
            with ledger.write() as db:
                if (
                    db.execute(
                        select(sources.c.sha256).where(sources.c.sha256 == key)
                    ).first()
                    is None
                ):
                    db.execute(insert(sources).values(sha256=key, metadata=info))
            return {"artifact_id": key, "sha256": key, "metadata": info}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(408, "Source upload timed out") from exc
        except OSError as exc:
            raise HTTPException(
                507 if exc.errno == errno.ENOSPC else 503,
                "Source storage unavailable or full",
            ) from exc
        finally:
            UPLOAD_SLOTS.release()
            if temporary:
                temporary.unlink(missing_ok=True)

    @router.get("/clips/sources/{key}", dependencies=[Depends(reader)])
    def get_source(key: str):
        return source_info(key)

    @router.get("/clips/youtube/{video_id}", dependencies=[Depends(worker)])
    def cached_source(video_id: str):
        rows = ledger.list_rows(
            sources, sources.c.metadata["youtube_id"].as_string() == video_id
        )
        for row in rows:
            if not youtube_resolution_ok(row["metadata"]):
                continue
            try:
                storage.verified(row["sha256"])
                return row
            except ValueError:
                continue
        return None

    @router.post("/clips/jobs", dependencies=[Depends(operator)], status_code=201)
    def submit_clip(spec: JobSpec):
        if spec.mode != Mode.CLIPS or spec.source is None:
            raise HTTPException(
                422, "Real CLIPS source required; use batches for synthetic demos"
            )
        if spec.source.type == "local_file":
            info = source_info(spec.source.artifact_id)
            from factory.clips_media import boundaries

            try:
                if spec.split_strategy == "equal_duration":
                    boundaries(
                        info["metadata"]["duration"], spec.parts, spec.output.fps
                    )
                elif info["metadata"]["duration"] < 30:
                    raise ValueError(
                        "Для топ-моментів відео має тривати щонайменше 30 секунд"
                    )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        batch, ids = ledger.submit([spec.model_dump(mode="json")])
        return {"id": ids[0], "batch": batch}

    def package(job):
        rows = ledger.list_rows(
            stages,
            stages.c.job == str(job),
            stages.c.name == "package",
            stages.c.state == "SUCCEEDED",
        )
        if not rows:
            raise HTTPException(409, "Local package is not ready")
        return rows[0]["result"]

    @router.get("/clips/jobs/{job}", dependencies=[Depends(operator)])
    def status(job: UUID):
        from factory.api import job_status

        value = job_status(job)
        value["archive"] = next(
            iter(ledger.list_rows(archives, archives.c.job == str(job))), None
        )
        if value["archive"]:
            # Resumable session URLs are internal bearer capabilities.
            value["archive"] = {
                k: v for k, v in value["archive"].items() if k != "files"
            }
            folder = value["archive"].get("folder_id")
            value["archive"]["folder_url"] = (
                f"https://drive.google.com/drive/folders/{folder}" if folder else None
            )
        return value

    @router.get("/clips/jobs/{job}/manifest", dependencies=[Depends(operator)])
    def manifest(job: UUID):
        result = package(job)
        archive: dict = next(
            iter(ledger.list_rows(archives, archives.c.job == str(job))), {}
        )
        files = archive.get("files", {})
        return {
            **result,
            "archive_status": archive.get("state"),
            "parts": [
                {
                    **p,
                    "drive_status": "UPLOADED"
                    if files.get(p["name"], {}).get("url")
                    else archive.get("state", "PENDING"),
                    "drive": {
                        k: v
                        for k, v in files.get(p["name"], {}).items()
                        if k in {"id", "url"}
                    },
                }
                for p in result["parts"]
            ],
        }

    @router.get("/clips/jobs/{job}/parts", dependencies=[Depends(operator)])
    def parts(job: UUID):
        return manifest(job)["parts"]

    @router.get(
        "/clips/jobs/{job}/parts/{part}/download", dependencies=[Depends(operator)]
    )
    def download_part(job: UUID, part: int):
        parts = package(job)["parts"]
        if not 1 <= part <= len(parts):
            raise HTTPException(404, "Unknown part")
        try:
            return FileResponse(
                storage.verified(parts[part - 1]["sha256"]),
                media_type="video/mp4",
                filename=f"part_{part:02}.mp4",
            )
        except ValueError as exc:
            raise HTTPException(404, "Part missing or corrupt") from exc

    @router.post("/clips/jobs/{job}/archive/retry", dependencies=[Depends(operator)])
    def retry(job: UUID):
        package(job)
        with ledger.write() as db:
            row = (
                db.execute(select(archives).where(archives.c.job == str(job)))
                .mappings()
                .first()
            )
            if (
                not row
                or row.state == "DISABLED"
                or (
                    row.state == "LOCAL_COMPLETE_DRIVE_NOT_CONFIGURED"
                    and os.getenv("DRIVE_ARCHIVE_ENABLED", "").lower()
                    not in {"1", "true"}
                )
            ):
                raise HTTPException(
                    409,
                    "Enable Drive archiving and configure DRIVE_ARCHIVE_ENABLED before submitting",
                )
            if row.state != "ARCHIVED":
                db.execute(
                    update(archives)
                    .where(archives.c.job == str(job))
                    .values(state="PENDING", error=None, due=now().isoformat())
                )
        return {"ok": True}

    @router.post("/clips/jobs/{job}/retry", dependencies=[Depends(operator)])
    def retry_processing(job: UUID):
        from factory.db import jobs

        with ledger.write() as db:
            job_row = (
                db.execute(ledger.lock(select(jobs).where(jobs.c.id == str(job))))
                .mappings()
                .first()
            )
            if not job_row or not job_row.spec.get("source"):
                raise HTTPException(404, "Unknown real CLIPS job")
            failed = (
                db.execute(
                    select(stages).where(
                        stages.c.job == str(job), stages.c.state == "FAILED"
                    )
                )
                .mappings()
                .first()
            )
            if failed is None:
                raise HTTPException(409, "No failed processing stage")
            db.execute(
                update(stages)
                .where(stages.c.id == failed.id)
                .values(state="READY", attempt=0, error=None, token=None)
            )
            db.execute(
                update(jobs).where(jobs.c.id == str(job)).values(state="GENERATING")
            )
        return {"ok": True}

    @router.post("/workers/stages/{stage}/checkpoint", dependencies=[Depends(worker)])
    def checkpoint(stage: UUID, lease: Lease, result: dict):
        with ledger.write() as db:
            row = ledger.owned(db, str(stage), lease.worker, str(lease.token))
            if row.name not in {"render_parts", "ingest_source"}:
                raise HTTPException(422, "Only CLIPS render checkpoints are supported")
            for part in result.get("parts", []):
                storage.verified(part["sha256"])
            db.execute(
                update(stages).where(stages.c.id == str(stage)).values(result=result)
            )
        return {"ok": True}


def complete_result(ledger, job, spec, name, result, outputs):
    if name == "ingest_source":
        storage.verified(result["sha256"])
    if name == "plan_splits" and spec.split_strategy == "highlights":
        planned = result["parts"]
        if not 1 <= len(planned) <= spec.parts:
            raise ValueError("Invalid highlight count")
        end = 0.0
        duration = outputs["probe_source"]["duration"]
        for index, part in enumerate(planned, 1):
            start, finish = part["start"], part["end"]
            if (
                part["part"] != index
                or part["name"] != f"part_{index:02}.mp4"
                or not end <= start < finish <= duration
                or not 30 <= round(finish - start, 6) <= 40
                or part["frames"] != round((finish - start) * spec.output.fps)
            ):
                raise ValueError("Invalid highlight interval")
            end = finish
    if name in {"render_parts", "validate_outputs"}:
        expected = outputs["plan_splits"]["parts"] if "plan_splits" in outputs else []
        count = len(expected) if spec.split_strategy == "highlights" else spec.parts
        if not 1 <= count <= spec.parts or len(result["parts"]) != count:
            raise ValueError("Part count mismatch")
        for index, part in enumerate(result["parts"], 1):
            if part["part"] != index:
                raise ValueError("Part order mismatch")
            storage.verified(part["sha256"])
    if name == "package":
        from factory.db import digest

        result = {
            "schema_version": "2.0",
            "real_clips": True,
            "job_id": job,
            "source": outputs["ingest_source"],
            "source_metadata": outputs["probe_source"],
            "parts": outputs["validate_outputs"]["parts"],
            "publishable": False,
            "review": {
                "decision": "pass",
                "media": outputs["validate_outputs"]["parts"],
            },
            "audio": "source",
            "cloud_calls": 0,
            "split_strategy": spec.split_strategy,
        }
        result["source_metadata_sha256"] = storage.put(
            json.dumps(result["source_metadata"], ensure_ascii=False, indent=2).encode()
        )
        result["manifest_hash"] = digest(result)
        result["manifest_sha256"] = storage.put(
            json.dumps(result, ensure_ascii=False, indent=2).encode()
        )
    if name == "archive_to_drive":
        enabled = spec.google_drive.enabled
        configured = os.getenv("DRIVE_ARCHIVE_ENABLED", "").lower() in {"1", "true"}
        state = (
            "PENDING"
            if enabled and configured
            else "LOCAL_COMPLETE_DRIVE_NOT_CONFIGURED"
            if enabled
            else "DISABLED"
        )
        result = {"status": state}
    return result
