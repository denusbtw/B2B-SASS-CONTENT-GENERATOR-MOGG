"""Live publishing extends targets with immutable approvals and fenced upload runtime."""

import hashlib
import os
import random
import re
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import insert, select, text, update

from factory import storage
from factory.db import Conflict, digest, jobs, now, stages, targets, uid
from factory.youtube_db import runtime
from factory.youtube_oauth import (
    AccessTokens,
    YouTubeError,
    connection,
    decrypt,
    encrypt,
)

LEASE_SECONDS = 120
POLL_STATES = (
    "UPLOADED_PRIVATE",
    "UPLOADED_UNLISTED",
    "UPLOADED_PROCESSING",
    "SCHEDULED",
)
RUN_STATES = ("QUEUED", "RETRY_PENDING", "UPLOADING", *POLL_STATES)


def source_snapshot(ledger, job, part, *, verify_artifact=True):
    rows = ledger.list_rows(
        stages,
        stages.c.job == str(job),
        stages.c.name == "package",
        stages.c.state == "SUCCEEDED",
    )
    job_rows = ledger.list_rows(jobs, jobs.c.id == str(job))
    if not rows or not job_rows:
        raise YouTubeError("youtube_completed_package_required")
    package = rows[0]["result"]
    spec = job_rows[0]["spec"]
    if spec.get("rights") not in {"original", "authorized"} or not spec.get("evidence"):
        raise YouTubeError("youtube_rights_and_evidence_required")
    review = package.get("review", {})
    if review.get("decision") != "pass" or not 1 <= part <= len(
        package.get("parts", [])
    ):
        raise YouTubeError("youtube_technical_qa_required")
    asset = package["parts"][part - 1]
    checks = review.get("media", [])
    if len(checks) < part or not (
        checks[part - 1].get("passed") is True
        or checks[part - 1].get("status") == "VALIDATED"
    ):
        raise YouTubeError("youtube_part_technical_qa_required")
    if asset.get("deleted"):
        raise YouTubeError("youtube_source_deleted")
    try:
        path = (
            storage.verified(asset["sha256"])
            if verify_artifact
            else storage.path_for(asset["sha256"])
        )
    except (ValueError, OSError):
        raise YouTubeError("youtube_local_artifact_unavailable") from None
    return {
        "sha256": asset["sha256"],
        "manifest_hash": package["manifest_hash"],
        "source_fingerprint": digest(
            {
                "sha256": asset["sha256"],
                "manifest": package["manifest_hash"],
                "rights": spec["rights"],
                "evidence": spec["evidence"],
                "review": review,
            }
        ),
    }, path


def check_source(ledger, snapshot, *, verify_artifact=True):
    source, path = source_snapshot(
        ledger, snapshot["job"], snapshot["part"], verify_artifact=verify_artifact
    )
    if any(snapshot.get(k) != v for k, v in source.items()):
        raise YouTubeError("youtube_source_changed_reapproval_required")
    return path


def create(ledger, spec):
    account = connection(ledger)
    if spec.channel_id != account["channel_id"]:
        raise YouTubeError("youtube_wrong_channel")
    source, path = source_snapshot(ledger, spec.job, spec.part)
    from factory.clips_media import probe

    try:
        media = probe(path)
    except (ValueError, OSError):
        raise YouTubeError("youtube_valid_mp4_or_ffprobe_required") from None
    with path.open("rb") as handle:
        mp4 = handle.read(8)[4:8] == b"ftyp"
    if not mp4 or media["video_codec"] != "h264":
        raise YouTubeError("youtube_validated_mp4_required")
    snapshot = {
        **spec.model_dump(mode="json"),
        **source,
        "provider": "youtube",
        "mode": "live",
        "account": "youtube:" + account["channel_id"],
        "connection_revision": account["revision"],
    }
    key = digest(["youtube", account["channel_id"], source["sha256"]])
    with ledger.write() as db:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        statement = (
            (pg_insert if ledger.postgres else sqlite_insert)(targets)
            .values(
                id=key,
                approval="",
                snapshot=snapshot,
                due=now().isoformat(),
                state="DRAFT",
            )
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(targets.c.id)
        )
        if db.execute(statement).scalar_one_or_none() is None:
            raise Conflict(
                "A YouTube publication for this artifact/channel already exists"
            )
        db.execute(
            insert(runtime).values(
                id=key,
                phase="NEW",
                offset=0,
                size=path.stat().st_size,
                attempts=0,
                created=now().isoformat(),
                updated=now().isoformat(),
            )
        )
        ledger.event(db, key, "youtube_draft_created", provider="youtube")
    return key


def get(ledger, key):
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise YouTubeError("youtube_publication_not_found")
    rows = ledger.list_rows(targets, targets.c.id == key)
    if not rows or rows[0]["snapshot"].get("provider") != "youtube":
        raise YouTubeError("youtube_publication_not_found")
    rows_runtime = ledger.list_rows(runtime, runtime.c.id == key)
    if not rows_runtime:
        raise YouTubeError("youtube_publication_runtime_missing")
    return rows[0], rows_runtime[0]


def approve(ledger, key, body):
    row, upload = get(ledger, key)
    snapshot = row["snapshot"]
    check_source(ledger, snapshot)
    account = connection(ledger)
    if (
        account["channel_id"] != body.channel_id
        or snapshot["channel_id"] != body.channel_id
    ):
        raise YouTubeError("youtube_wrong_or_reconnected_channel")
    if body.snapshot_hash != digest(snapshot):
        raise Conflict("Draft changed; review the current snapshot")
    if snapshot["visibility"] == "public" and not body.approve_public:
        raise YouTubeError("youtube_explicit_public_approval_required")
    if (
        snapshot.get("publish_at")
        and datetime.fromisoformat(snapshot["publish_at"]) <= now()
    ):
        raise YouTubeError("youtube_schedule_in_past")
    with ledger.write() as db:
        current = (
            db.execute(ledger.lock(select(targets).where(targets.c.id == key)))
            .mappings()
            .one()
        )
        if (
            current.state not in {"DRAFT", "AUTH_REQUIRED"}
            or digest(current.snapshot) != body.snapshot_hash
        ):
            raise Conflict(
                "Only a reviewed DRAFT or AUTH_REQUIRED publication can be approved"
            )
        snapshot = {**current.snapshot, "connection_revision": account["revision"]}
        db.execute(
            update(targets)
            .where(targets.c.id == key)
            .values(
                snapshot=snapshot, approval=uid(), state="QUEUED", due=now().isoformat()
            )
        )
        db.execute(
            update(runtime)
            .where(runtime.c.id == key)
            .values(
                approved_hash=digest(snapshot),
                attempts=0,
                error=None,
                updated=now().isoformat(),
            )
        )
        ledger.event(
            db,
            key,
            "youtube_live_upload_approved",
            channel=body.channel_id,
            visibility=snapshot["visibility"],
        )


@contextmanager
def publication_lock(ledger, key):
    """Session advisory lock on PG; crash-released OS lock for local SQLite."""
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise YouTubeError("youtube_publication_not_found")
    if ledger.postgres:
        lock_key = int.from_bytes(
            hashlib.sha256(("youtube:" + key).encode()).digest()[:8], "big"
        ) & ((1 << 63) - 1)
        with ledger.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as db:
            acquired = db.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
            ).scalar_one()
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    db.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                    )
        return
    folder = storage.ROOT / ".publisher-locks"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / key).open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, 2) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def remote_state(remote, snapshot):
    status = remote["status"]
    if status.get("uploadStatus") in {"failed", "rejected", "deleted"} or remote[
        "processing"
    ].get("processingStatus") in {"failed", "terminated"}:
        return "FAILED", "youtube_remote_processing_failed"
    privacy = status.get("privacyStatus")
    processed = (
        status.get("uploadStatus") == "processed"
        or remote["processing"].get("processingStatus") == "succeeded"
    )
    if privacy == "public" and processed:
        return "PUBLISHED", None
    if snapshot.get("publish_at") and privacy == "private":
        actual = status.get("publishAt")
        if not actual or datetime.fromisoformat(
            actual.replace("Z", "+00:00")
        ) != datetime.fromisoformat(snapshot["publish_at"]):
            return "NEEDS_RECONCILIATION", "youtube_remote_schedule_not_confirmed"
        return "SCHEDULED", None
    if not processed:
        return "UPLOADED_PROCESSING", None
    if privacy == "private":
        return "UPLOADED_PRIVATE", None
    if privacy == "unlisted":
        return "UPLOADED_UNLISTED", None
    return "UPLOADED_PROCESSING", None


def dispatch_youtube(ledger, adapter):
    at = now().isoformat()
    # JSON provider filtering keeps every mock target out of the live adapter.
    with ledger.engine.connect() as db:
        candidates = (
            db.execute(
                select(targets.c.id)
                .join(runtime, runtime.c.id == targets.c.id)
                .where(
                    targets.c.snapshot["provider"].as_string() == "youtube",
                    targets.c.state.in_(RUN_STATES),
                    targets.c.due <= at,
                    ((runtime.c.token.is_(None)) | (runtime.c.expires <= at)),
                )
                .order_by(targets.c.due)
                .limit(10)
            )
            .scalars()
            .all()
        )
    for key in candidates:
        with publication_lock(ledger, key) as acquired:
            if not acquired:
                continue
            return execute_locked(ledger, adapter, key)
    return False


def execute_locked(ledger, adapter, key):
    token = uid()
    with ledger.write() as db:
        row = (
            db.execute(ledger.lock(select(targets).where(targets.c.id == key)))
            .mappings()
            .one()
        )
        upload = db.execute(select(runtime).where(runtime.c.id == key)).mappings().one()
        if (
            row.state not in RUN_STATES
            or row.due > now().isoformat()
            or (upload.token and upload.expires > now().isoformat())
        ):
            return False
        if not row.approval or upload.approved_hash != digest(row.snapshot):
            db.execute(
                update(targets).where(targets.c.id == key).values(state="FAILED")
            )
            db.execute(
                update(runtime)
                .where(runtime.c.id == key)
                .values(error="youtube_approval_missing_or_changed")
            )
            return True
        db.execute(
            update(runtime)
            .where(runtime.c.id == key)
            .values(
                token=token,
                expires=(now() + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                updated=now().isoformat(),
            )
        )
        db.execute(
            update(targets)
            .where(targets.c.id == key)
            .values(state="UPLOADING" if not row.post_id else row.state)
        )
    snapshot = row.snapshot

    def save(**values):
        session_url = values.pop("session_url", None)
        identifier = values.pop("video_id", None)
        chunk_size = values.pop("chunk_size", None)
        if session_url is not None:
            values["session"] = encrypt({"url": session_url, "chunk_size": chunk_size})
        with ledger.write() as db:
            owned = db.execute(
                update(runtime)
                .where(
                    runtime.c.id == key,
                    runtime.c.token == token,
                    runtime.c.expires > now().isoformat(),
                )
                .values(
                    **values,
                    updated=now().isoformat(),
                    expires=(now() + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                )
            ).rowcount
            if not owned:
                raise Conflict("YouTube publication lease is stale")
            if identifier:
                db.execute(
                    update(targets)
                    .where(targets.c.id == key)
                    .values(
                        post_id=identifier,
                        analytics={
                            "provider": "youtube",
                            "url": "https://www.youtube.com/watch?v=" + identifier,
                        },
                    )
                )

    def guard():
        account = connection(ledger)
        if (
            not row.post_id and account["revision"] != snapshot["connection_revision"]
        ) or account["channel_id"] != snapshot["channel_id"]:
            raise YouTubeError(
                "youtube_connection_changed_reapproval_required", auth=True
            )
        if not row.post_id:
            check_source(ledger, snapshot, verify_artifact=False)
        current, _ = get(ledger, key)
        if (
            current["state"] == "CANCELLED"
            or digest(current["snapshot"]) != upload.approved_hash
        ):
            raise YouTubeError("youtube_approval_changed")
        if (
            not row.post_id
            and snapshot.get("publish_at")
            and datetime.fromisoformat(snapshot["publish_at"]) <= now()
        ):
            raise YouTubeError(
                "youtube_schedule_elapsed_reconciliation_required",
                ambiguous=upload.phase != "NEW",
            )
        save()

    try:
        guard()
        from_drive = os.getenv("YOUTUBE_SOURCE_BACKEND", "local") == "drive"
        path = (
            check_source(ledger, snapshot, verify_artifact=not from_drive)
            if not row.post_id
            else None
        )
        if from_drive and not row.post_id:
            from factory.drive_source import resolve

            path = resolve(ledger, snapshot, adapter.http)
        if isinstance(adapter.tokens, AccessTokens):
            adapter.tokens.revision = (
                connection(ledger)["revision"]
                if row.post_id
                else snapshot["connection_revision"]
            )
        if not row.post_id and adapter.channel() != snapshot["channel_id"]:
            raise YouTubeError("youtube_connected_channel_mismatch", auth=True)
        if row.post_id:
            remote = adapter.verify(row.post_id, snapshot["channel_id"])
            state, error = remote_state(remote, snapshot)
            save(remote=remote, verified=now().isoformat(), error=error)
        else:
            session_data = decrypt(upload.session) if upload.session else {}
            session = session_data.get("url")
            assert path is not None
            identifier = adapter.upload(
                path,
                snapshot,
                {
                    "phase": upload.phase,
                    "session_url": session,
                    "chunk_size": session_data.get("chunk_size"),
                },
                save,
                guard,
            )
            # Upload acceptance is not proof of processing or public availability.
            state = "UPLOADED_PROCESSING"
            error = None
            save(
                remote={
                    "video_id": identifier,
                    "url": "https://www.youtube.com/watch?v=" + identifier,
                },
                error=None,
            )
        with ledger.write() as db:
            owned = db.execute(
                update(runtime)
                .where(runtime.c.id == key, runtime.c.token == token)
                .values(token=None, expires=None, updated=now().isoformat())
            ).rowcount
            if not owned:
                raise Conflict("YouTube publication lease is stale")
            db.execute(
                update(targets)
                .where(targets.c.id == key)
                .values(
                    state=state,
                    due=(
                        now()
                        + timedelta(
                            seconds=int(os.getenv("YOUTUBE_STATUS_POLL_SECONDS", "300"))
                        )
                    ).isoformat(),
                )
            )
            ledger.event(db, key, "youtube_" + state.lower(), provider="youtube")
    except YouTubeError as exc:
        _, current = get(ledger, key)
        attempts = current["attempts"] + 1
        if exc.auth:
            state = "AUTH_REQUIRED"
        elif exc.ambiguous and not current["session"]:
            state = "NEEDS_RECONCILIATION"
        elif exc.code in {
            "youtube_session_expired_reconciliation_required",
            "youtube_remote_identity_unverified",
            "youtube_upload_response_uncertain",
            "youtube_invalid_upload_offset",
            "youtube_schedule_elapsed_reconciliation_required",
        }:
            state = "NEEDS_RECONCILIATION"
        elif exc.retry and attempts < int(os.getenv("YOUTUBE_MAX_ATTEMPTS", "8")):
            state = "RETRY_PENDING"
        else:
            state = "FAILED"
        delay = max(
            exc.retry_after,
            min(3600, 2 ** min(attempts, 11)) * random.uniform(0.75, 1.25),
        )
        with ledger.write() as db:
            values = {
                "token": None,
                "expires": None,
                "error": exc.code,
                "attempts": attempts,
                "updated": now().isoformat(),
            }
            if (
                current["phase"] == "INITIATING"
                and not exc.ambiguous
                and not current["session"]
            ):
                values["phase"] = "NEW"
            changed = db.execute(
                update(runtime)
                .where(runtime.c.id == key, runtime.c.token == token)
                .values(**values)
            ).rowcount
            if changed:
                db.execute(
                    update(targets)
                    .where(targets.c.id == key)
                    .values(
                        state=state, due=(now() + timedelta(seconds=delay)).isoformat()
                    )
                )
                ledger.event(db, key, "youtube_" + state.lower(), code=exc.code)
    except (ValueError, OSError):
        with ledger.write() as db:
            changed = db.execute(
                update(runtime)
                .where(runtime.c.id == key, runtime.c.token == token)
                .values(
                    token=None,
                    expires=None,
                    error="youtube_local_artifact_unavailable",
                    updated=now().isoformat(),
                )
            ).rowcount
            if changed:
                db.execute(
                    update(targets).where(targets.c.id == key).values(state="FAILED")
                )
    return True
