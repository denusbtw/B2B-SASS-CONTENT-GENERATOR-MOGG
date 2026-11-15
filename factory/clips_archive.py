"""Per-job Drive archive, called under the existing archive-worker advisory lock."""

from datetime import timedelta
from sqlalchemy import update
from factory import storage
from factory.clips_db import archives
from factory.db import now, stages
from factory.drive_oauth import DriveError


def archive_next(ledger, drive):
    from factory.drive_worker import folder_for

    rows = ledger.list_rows(
        archives,
        archives.c.state.in_(["PENDING", "ARCHIVE_FAILED"]),
        archives.c.due <= now().isoformat(),
    )
    if not rows:
        return False
    row = sorted(rows, key=lambda x: (x["due"], x["job"]))[0]
    job = row["job"]
    where = archives.c.job == job

    def save(**values):
        with ledger.write() as db:
            db.execute(update(archives).where(where).values(**values))

    try:
        package = ledger.list_rows(
            stages,
            stages.c.job == job,
            stages.c.name == "package",
            stages.c.state == "SUCCEEDED",
        )[0]["result"]
        root = folder_for(ledger, drive)
        folder = row["folder_id"]
        if not folder:
            folder = drive.allocate()
            save(folder_id=folder)
        drive.prepare_child(
            folder, root, job, package["source_metadata"].get("title", "Video")
        )
        save(folder_confirmed=1)
        files = row["files"]
        # Stable manifest bytes: Drive URLs are available through the API overlay.
        assets = [(p["name"], p["sha256"], "video/mp4") for p in package["parts"]]
        assets.append(("manifest.json", package["manifest_sha256"], "application/json"))
        for name, key, mime in assets:
            item = files.setdefault(name, {"sha256": key})
            if item.get("url"):
                continue
            if not item.get("id"):
                item["id"] = drive.allocate()
                save(files=files)

            def session(value):
                item["session"] = value
                save(files=files)

            item["url"] = drive.upload(
                storage.verified(key),
                name,
                item["id"],
                folder,
                key,
                mime_type=mime,
                session=item.get("session"),
                save_session=session,
            )
            item.pop("session", None)
            save(files=files)
        save(state="ARCHIVED", error=None, attempts=row["attempts"] + 1)
    except (DriveError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, DriveError) else "drive_local_artifact_error"
        save(
            state="ARCHIVE_FAILED",
            error=code,
            attempts=row["attempts"] + 1,
            due=(
                now() + timedelta(seconds=min(300, 2 ** min(row["attempts"] + 1, 9)))
            ).isoformat(),
        )
    return True
