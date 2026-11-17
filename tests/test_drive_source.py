import httpx
import pytest

from factory.drive import Drive
from factory.drive_source import DriveFile
from factory.youtube_oauth import YouTubeError


class Tokens:
    def __init__(self):
        self.refreshed = False

    def get(self, refresh=False):
        self.refreshed |= refresh
        return "fixture-token"


def test_drive_range_reads_are_bounded_and_seekable():
    data = b"abcdefghij"
    calls = []

    def request(req):
        calls.append(req.headers["range"])
        start, end = map(int, req.headers["range"].removeprefix("bytes=").split("-"))
        return httpx.Response(
            206,
            content=data[start : end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/10"},
        )

    with httpx.Client(transport=httpx.MockTransport(request)) as http:
        source = DriveFile(Drive(http, Tokens()), "managed-file", 10)
        assert source.stat().st_size == 10
        with source.open("rb") as handle:
            assert handle.read(4) == b"abcd"
            handle.seek(7)
            assert handle.read(4) == b"hij"
            assert handle.read(4) == b""
    assert calls == ["bytes=0-3", "bytes=7-9"]


def test_drive_read_refreshes_and_rejects_full_response():
    tokens = Tokens()
    calls = []

    def request(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(
            206, content=b"ab", headers={"Content-Range": "bytes 0-1/2"}
        )

    with httpx.Client(transport=httpx.MockTransport(request)) as http:
        assert DriveFile(Drive(http, tokens), "managed-file", 2).read(2) == b"ab"
    assert tokens.refreshed
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as http:
        with pytest.raises(YouTubeError, match="range_not_confirmed"):
            DriveFile(Drive(http, Tokens()), "managed-file", 2).read(2)


def test_drive_truncated_response_is_retryable():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                206, content=b"a", headers={"Content-Range": "bytes 0-1/2"}
            )
        )
    ) as http:
        with pytest.raises(YouTubeError) as exc:
            DriveFile(Drive(http, Tokens()), "managed-file", 2).read(2)
    assert exc.value.retry


@pytest.mark.parametrize("wrong_parent", [False, True])
def test_resolver_only_accepts_verified_managed_job_folder(monkeypatch, wrong_parent):
    from factory import drive_source
    from factory.drive import FOLDER, FOLDER_MIME, MARKER
    from factory.clips_db import archives
    from factory.db import stages
    from factory.drive_worker import settings

    sha = "a" * 64

    class Ledger:
        def list_rows(self, table, *where):
            if table is archives:
                return [
                    {
                        "state": "ARCHIVED",
                        "folder_id": "job-folder",
                        "files": {
                            "part_01.mp4": {
                                "id": "managed-file",
                                "sha256": sha,
                                "url": "https://drive.google.com/file/d/managed-file/view",
                            }
                        },
                    }
                ]
            if table is settings:
                return [{"confirmed": 1, "file_id": "managed-root"}]
            if table is stages:
                return [{"result": {"parts": [{"name": "part_01.mp4", "size": 2}]}}]
            raise AssertionError(table)

    responses = {
        "managed-root": {
            "id": "managed-root",
            "name": FOLDER,
            "mimeType": FOLDER_MIME,
            "ownedByMe": True,
            "trashed": False,
            "appProperties": {"factory": MARKER},
        },
        "job-folder": {
            "id": "job-folder",
            "parents": ["unrelated" if wrong_parent else "managed-root"],
            "trashed": False,
            "appProperties": {"factory": MARKER, "job": "job-fixture"},
        },
        "managed-file": {
            "id": "managed-file",
            "parents": ["job-folder"],
            "mimeType": "video/mp4",
            "trashed": False,
            "appProperties": {"sha256": sha},
            "size": "2",
        },
    }
    called = []

    def request(req):
        key = req.url.path.rsplit("/", 1)[1]
        called.append(key)
        assert req.url.params.get("alt") != "media"
        return httpx.Response(200, json=responses[key])

    monkeypatch.setattr(drive_source, "load_tokens", lambda _: Tokens())
    with httpx.Client(transport=httpx.MockTransport(request)) as http:
        if wrong_parent:
            with pytest.raises(YouTubeError, match="unavailable_or_unverified"):
                drive_source.resolve(
                    Ledger(), {"job": "job-fixture", "part": 1, "sha256": sha}, http
                )
            assert "managed-file" not in called
        else:
            source = drive_source.resolve(
                Ledger(), {"job": "job-fixture", "part": 1, "sha256": sha}, http
            )
            assert source.stat().st_size == 2
    assert set(called) <= set(responses)
