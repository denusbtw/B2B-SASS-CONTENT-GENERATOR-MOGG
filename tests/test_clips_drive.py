"""Drive transport fixture tests: folder recovery, cross-process session resume, dedup."""

import hashlib
import json

import httpx
from sqlalchemy import insert, update

from factory import storage
from factory.clips_archive import archive_next
from factory.clips_db import archives
from factory.db import stages, now
from factory.drive import UPLOAD, Drive, FOLDER_MIME, MARKER
from factory.drive_oauth import DriveError
from test_clips import setup  # noqa: F401, F811


class Tokens:
    def get(self, refresh=False):
        return "test-access"


class Transport:
    def __init__(self):
        self.files = {}
        self.sessions = {}
        self.counter = 0
        self.finished = []
        self.interrupt = True
        self.resumed = []

    def __call__(self, request):
        path = request.url.path
        if path.endswith("/generateIds"):
            self.counter += 1
            return httpx.Response(200, json={"ids": [f"id_{self.counter}"]})
        if path == "/drive/v3/files" and request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if path == "/drive/v3/files" and request.method == "POST":
            data = json.loads(request.content)
            data.update(ownedByMe=True, trashed=False)
            self.files[data["id"]] = data
            return httpx.Response(200, json=data)
        if path.startswith("/drive/v3/files/"):
            data = self.files.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=data) if data else httpx.Response(404)
        if request.method == "POST" and str(request.url).startswith(UPLOAD):
            data = json.loads(request.content)
            session = data["id"]
            self.sessions[session] = {
                "meta": data,
                "bytes": bytearray(),
                "size": int(request.headers["X-Upload-Content-Length"]),
            }
            return httpx.Response(
                200, headers={"Location": UPLOAD + "?session=" + session}
            )
        if request.method == "PUT":
            session = request.url.params["session"]
            record = self.sessions[session]
            offset = len(record["bytes"])
            if (
                record["meta"]["name"] == "part_02.mp4"
                and offset > 0
                and self.interrupt
            ):
                return httpx.Response(503)
            if request.headers["Content-Range"].startswith("bytes */"):
                if offset:
                    self.resumed.append(offset)
                return httpx.Response(
                    308, headers={"Range": f"bytes=0-{offset - 1}"} if offset else {}
                )
            start = int(request.headers["Content-Range"].split(" ")[1].split("-")[0])
            assert start == offset
            record["bytes"].extend(request.content)
            if len(record["bytes"]) < record["size"]:
                return httpx.Response(
                    308, headers={"Range": f"bytes=0-{len(record['bytes']) - 1}"}
                )
            data = record["meta"]
            assert (
                hashlib.sha256(record["bytes"]).hexdigest()
                == data["appProperties"]["sha256"]
            )
            data.update(size=str(len(record["bytes"])), trashed=False)
            self.files[session] = data
            self.finished.append(data["name"])
            return httpx.Response(200, json=data)
        raise AssertionError(str(request.url))


def test_job_folder_six_uploads_and_resume(setup):  # noqa: F811
    client, ledger, tmp = setup
    _, [job] = ledger.submit([{"mode": "STORY"}])
    parts = []
    for i in range(1, 6):
        key = storage.put(bytes([i]) * (1024 * 1024 + 100))
        parts.append({"name": f"part_{i:02}.mp4", "sha256": key})
    manifest = storage.put(json.dumps({"parts": parts}).encode())
    with ledger.write() as db:
        db.execute(
            update(stages)
            .where(stages.c.job == job, stages.c.name == "package")
            .values(
                state="SUCCEEDED",
                result={
                    "real_clips": True,
                    "source_metadata": {"title": "Example / Video"},
                    "parts": parts,
                    "manifest_sha256": manifest,
                },
            )
        )
        db.execute(
            insert(archives).values(
                job=job, state="PENDING", files={}, attempts=0, due=now().isoformat()
            )
        )
    transport = Transport()
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        drive = Drive(http, Tokens())
        assert archive_next(ledger, drive)
        row = ledger.list_rows(archives)[0]
        assert row["state"] == "ARCHIVE_FAILED"
        assert row["files"]["part_01.mp4"]["url"]
        assert row["files"]["part_02.mp4"]["session"]
        folder = row["folder_id"]
        assert transport.files[folder]["mimeType"] == FOLDER_MIME
        assert transport.files[folder]["appProperties"] == {
            "factory": MARKER,
            "job": job,
        }
        transport.interrupt = False
        response = client.post(f"/api/v1/clips/jobs/{job}/archive/retry")
        assert response.status_code == 200, response.text
        # A new Drive instance models a restarted worker; state is recovered from DB.
        assert archive_next(ledger, Drive(http, Tokens()))
        row = ledger.list_rows(archives)[0]
        assert row["state"] == "ARCHIVED" and row["folder_id"] == folder
        assert len(row["files"]) == 6 and all(
            v.get("url") for v in row["files"].values()
        )
        assert transport.finished == [f"part_{i:02}.mp4" for i in range(1, 6)] + [
            "manifest.json"
        ]
        assert 1024 * 1024 in transport.resumed
        assert not archive_next(ledger, drive)
        assert len(transport.finished) == 6
        assert (
            len(
                [
                    f
                    for f in transport.files.values()
                    if f.get("mimeType") == FOLDER_MIME
                ]
            )
            == 2
        )


def test_drive_auth_and_rate_errors():
    for status, body, code, retry in [
        (401, {}, "oauth_reconnect_required", False),
        (
            403,
            {"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
            "drive_rate_limited",
            True,
        ),
    ]:
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json=body)
            )
        ) as http:
            try:
                Drive(http, Tokens()).get_file("file")
            except DriveError as exc:
                assert exc.code == code and exc.retry == retry
            else:
                raise AssertionError("Expected DriveError")
