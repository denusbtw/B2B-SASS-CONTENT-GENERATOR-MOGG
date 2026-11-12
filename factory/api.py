import hmac
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import text

from factory.contracts import (
    Approval,
    BatchInput,
    Claim,
    Failure,
    JobSpec,
    Lease,
    Plan,
    Review,
)
from factory.db import Conflict, configured_ledger, jobs, stages, targets, workers
from factory.storage import MAX_BYTES, put, verified

ledger = configured_ledger()
router = APIRouter(prefix="/api/v1")


def check_token(provided: str, names: tuple[str, ...]):
    if not any(
        (configured := os.getenv(name)) and hmac.compare_digest(provided, configured)
        for name in names
    ):
        raise HTTPException(401, "Missing or invalid token")


def operator(authorization: Annotated[str, Header()] = ""):
    check_token(authorization, ("OPERATOR_TOKEN",))


def worker(authorization: Annotated[str, Header()] = ""):
    check_token(authorization, ("WORKER_AUTH_TOKEN",))


def reader(authorization: Annotated[str, Header()] = ""):
    check_token(authorization, ("OPERATOR_TOKEN", "WORKER_AUTH_TOKEN"))


@router.get("/health/live")
def live():
    return {"status": "ok"}


@router.get("/health/ready")
def ready():
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with ledger.engine.connect() as db:
            db.execute(text("SELECT count(*) FROM stages")).scalar_one()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Database or migration unavailable") from exc
    return {"database": "ready", "integration_mode": "mock"}


@router.post("/batches", dependencies=[Depends(operator)], status_code=201)
def submit(body: BatchInput):
    accepted = []
    indexes = []
    rejected = []
    for index, item in enumerate(body.jobs):
        try:
            accepted.append(JobSpec.model_validate(item).model_dump(mode="json"))
            indexes.append(index)
        except ValidationError as exc:
            rejected.append(
                {
                    "index": index,
                    "errors": exc.errors(include_input=False, include_context=False),
                }
            )
    if not accepted:
        raise HTTPException(422, rejected)
    batch, ids = ledger.submit(accepted)
    return {
        "id": batch,
        "accepted": [{"index": i, "id": key} for i, key in zip(indexes, ids)],
        "rejected": rejected,
    }


@router.get("/batches/{batch}", dependencies=[Depends(operator)])
def batch_status(batch: UUID):
    rows = ledger.list_rows(jobs, jobs.c.batch == str(batch))
    if not rows:
        raise HTTPException(404, "Unknown batch")
    return {"id": batch, "jobs": rows}


def job_stages(job: UUID):
    return ledger.list_rows(stages, stages.c.job == str(job))


@router.get("/jobs/{job}", dependencies=[Depends(operator)])
def job_status(job: UUID):
    rows = ledger.list_rows(jobs, jobs.c.id == str(job))
    if not rows:
        raise HTTPException(404, "Unknown job")
    return {
        **rows[0],
        "stages": [
            {k: v for k, v in stage.items() if k != "token"}
            for stage in sorted(job_stages(job), key=lambda row: row["position"])
        ],
    }


@router.get("/workers", dependencies=[Depends(operator)])
def list_workers():
    return ledger.list_rows(workers)


@router.post("/workers/claim", dependencies=[Depends(worker)])
def claim(body: Claim):
    return ledger.claim(body.worker)


@router.post("/workers/stages/{stage}/heartbeat", dependencies=[Depends(worker)])
def heartbeat(stage: UUID, body: Lease):
    ledger.heartbeat(str(stage), body.worker, str(body.token))
    return {"ok": True}


@router.post("/workers/stages/{stage}/complete", dependencies=[Depends(worker)])
def complete(stage: UUID, lease: Lease, result: dict):
    rows = ledger.list_rows(stages, stages.c.id == str(stage))
    if not rows:
        raise HTTPException(404, "Unknown stage")
    row = rows[0]
    job = ledger.list_rows(jobs, jobs.c.id == row["job"])[0]
    spec = JobSpec.model_validate(job["spec"])
    outputs = {
        stage["name"]: stage["result"]
        for stage in ledger.list_rows(
            stages, stages.c.job == job["id"], stages.c.state == "SUCCEEDED"
        )
    }
    try:
        if row["name"].endswith("_plan"):
            plan = Plan.model_validate(result)
            if len(plan.timelines) != spec.parts or any(
                item.mode != spec.mode or item.part != i
                for i, item in enumerate(plan.timelines, 1)
            ):
                raise ValueError("Plan mismatch")
            result = plan.model_dump(mode="json")
        elif row["name"] == "render":
            if len(result["parts"]) != spec.parts:
                raise ValueError("Render count mismatch")
            for part in result["parts"]:
                verified(part["sha256"])
                verified(part["captions"])
        elif row["name"] == "qa":
            result = Review.model_validate(result).model_dump(mode="json")
        elif row["name"] == "package":
            from factory.db import digest

            result = {
                "schema_version": "1.0",
                "parts": outputs["render"]["parts"],
                "publishable": spec.rights_pass and outputs["qa"]["decision"] == "pass",
                "rights": spec.rights,
                "evidence": spec.evidence,
                "review": outputs["qa"],
                "storage": "local_mock",
                "audio": "synthetic_tone",
                "cloud_calls": 0,
            }
            result["manifest_hash"] = digest(result)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, "Invalid stage result") from exc
    ledger.finish(str(stage), lease.worker, str(lease.token), result=result)
    return {"ok": True}


@router.post("/workers/stages/{stage}/fail", dependencies=[Depends(worker)])
def fail(stage: UUID, body: Failure):
    ledger.finish(str(stage), body.worker, str(body.token), error=body.code)
    return {"ok": True}


@router.post("/artifacts/upload", dependencies=[Depends(worker)])
async def upload(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "Artifact exceeds size limit")
        data.extend(chunk)
    return {"sha256": put(bytes(data))}


@router.get("/artifacts/{key}/download", dependencies=[Depends(reader)])
def download(key: str):
    try:
        return FileResponse(verified(key), media_type="application/octet-stream")
    except ValueError as exc:
        raise HTTPException(404, "Artifact missing or corrupt") from exc


@router.get("/packages", dependencies=[Depends(operator)])
def packages():
    return [
        {"job": row["job"], **row["result"]}
        for row in ledger.list_rows(
            stages, stages.c.name == "package", stages.c.state == "SUCCEEDED"
        )
    ]


@router.get("/platform-accounts", dependencies=[Depends(operator)])
def accounts():
    from typing import get_args
    from factory.contracts import TargetSpec

    return [
        {"id": account, "mode": "mock"}
        for account in get_args(TargetSpec.model_fields["account"].annotation)
    ]


@router.post("/publication-batches", dependencies=[Depends(operator)], status_code=201)
def approve(body: Approval):
    snapshots = []
    for target in body.targets:
        package = next(
            (
                s["result"]
                for s in job_stages(target.job)
                if s["name"] == "package" and s["state"] == "SUCCEEDED"
            ),
            None,
        )
        if (
            not package
            or not package["publishable"]
            or target.part > len(package["parts"])
        ):
            raise HTTPException(409, "Missing package, QA pass or rights evidence")
        media = package["parts"][target.part - 1]
        try:
            verified(media["sha256"])
        except ValueError as exc:
            raise HTTPException(409, "Approved media is missing or corrupt") from exc
        snapshots.append(
            {
                **target.model_dump(mode="json"),
                "sha256": media["sha256"],
                "manifest_hash": package["manifest_hash"],
                "mode": "mock",
                "scheduled_at": target.scheduled_at.isoformat(),
            }
        )
    return {"approval": ledger.approve(snapshots), "targets": len(snapshots)}


@router.get("/publication-targets", dependencies=[Depends(operator)])
def publication_targets():
    return ledger.list_rows(targets)


def mount(app):
    app.include_router(router)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/operator", include_in_schema=False)
    def dashboard():
        return FileResponse(
            Path(__file__).with_name("operator.html"), media_type="text/html"
        )
