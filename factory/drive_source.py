"""Bounded, authenticated Drive reads for an already approved YouTube publication."""

from types import SimpleNamespace

import httpx

from factory.drive import API, MARKER, Drive, valid_id
from factory.drive_oauth import DriveError, load_tokens
from factory.youtube_oauth import YouTubeError


class DriveFile:
    """Small seekable facade; a read never buffers more than one upload chunk."""

    def __init__(self, drive, file_id, size):
        self.drive, self.file_id, self.size = drive, valid_id(file_id), size
        self.offset = 0

    def stat(self):
        return SimpleNamespace(st_size=self.size)

    def open(self, mode):
        if mode != "rb":
            raise ValueError("Drive publication sources are read-only")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def seek(self, offset):
        if not 0 <= offset <= self.size:
            raise YouTubeError("youtube_drive_invalid_offset")
        self.offset = offset

    def read(self, count):
        if not 0 < count <= 64 * 1024**2:
            raise YouTubeError("youtube_drive_invalid_read_size")
        count = min(count, self.size - self.offset)
        if not count:
            return b""
        end = self.offset + count - 1
        try:
            for refresh in (False, True):
                with self.drive.http.stream(
                    "GET",
                    f"{API}/files/{self.file_id}",
                    params={"alt": "media"},
                    headers={
                        "Authorization": "Bearer "
                        + self.drive.tokens.get(refresh=refresh),
                        "Range": f"bytes={self.offset}-{end}",
                    },
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 401 and not refresh:
                        continue
                    if (
                        response.status_code != 206
                        or response.headers.get("Content-Range")
                        != f"bytes {self.offset}-{end}/{self.size}"
                    ):
                        raise YouTubeError(
                            "youtube_drive_range_not_confirmed",
                            retry=response.status_code
                            in {408, 429, 500, 502, 503, 504},
                        )
                    chunks, size = [], 0
                    for chunk in response.iter_bytes(chunk_size=256 * 1024):
                        size += len(chunk)
                        if size > count:
                            raise YouTubeError("youtube_drive_read_exceeded_range")
                        chunks.append(chunk)
                    if size != count:
                        raise YouTubeError("youtube_drive_incomplete_read", retry=True)
                    self.offset += size
                    return b"".join(chunks)
        except DriveError:
            raise YouTubeError(
                "youtube_drive_reauthorization_required", auth=True
            ) from None
        except httpx.RequestError:
            raise YouTubeError("youtube_drive_network_error", retry=True) from None
        raise YouTubeError("youtube_drive_reauthorization_required", auth=True)


def resolve(ledger, snapshot, http):
    from factory.clips_db import archives
    from factory.db import stages
    from factory.drive_worker import settings

    records = ledger.list_rows(archives, archives.c.job == snapshot["job"])
    roots = ledger.list_rows(settings, settings.c.key == "folder")
    packages = ledger.list_rows(
        stages, stages.c.job == snapshot["job"], stages.c.name == "package"
    )
    if (
        not records
        or records[0]["state"] != "ARCHIVED"
        or not roots
        or not roots[0]["confirmed"]
        or not packages
    ):
        raise YouTubeError("youtube_verified_drive_archive_required")
    archive, package = records[0], packages[0]["result"]
    part = package["parts"][snapshot["part"] - 1]
    item = archive["files"].get(part["name"], {})
    if (
        not item.get("id")
        or item.get("sha256") != snapshot["sha256"]
        or not item.get("url")
    ):
        raise YouTubeError("youtube_drive_source_not_verified")
    try:
        drive = Drive(http, load_tokens(http))
        root = roots[0]["file_id"]
        root_info = drive.get_file(root)
        if root_info is None:
            raise DriveError("drive_destination_missing")
        drive.verify_folder(root, root_info)
        child = drive.get_file(archive["folder_id"])
        if (
            not child
            or child.get("parents") != [root]
            or child.get("trashed") is not False
            or child.get("appProperties", {})
            != {"factory": MARKER, "job": snapshot["job"]}
        ):
            raise DriveError("drive_job_folder_invalid")
        remote = drive.get_file(item["id"])
        if remote is None:
            raise DriveError("drive_video_missing")
        drive.verify_upload(
            remote,
            item["id"],
            archive["folder_id"],
            snapshot["sha256"],
            part["size"],
            "video/mp4",
        )
        return DriveFile(drive, item["id"], part["size"])
    except (DriveError, OSError, ValueError):
        raise YouTubeError("youtube_drive_source_unavailable_or_unverified") from None
