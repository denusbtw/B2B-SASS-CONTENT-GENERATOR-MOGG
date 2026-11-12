import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import update

from factory import drive, storage
from factory.db import Ledger, metadata, now
from factory.drive import API, FOLDER, FOLDER_MIME, MARKER, Drive
from factory.drive_oauth import SCOPE, AccessTokens, DriveError, OAuthClient
from factory.drive_worker import (
    archive_next,
    drive_metadata,
    filename,
    settings,
    uploads,
)


def tokens(http):
    return AccessTokens(
        http, OAuthClient("desktop-id", "desktop-secret"), "drive-refresh"
    )


def folder_data():
    return {
        "id": "folder_1",
        "name": FOLDER,
        "mimeType": FOLDER_MIME,
        "ownedByMe": True,
        "trashed": False,
        "appProperties": {"factory": MARKER},
    }


def test_authorization_uses_pkce_and_drive_file_only():
    client = OAuthClient("client", "secret")
    url = client.authorization_url(
        "http://127.0.0.1:9999/oauth/callback", "csrf", "verifier"
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["scope"] == [SCOPE]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert query["state"] == ["csrf"]
    assert query["code_challenge"] != ["verifier"]
    assert "client_secret" not in query


def test_code_exchange_and_scope_rejection():
    responses = [SCOPE, "https://www.googleapis.com/auth/drive"]

    def handler(request):
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == ["authorization_code"]
        assert body["code_verifier"] == ["proof"]
        assert request.url.host == "oauth2.googleapis.com"
        return httpx.Response(
            200,
            json={
                "access_token": "one",
                "refresh_token": "refresh",
                "scope": responses.pop(0),
                "expires_in": 3600,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = OAuthClient("client", "secret")
        form = {
            "grant_type": "authorization_code",
            "code": "temporary",
            "code_verifier": "proof",
        }
        assert client.exchange(http, form)["refresh_token"] == "refresh"
        with pytest.raises(DriveError, match="scope_mismatch"):
            client.exchange(http, form)


def test_refresh_after_expiry_and_401(monkeypatch):
    calls = []
    clock = [0.0]
    monkeypatch.setattr("factory.drive_oauth.monotonic", lambda: clock[0])

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            assert form["refresh_token"] == ["drive-refresh"]
            assert form["grant_type"] == ["refresh_token"]
            calls.append("refresh")
            return httpx.Response(
                200, json={"access_token": f"access-{len(calls)}", "expires_in": 3600}
            )
        if request.headers["Authorization"] == "Bearer access-1":
            return httpx.Response(401)
        return httpx.Response(200, json=folder_data())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        access = tokens(http)
        api = Drive(http, access)
        assert api.get_file("folder_1") == folder_data()
        assert access.get() == "access-2"
        clock[0] = 3600
        assert access.get() == "access-3"
    assert calls == ["refresh", "refresh", "refresh"]


def test_folder_selection_and_fail_closed():
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "access", "expires_in": 3600}
            )
        if request.url.path == "/drive/v3/files":
            query = request.url.params["q"]
            assert (
                FOLDER in query
                and "appProperties has" in query
                and "trashed = false" in query
            )
            return httpx.Response(200, json={"files": [folder_data()]})
        assert request.method == "GET"
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        api = Drive(http, tokens(http))
        assert api.find_folder() == "folder_1"
        with pytest.raises(DriveError, match="destination_unavailable"):
            api.prepare_folder("folder_1", confirmed=True)
    with pytest.raises(DriveError, match="destination_invalid"):
        Drive.verify_folder("folder_1", {**folder_data(), "appProperties": {}})


def test_archive_retry_reuses_remote_id_and_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "ROOT", tmp_path / "files")
    monkeypatch.setattr(drive, "CHUNK_SIZE", 4)
    data = b"demo-mp4"
    sha = storage.put(data)
    ledger = Ledger(f"sqlite:///{tmp_path / 'db.sqlite'}")
    metadata.create_all(ledger.engine)
    drive_metadata.create_all(ledger.engine)
    for _ in range(2):
        ledger.submit([{"mode": "STORY"}])
        for result in ({}, {}, {}, {"publishable": True, "parts": [{"sha256": sha}]}):
            claim = ledger.claim("test")
            ledger.finish(claim["id"], "test", claim["token"], result=result)
    created = {}
    requests = []
    session = "https://www.googleapis.com/upload/drive/v3/files?upload_id=session"
    chunk_count = 0

    def handler(request):
        nonlocal chunk_count
        route = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "access", "expires_in": 3600}
            )
        requests.append((request.method, route))
        if route.endswith("/generateIds"):
            allocated = "folder_1" if "folder_1" not in created else "video_1"
            return httpx.Response(200, json={"ids": [allocated]})
        if route == "/drive/v3/files" and request.method == "GET":
            return httpx.Response(200, json={"files": []})
        if route == "/drive/v3/files" and request.method == "POST":
            assert b"folder_1" in request.content
            created["folder_1"] = folder_data()
            return httpx.Response(200, json=created["folder_1"])
        if route.startswith("/drive/v3/files/"):
            item = created.get(route.split("/")[-1])
            # The first reconciling GET temporarily cannot see the successfully stored file.
            if "video_1" in route and chunk_count == 2:
                chunk_count += 1
                return httpx.Response(503)
            return httpx.Response(200, json=item) if item else httpx.Response(404)
        if request.method == "POST":
            assert request.url.params["uploadType"] == "resumable"
            import json

            item = json.loads(request.content)
            assert item["parents"] == ["folder_1"]
            assert item["name"].startswith("export-") and item["name"].endswith(".mp4")
            assert item["id"] == "video_1"
            return httpx.Response(200, headers={"Location": session})
        if request.headers["Content-Range"] == "bytes */8":
            return httpx.Response(503)
        chunk_count += 1
        if chunk_count == 1:
            assert request.headers["Content-Range"] == "bytes 0-3/8"
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        assert request.headers["Content-Range"] == "bytes 4-7/8"
        created["video_1"] = {
            "id": "video_1",
            "parents": ["folder_1"],
            "trashed": False,
            "mimeType": "video/mp4",
            "size": len(data),
            "appProperties": {"sha256": hashlib.sha256(data).hexdigest()},
        }
        raise httpx.ReadTimeout("lost final response", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        api = Drive(http, tokens(http))
        assert archive_next(ledger, api, "export-{part}.mp4")
        [failed] = ledger.list_rows(uploads)
        assert failed["remote_id"] == "video_1" and failed["url"] is None
        assert failed["error"] == "drive_temporarily_unavailable"
        for _ in range(2):
            with ledger.write() as db:
                db.execute(update(uploads).values(due=now().isoformat()))
            assert archive_next(ledger, api, "export-{part}.mp4")
        [saved] = ledger.list_rows(uploads)
        assert saved["url"] == "https://drive.google.com/file/d/video_1/view"
        assert ledger.list_rows(settings)[0]["file_id"] == "folder_1"
        assert not archive_next(ledger, api, "export-{part}.mp4")
    assert requests.count(("POST", "/upload/drive/v3/files")) == 1
    assert requests.count(("POST", "/drive/v3/files")) == 1
    ledger.engine.dispose()


def test_upload_rejects_untrusted_session(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "access", "expires_in": 3600}
            )
        assert str(request.url).startswith("https://www.googleapis.com")
        if request.method == "GET":
            assert request.url.path == urlsplit(API).path + "/files/video_1"
            return httpx.Response(404)
        return httpx.Response(200, headers={"Location": "https://example.org/steal"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DriveError, match="invalid_upload_session"):
            Drive(http, tokens(http)).upload(
                path, "demo.mp4", "video_1", "folder_1", "abc"
            )


@pytest.mark.parametrize(
    "template", ["../{part}.mp4", "{unknown}.mp4", "{part:99999}.mp4"]
)
def test_filename_validation(template):
    with pytest.raises(DriveError):
        filename(template, "job", 1, "a" * 64)


def test_upload_queries_session_after_network_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(drive, "CHUNK_SIZE", 4)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video123")
    ranges = []
    metadata = {
        "id": "video_1",
        "mimeType": "video/mp4",
        "parents": ["folder_1"],
        "trashed": False,
        "size": "8",
        "appProperties": {"sha256": "abc"},
    }

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "access", "expires_in": 3600}
            )
        if request.method == "GET":
            return httpx.Response(404)
        if request.method == "POST":
            return httpx.Response(
                200,
                headers={
                    "Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=test",
                },
            )
        ranges.append(request.headers["Content-Range"])
        if len(ranges) == 1:
            raise httpx.ReadTimeout("lost chunk response", request=request)
        if len(ranges) == 2:
            assert not request.content
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        return httpx.Response(201, json=metadata)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert (
            Drive(http, tokens(http))
            .upload(path, "demo.mp4", "video_1", "folder_1", "abc")
            .endswith("/video_1/view")
        )
    assert ranges == ["bytes 0-3/8", "bytes */8", "bytes 4-7/8"]


def test_create_conflict_checks_only_the_reserved_id(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    seen = []
    metadata = {
        "id": "video_1",
        "mimeType": "video/mp4",
        "parents": ["wrong_folder"],
        "trashed": False,
        "size": "5",
        "appProperties": {"sha256": "abc"},
    }

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "access", "expires_in": 3600}
            )
        seen.append((request.method, request.url.path))
        if len(seen) == 1:
            return httpx.Response(404)
        if request.method == "POST":
            return httpx.Response(409)
        return httpx.Response(200, json=metadata)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DriveError, match="metadata_mismatch"):
            Drive(http, tokens(http)).upload(
                path, "demo.mp4", "video_1", "folder_1", "abc"
            )
    assert seen == [
        ("GET", "/drive/v3/files/video_1"),
        ("POST", "/upload/drive/v3/files"),
        ("GET", "/drive/v3/files/video_1"),
    ]
