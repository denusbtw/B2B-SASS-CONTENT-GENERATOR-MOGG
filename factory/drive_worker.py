"""Opt-in durable archive worker. Only completed, verified MP4 artifacts are sent."""

import os
from datetime import timedelta
from string import Formatter
from time import sleep

import httpx
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import OperationalError

from factory.db import configured_ledger, jobs, now, stages
from factory.drive import Drive
from factory.drive_oauth import DriveError, load_tokens
from factory.storage import verified

drive_metadata = MetaData()
settings = Table(
    "drive_settings",
    drive_metadata,
    Column("key", String(20), primary_key=True),
    Column("file_id", String(256), nullable=False),
    Column("confirmed", Integer, nullable=False),
)
scanned = Table(
    "drive_scanned_jobs", drive_metadata, Column("job_id", String(36), primary_key=True)
)
uploads = Table(
    "drive_uploads",
    drive_metadata,
    Column("sha256", String(64), primary_key=True),
    Column("job_id", String(36), nullable=False),
    Column("part", Integer, nullable=False),
    Column("remote_id", String(256)),
    Column("url", String(300)),
    Column("error", String(80)),
    Column("attempts", Integer, nullable=False),
    Column("due", String(40), nullable=False, index=True),
)
LOCK = 2943518351671704


def filename(template: str, job_id: str, part: int, sha256: str) -> str:
    fields = {"job_id": job_id, "part": part, "sha256": sha256}
    try:
        if any(
            field not in fields or conversion or spec
            for _, field, spec, conversion in Formatter().parse(template)
            if field is not None
        ):
            raise ValueError
        if len(template) > 200:
            raise ValueError
        name = template.format(**fields)
    except (KeyError, ValueError, IndexError):
        raise DriveError("drive_filename_template_invalid") from None
    if (
        not name.lower().endswith(".mp4")
        or len(name.encode("utf-8")) > 240
        or any(character in '/\\:*?"<>|' or ord(character) < 32 for character in name)
    ):
        raise DriveError("drive_filename_invalid")
    return name


def discover(ledger) -> None:
    with ledger.write() as db:
        pending = (
            db.execute(
                select(stages.c.job, stages.c.result)
                .join(jobs, jobs.c.id == stages.c.job)
                .where(
                    stages.c.name == "package",
                    stages.c.state == "SUCCEEDED",
                    jobs.c.state == "COMPLETE",
                    ~select(scanned.c.job_id)
                    .where(scanned.c.job_id == stages.c.job)
                    .exists(),
                )
                .limit(50)
            )
            .tuples()
            .all()
        )
        for job_id, result in pending:
            for part, asset in enumerate(result["parts"], 1):
                if asset.get("deleted"):
                    continue
                key = asset["sha256"]
                if (
                    db.execute(
                        select(uploads.c.sha256).where(uploads.c.sha256 == key)
                    ).scalar_one_or_none()
                    is None
                ):
                    db.execute(
                        insert(uploads).values(
                            sha256=key,
                            job_id=job_id,
                            part=part,
                            attempts=0,
                            due=now().isoformat(),
                        )
                    )
            db.execute(insert(scanned).values(job_id=job_id))


def folder_for(ledger, drive: Drive) -> str:
    with ledger.engine.connect() as db:
        row = (
            db.execute(select(settings).where(settings.c.key == "folder"))
            .mappings()
            .first()
        )
    if row is None:
        known = drive.find_folder()
        folder_id = known or drive.allocate()
        confirmed = bool(known)
        with ledger.write() as db:
            db.execute(
                insert(settings).values(
                    key="folder", file_id=folder_id, confirmed=int(confirmed)
                )
            )
    else:
        folder_id, confirmed = row.file_id, bool(row.confirmed)
    drive.prepare_folder(folder_id, confirmed=confirmed)
    if not confirmed:
        with ledger.write() as db:
            db.execute(
                update(settings).where(settings.c.key == "folder").values(confirmed=1)
            )
    return folder_id


def archive_next(ledger, drive: Drive, template: str) -> bool:
    discover(ledger)
    with ledger.engine.connect() as db:
        item = (
            db.execute(
                select(uploads)
                .where(uploads.c.url.is_(None), uploads.c.due <= now().isoformat())
                .order_by(uploads.c.due, uploads.c.sha256)
                .limit(1)
            )
            .mappings()
            .first()
        )
    if item is None:
        return False
    key = item.sha256
    update_item = update(uploads).where(uploads.c.sha256 == key)
    try:
        try:
            path = verified(key)
        except (ValueError, OSError):
            raise DriveError("drive_local_artifact_unavailable") from None
        name = filename(template, item.job_id, item.part, key)
        folder = folder_for(ledger, drive)
        file_id = item.remote_id
        if file_id is None:
            file_id = drive.allocate()
            with ledger.write() as db:
                db.execute(update_item.values(remote_id=file_id))
        url = drive.upload(path, name, file_id, folder, key)
    except (DriveError, OSError) as exc:
        code = exc.code if isinstance(exc, DriveError) else "drive_local_io_error"
        transient = isinstance(exc, DriveError) and exc.retry
        delay = min(300, 2 ** min(item.attempts + 1, 9)) if transient else 300
        with ledger.write() as db:
            db.execute(
                update_item.values(
                    error=code,
                    attempts=item.attempts + 1,
                    due=(now() + timedelta(seconds=delay)).isoformat(),
                )
            )
        print(f"Drive upload {key[:12]} deferred: {code}", flush=True)
    else:
        with ledger.write() as db:
            db.execute(
                update_item.values(url=url, error=None, attempts=item.attempts + 1)
            )
        print(f"Drive upload {key[:12]} confirmed", flush=True)
    return True


def main():
    ledger = configured_ledger()
    if not ledger.postgres:
        raise SystemExit("Google Drive worker requires PostgreSQL.")
    template = os.getenv("DRIVE_FILENAME_TEMPLATE") or "{job_id}-part-{part}.mp4"
    try:
        filename(template, "example", 1, "0" * 64)
        with httpx.Client(timeout=35, trust_env=False) as http:
            drive = Drive(http, load_tokens(http))
            while True:
                try:
                    with ledger.engine.connect() as lock:
                        acquired = lock.execute(
                            text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK}
                        ).scalar_one()
                        if acquired:
                            try:
                                processed = archive_next(ledger, drive, template)
                            finally:
                                lock.execute(
                                    text("SELECT pg_advisory_unlock(:key)"),
                                    {"key": LOCK},
                                )
                        else:
                            processed = False
                except OperationalError:
                    print("Drive archive database unavailable; will retry", flush=True)
                    processed = False
                if not processed:
                    sleep(2)
    except DriveError as exc:
        raise SystemExit(exc.code) from None


if __name__ == "__main__":
    main()
