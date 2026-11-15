"""Offline production-adapter tests. All Google endpoints use MockTransport."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from time import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from factory import api, storage, youtube_api
from factory.db import Ledger, metadata, now, stages, targets, uid
from factory.media import ffmpeg
from factory.publisher import MockPublisher, dispatch
from factory.youtube import YouTubePublisher
from factory.youtube_db import connections, runtime, youtube_metadata
from factory.youtube_oauth import (
    AccessTokens,
    OAuthConfig,
    SCOPES,
    YouTubeError,
    begin,
    consume,
    decrypt,
    encrypt,
)
from factory.youtube_publications import get, publication_lock
from main import app

CHANNEL = "UC" + "a" * 22
VIDEO = "video000001"


class Google:
    def __init__(self):
        self.sessions = {}
        self.starts = 0
        self.uploaded = []
        self.chunks = []
        self.fail_chunk = False
        self.lose_final = False
        self.crash_init = False
        self.expired = False
        self.init_status = 200
        self.error_reason = ""
        self.refreshes = 0
        self.invalid_refresh = False
        self.remote_privacy = None
        self.channel = CHANNEL
        self.entered = None
        self.release = None
        self.paused = False

    def __call__(self, request):
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            if path == "/revoke":
                return httpx.Response(200)
            self.refreshes += 1
            if self.invalid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "access-test",
                    "refresh_token": "refresh-test",
                    "expires_in": 3600,
                    "scope": " ".join(SCOPES),
                },
            )
        assert request.headers["Authorization"] == "Bearer access-test"
        if path == "/youtube/v3/channels":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": self.channel, "snippet": {"title": "Test channel"}}
                    ]
                },
            )
        if path == "/youtube/v3/videos":
            if not self.uploaded:
                return httpx.Response(200, json={"items": []})
            item = json.loads(json.dumps(self.uploaded[-1]))
            item["status"]["uploadStatus"] = "processed"
            if self.remote_privacy:
                item["status"]["privacyStatus"] = self.remote_privacy
            item["processingDetails"] = {"processingStatus": "succeeded"}
            return httpx.Response(200, json={"items": [item]})
        assert path == "/upload/youtube/v3/videos"
        if request.method == "POST":
            if self.init_status != 200:
                return httpx.Response(
                    self.init_status,
                    json={"error": {"errors": [{"reason": self.error_reason}]}},
                    headers={"Retry-After": "10"},
                )
            self.starts += 1
            key = str(self.starts)
            self.sessions[key] = {
                "metadata": json.loads(request.content),
                "size": int(request.headers["X-Upload-Content-Length"]),
                "bytes": bytearray(),
                "complete": False,
            }
            if self.crash_init:
                raise KeyboardInterrupt("simulated worker crash")
            return httpx.Response(
                200,
                headers={
                    "Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id="
                    + key
                },
            )
        key = request.url.params["upload_id"]
        if self.expired:
            return httpx.Response(404)
        session = self.sessions[key]
        offset = len(session["bytes"])
        if request.headers["Content-Range"].startswith("bytes */"):
            if session["complete"]:
                return httpx.Response(201, json=self.uploaded[-1])
            return httpx.Response(
                308, headers={"Range": f"bytes=0-{offset - 1}"} if offset else {}
            )
        if self.entered and not self.paused:
            self.paused = True
            self.entered.set()
            assert self.release.wait(10)
        if self.fail_chunk and offset > 0:
            self.fail_chunk = False
            raise httpx.ReadTimeout(
                "sensitive-session-url-must-not-be-logged", request=request
            )
        first = int(request.headers["Content-Range"].split(" ")[1].split("-")[0])
        assert first == offset
        session["bytes"].extend(request.content)
        self.chunks.append(first)
        if len(session["bytes"]) < session["size"]:
            return httpx.Response(
                308, headers={"Range": f"bytes=0-{len(session['bytes']) - 1}"}
            )
        session["complete"] = True
        data = {**session["metadata"], "id": VIDEO}
        data["snippet"]["channelId"] = self.channel
        data["status"]["uploadStatus"] = "uploaded"
        self.uploaded.append(data)
        if self.lose_final:
            self.lose_final = False
            raise httpx.ReadTimeout("lost final response", request=request)
        return httpx.Response(201, json=data)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    path = tmp_path_factory.mktemp("youtube-media") / "test.mp4"
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    return path.read_bytes()


@pytest.fixture
def setup(monkeypatch, tmp_path, media):
    ledger = Ledger(f"sqlite:///{tmp_path / 'youtube.db'}")
    metadata.create_all(ledger.engine)
    youtube_metadata.create_all(ledger.engine)
    monkeypatch.setattr(api, "ledger", ledger)
    monkeypatch.setattr(storage, "ROOT", tmp_path / "storage")
    for name, value in {
        "PUBLISHER_PROVIDER": "youtube",
        "YOUTUBE_ENABLED": "true",
        "YOUTUBE_CLIENT_ID": "test-client",
        "YOUTUBE_CLIENT_SECRET": "test-secret",
        "YOUTUBE_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "YOUTUBE_REDIRECT_URI": "http://testserver/api/v1/integrations/youtube/callback",
        "YOUTUBE_UPLOAD_CHUNK_BYTES": str(256 * 1024),
        "OPERATOR_TOKEN": "operator",
    }.items():
        monkeypatch.setenv(name, value)
    # Test callback host is only for TestClient; production config allows loopback HTTP.
    monkeypatch.setenv(
        "YOUTUBE_REDIRECT_URI",
        "http://127.0.0.1:8080/api/v1/integrations/youtube/callback",
    )
    google = Google()
    monkeypatch.setattr(
        youtube_api,
        "http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(google)),
    )
    revision = uid()
    with ledger.write() as db:
        db.execute(
            insert(connections).values(
                id="youtube",
                channel_id=CHANNEL,
                channel_title="Test channel",
                revision=revision,
                state="CONNECTED",
                credentials=encrypt(
                    {
                        "access_token": "access-test",
                        "refresh_token": "refresh-test",
                        "expires_at": time() + 3600,
                    }
                ),
                updated=now().isoformat(),
            )
        )
    key = storage.put(media)
    _, [job] = ledger.submit(
        [
            {
                "mode": "STORY",
                "prompt": "Authorized fixture",
                "rights": "original",
                "evidence": "Generated in offline test",
            }
        ]
    )
    with ledger.write() as db:
        db.execute(
            update(stages)
            .where(stages.c.job == job, stages.c.name == "package")
            .values(
                state="SUCCEEDED",
                result={
                    "publishable": True,
                    "parts": [{"sha256": key}],
                    "manifest_hash": "fixture-manifest",
                    "review": {"decision": "pass", "media": [{"passed": True}]},
                },
            )
        )
    body = {
        "job": job,
        "part": 1,
        "channel_id": CHANNEL,
        "title": "Approved test",
        "description": "Description",
        "made_for_kids": False,
        "contains_synthetic_media": False,
    }
    with TestClient(app, headers={"Authorization": "operator"}) as client:
        yield client, ledger, google, body
    ledger.engine.dispose()


def draft(setup, **extra):
    client, _, _, body = setup
    response = client.post("/api/v1/publications/youtube", json={**body, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def approved(setup, **extra):
    client, _, _, _ = setup
    row = draft(setup, **extra)
    response = client.post(
        f"/api/v1/publications/{row['id']}/approve",
        json={
            "snapshot_hash": row["snapshot_hash"],
            "channel_id": CHANNEL,
            "approve_live_upload": True,
            "approve_public": extra.get("visibility") == "public",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def worker(setup):
    _, ledger, google, _ = setup
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        return dispatch(ledger, YouTubePublisher(http, AccessTokens(ledger, http)))


def due(ledger, key):
    with ledger.write() as db:
        db.execute(
            update(targets)
            .where(targets.c.id == key)
            .values(due=(now() - timedelta(seconds=1)).isoformat())
        )


def test_oauth_url_state_pkce_and_browser_binding(setup):
    client, ledger, _, _ = setup
    response = client.get("/api/v1/integrations/youtube/connect")
    assert response.status_code == 200
    query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert query["scope"] == [" ".join(SCOPES)]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert "client_secret" not in query
    assert "HttpOnly" in response.headers["set-cookie"]
    state = query["state"][0]
    with pytest.raises(YouTubeError, match="browser_mismatch"):
        consume(ledger, state, "different-browser")
    assert (
        client.get(
            "/api/v1/integrations/youtube/callback",
            params={"code": "authorization-code", "state": state},
        ).status_code
        == 200
    )
    status = client.get("/api/v1/integrations/youtube/status").json()
    assert status["connection"]["state"] == "CONFIRM_CHANNEL"
    assert "credentials" not in status["connection"]
    assert (
        client.post(
            "/api/v1/integrations/youtube/confirm",
            json={"channel_id": "wrong", "revision": status["connection"]["revision"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/integrations/youtube/confirm",
            json={"channel_id": CHANNEL, "revision": status["connection"]["revision"]},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/integrations/youtube/callback",
            params={"code": "authorization-code", "state": state},
        ).status_code
        == 422
    )
    with pytest.raises(YouTubeError):
        consume(ledger, "invalid", "browser")


def test_expired_state_and_refresh_encryption(setup):
    _, ledger, google, _ = setup
    from factory.youtube_db import oauth_states

    url = begin(ledger, "browser")
    state = parse_qs(urlsplit(url).query)["state"][0]
    with ledger.write() as db:
        db.execute(update(oauth_states).values(expires="2000-01-01"))
        db.execute(
            update(connections).values(
                credentials=encrypt({"refresh_token": "refresh-test", "expires_at": 0})
            )
        )
    with pytest.raises(YouTubeError, match="state_invalid"):
        consume(ledger, state, "browser")
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        assert AccessTokens(ledger, http).get() == "access-test"
    row = ledger.list_rows(connections)[0]
    assert (
        "refresh-test" not in row["credentials"]
        and "access-test" not in row["credentials"]
    )
    assert decrypt(row["credentials"])["expires_at"] > time()


def test_revoked_credentials_and_disconnect(setup):
    client, ledger, google, _ = setup
    google.invalid_refresh = True
    with ledger.write() as db:
        db.execute(
            update(connections).values(
                credentials=encrypt({"refresh_token": "refresh-test", "expires_at": 0})
            )
        )
    with httpx.Client(transport=httpx.MockTransport(google)) as http:
        with pytest.raises(YouTubeError) as exc:
            AccessTokens(ledger, http).get()
        assert exc.value.auth
    assert ledger.list_rows(connections)[0]["state"] == "AUTH_REQUIRED"
    assert client.post("/api/v1/integrations/youtube/disconnect").status_code == 200
    assert ledger.list_rows(connections) == []


def test_private_upload_verified_separately(setup):
    client, ledger, google, _ = setup
    row = approved(setup)
    assert worker(setup)
    saved = client.get("/api/v1/publications/" + row["id"]).json()
    assert saved["state"] == "UPLOADED_PROCESSING" and saved["post_id"] == VIDEO
    assert saved["url"] == "https://www.youtube.com/watch?v=" + VIDEO
    assert saved["upload"]["offset"] == saved["upload"]["size"]
    assert saved["upload"]["verified"] is None
    assert "session" not in json.dumps(saved)
    metadata = google.sessions["1"]["metadata"]
    assert metadata["status"]["privacyStatus"] == "private"
    assert metadata["status"]["selfDeclaredMadeForKids"] is False
    assert metadata["status"]["containsSyntheticMedia"] is False
    due(ledger, row["id"])
    assert worker(setup)
    saved = client.get("/api/v1/publications/" + row["id"]).json()
    assert saved["state"] == "UPLOADED_PRIVATE" and saved["upload"]["verified"]
    assert google.starts == 1


def test_interrupted_upload_resumes_offset(setup):
    client, ledger, google, _ = setup
    row = approved(setup)
    google.fail_chunk = True
    assert worker(setup)
    assert get(ledger, row["id"])[0]["state"] == "RETRY_PENDING"
    assert "sensitive" not in json.dumps(
        client.get("/api/v1/publications/" + row["id"]).json()
    )
    due(ledger, row["id"])
    assert worker(setup)
    assert google.starts == 1 and google.chunks.count(0) == 1
    assert get(ledger, row["id"])[0]["post_id"] == VIDEO


def test_lost_completion_response_does_not_duplicate(setup):
    _, ledger, google, _ = setup
    row = approved(setup)
    google.lose_final = True
    assert worker(setup)
    assert len(google.uploaded) == 1
    assert get(ledger, row["id"])[0]["state"] == "RETRY_PENDING"
    due(ledger, row["id"])
    assert worker(setup)
    assert google.starts == 1 and len(google.uploaded) == 1
    assert get(ledger, row["id"])[0]["post_id"] == VIDEO


def test_crash_during_session_creation_requires_intervention(setup):
    client, ledger, google, _ = setup
    row = approved(setup)
    google.crash_init = True
    with pytest.raises(KeyboardInterrupt):
        worker(setup)
    with ledger.write() as db:
        db.execute(
            update(runtime)
            .where(runtime.c.id == row["id"])
            .values(expires="2000-01-01")
        )
    google.crash_init = False
    assert worker(setup)
    assert get(ledger, row["id"])[0]["state"] == "NEEDS_RECONCILIATION"
    assert google.starts == 1
    assert client.post(f"/api/v1/publications/{row['id']}/retry").status_code == 409
    assert (
        client.post(f"/api/v1/publications/{row['id']}/reconcile", json={}).status_code
        == 409
    )


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (429, "", "RETRY_PENDING"),
        (403, "rateLimitExceeded", "RETRY_PENDING"),
        (403, "quotaExceeded", "FAILED"),
        (400, "invalidTitle", "FAILED"),
        (403, "forbidden", "FAILED"),
    ],
)
def test_remote_errors_are_not_masked(setup, status, reason, expected):
    _, ledger, google, _ = setup
    row = approved(setup)
    google.init_status, google.error_reason = status, reason
    assert worker(setup)
    target, upload = get(ledger, row["id"])
    assert target["state"] == expected
    assert target["post_id"] is None and upload["error"]
    assert google.starts == 0


def test_approval_and_mock_separation(setup):
    client, ledger, google, body = setup
    row = draft(setup)
    assert not worker(setup)
    assert (
        client.post(
            f"/api/v1/publications/{row['id']}/approve",
            json={
                "snapshot_hash": "a" * 64,
                "channel_id": CHANNEL,
                "approve_live_upload": True,
            },
        ).status_code
        == 409
    )
    ledger.approve(
        [
            {
                "account": "mock-youtube-a",
                "sha256": row["snapshot"]["sha256"],
                "scheduled_at": now().isoformat(),
                "provider": "mock",
            }
        ]
    )
    assert not worker(setup)
    assert dispatch(ledger, MockPublisher())
    assert google.starts == 0
    assert get(ledger, row["id"])[0]["state"] == "DRAFT"


def test_public_schedule_and_remote_reconciliation(setup):
    client, ledger, google, _ = setup
    timestamp = (now() + timedelta(hours=2)).isoformat()
    row = approved(
        setup,
        visibility="public",
        publish_at=timestamp,
        tags=["test"],
        category_id="22",
    )
    assert worker(setup)
    target, _ = get(ledger, row["id"])
    assert target["state"] != "PUBLISHED"
    status = google.sessions["1"]["metadata"]["status"]
    assert status["privacyStatus"] == "private" and datetime.fromisoformat(
        status["publishAt"]
    ) == datetime.fromisoformat(timestamp)
    response = client.post(f"/api/v1/publications/{row['id']}/reconcile", json={})
    assert response.status_code == 200 and response.json()["state"] == "SCHEDULED"
    google.remote_privacy = "public"
    response = client.post(f"/api/v1/publications/{row['id']}/reconcile", json={})
    assert response.json()["state"] == "PUBLISHED"
    assert response.json()["upload"]["verified"]


def test_public_approval_missing_and_invalid_metadata(setup):
    client, _, _, body = setup
    for changes in [
        {"made_for_kids": None},
        {"contains_synthetic_media": None},
        {"title": ""},
        {"publish_at": "2026-01-01T00:00:00Z", "visibility": "public"},
        {"publish_at": "2099-01-01T00:00:00", "visibility": "public"},
        {"publish_at": "2099-01-01T00:00:00Z", "visibility": "private"},
    ]:
        assert (
            client.post(
                "/api/v1/publications/youtube", json={**body, **changes}
            ).status_code
            == 422
        )
    row = draft(setup, visibility="public")
    response = client.post(
        f"/api/v1/publications/{row['id']}/approve",
        json={
            "snapshot_hash": row["snapshot_hash"],
            "channel_id": CHANNEL,
            "approve_live_upload": True,
        },
    )
    assert response.status_code == 422 and "public_approval" in response.text


def test_duplicate_worker_and_os_lock(setup):
    _, ledger, google, _ = setup
    row = approved(setup)
    google.entered, google.release = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker, setup)
        try:
            assert google.entered.wait(10)
            assert not worker(setup)
            with publication_lock(ledger, row["id"]) as acquired:
                assert not acquired
        finally:
            google.release.set()
        assert running.result(timeout=10)
    assert google.starts == 1 and len(google.uploaded) == 1


def test_expired_session_no_new_upload_and_manual_video_identity(setup):
    client, ledger, google, _ = setup
    row = approved(setup)
    google.lose_final = True
    assert worker(setup)
    google.expired = True
    due(ledger, row["id"])
    assert worker(setup)
    assert get(ledger, row["id"])[0]["state"] == "NEEDS_RECONCILIATION"
    assert google.starts == 1
    response = client.post(
        f"/api/v1/publications/{row['id']}/reconcile",
        json={"video_id": VIDEO, "confirm_remote_identity": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["post_id"] == VIDEO


def test_config_no_fallback_and_missing_approval_hash(setup, monkeypatch):
    client, ledger, google, _ = setup
    row = approved(setup)
    with ledger.write() as db:
        db.execute(update(targets).where(targets.c.id == row["id"]).values(approval=""))
    assert worker(setup)
    assert get(ledger, row["id"])[0]["state"] == "FAILED" and google.starts == 0
    monkeypatch.setenv("YOUTUBE_ENABLED", "false")
    assert client.get("/api/v1/integrations/youtube/connect").status_code == 503
    monkeypatch.setenv("YOUTUBE_ENABLED", "true")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "")
    with pytest.raises(YouTubeError, match="configuration"):
        OAuthConfig.load()


def test_metadata_edit_invalidates_approval(setup):
    client, ledger, google, body = setup
    row = draft(setup)
    response = client.patch(
        "/api/v1/publications/" + row["id"], json={**body, "title": "Corrected title"}
    )
    assert response.status_code == 200, response.text
    edited = response.json()
    assert (
        edited["snapshot_hash"] != row["snapshot_hash"] and edited["state"] == "DRAFT"
    )
    assert get(ledger, row["id"])[1]["approved_hash"] is None
    assert (
        client.post(
            f"/api/v1/publications/{row['id']}/approve",
            json={
                "snapshot_hash": row["snapshot_hash"],
                "channel_id": CHANNEL,
                "approve_live_upload": True,
            },
        ).status_code
        == 409
    )
    assert not worker(setup) and google.starts == 0


def test_reconnect_requires_new_explicit_approval(setup):
    client, ledger, google, _ = setup
    row = approved(setup)
    with ledger.write() as db:
        db.execute(update(connections).values(revision=uid()))
    assert worker(setup)
    saved = client.get("/api/v1/publications/" + row["id"]).json()
    assert saved["state"] == "AUTH_REQUIRED"
    assert client.post(f"/api/v1/publications/{row['id']}/retry").status_code == 409
    response = client.post(
        f"/api/v1/publications/{row['id']}/approve",
        json={
            "snapshot_hash": saved["snapshot_hash"],
            "channel_id": CHANNEL,
            "approve_live_upload": True,
        },
    )
    assert response.status_code == 200, response.text
    assert worker(setup) and google.starts == 1


def test_rights_qa_and_artifact_changes_block_live_upload(setup):
    client, ledger, google, body = setup
    from factory.db import jobs

    with ledger.write() as db:
        spec = db.execute(
            select(jobs.c.spec).where(jobs.c.id == body["job"])
        ).scalar_one()
        spec["rights"] = "unknown"
        db.execute(update(jobs).where(jobs.c.id == body["job"]).values(spec=spec))
    response = client.post("/api/v1/publications/youtube", json=body)
    assert response.status_code == 422 and "rights" in response.text
    with ledger.write() as db:
        spec["rights"] = "original"
        db.execute(update(jobs).where(jobs.c.id == body["job"]).values(spec=spec))
    row = approved(setup)
    with ledger.write() as db:
        package = db.execute(
            select(stages.c.result).where(
                stages.c.job == body["job"], stages.c.name == "package"
            )
        ).scalar_one()
        package["manifest_hash"] = "changed-after-approval"
        db.execute(
            update(stages)
            .where(stages.c.job == body["job"], stages.c.name == "package")
            .values(result=package)
        )
    assert worker(setup)
    assert get(ledger, row["id"])[0]["state"] == "FAILED" and google.starts == 0


def test_session_creation_timeout_is_not_retried_blindly(setup, monkeypatch):
    _, ledger, google, _ = setup
    row = approved(setup)
    real = YouTubePublisher.request

    def uncertain(self, method, url, **kwargs):
        if method == "POST":
            raise YouTubeError("youtube_network_error", retry=True, ambiguous=True)
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(YouTubePublisher, "request", uncertain)
    assert worker(setup)
    target, upload = get(ledger, row["id"])
    assert target["state"] == "NEEDS_RECONCILIATION" and upload["phase"] == "INITIATING"
    assert not worker(setup)


def test_worker_crash_after_chunk_recovers_saved_session(setup, monkeypatch):
    _, ledger, google, _ = setup
    row = approved(setup)
    real = YouTubePublisher.request
    crashed = [False]

    def interrupted(self, method, url, **kwargs):
        response = real(self, method, url, **kwargs)
        if method == "PUT" and kwargs.get("content") and not crashed[0]:
            crashed[0] = True
            raise KeyboardInterrupt("process terminated after remote chunk acceptance")
        return response

    monkeypatch.setattr(YouTubePublisher, "request", interrupted)
    with pytest.raises(KeyboardInterrupt):
        worker(setup)
    with ledger.write() as db:
        db.execute(
            update(runtime)
            .where(runtime.c.id == row["id"])
            .values(expires="2000-01-01")
        )
    monkeypatch.setattr(YouTubePublisher, "request", real)
    assert worker(setup)
    assert google.starts == 1 and google.chunks.count(0) == 1
    assert get(ledger, row["id"])[0]["post_id"] == VIDEO


def test_wrong_channel_and_schedule_rejection_do_not_claim_success(setup):
    client, ledger, google, _ = setup
    row = approved(setup)
    google.channel = "UC" + "b" * 22
    assert worker(setup)
    assert get(ledger, row["id"])[0]["state"] == "AUTH_REQUIRED" and google.starts == 0
    google.channel = CHANNEL
    # Re-approve the AUTH_REQUIRED record before any remote transfer.
    current = client.get("/api/v1/publications/" + row["id"]).json()
    assert (
        client.post(
            f"/api/v1/publications/{row['id']}/approve",
            json={
                "snapshot_hash": current["snapshot_hash"],
                "channel_id": CHANNEL,
                "approve_live_upload": True,
            },
        ).status_code
        == 200
    )
    assert worker(setup)
    # A manual remote schedule mismatch is reported honestly.
    from factory.youtube_publications import remote_state

    state, error = remote_state(
        {
            "status": {"privacyStatus": "private", "uploadStatus": "processed"},
            "processing": {},
        },
        {"publish_at": (now() + timedelta(hours=1)).isoformat()},
    )
    assert (
        state == "NEEDS_RECONCILIATION"
        and error == "youtube_remote_schedule_not_confirmed"
    )


def test_live_ids_cannot_traverse_paths(setup):
    client, _, _, _ = setup
    response = client.post("/api/v1/publications/C:%5Coutside/reconcile", json={})
    assert response.status_code == 404


def test_rejected_oauth_scope_and_401_refresh(setup):
    _, ledger, google, _ = setup
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"access_token": "x", "expires_in": 3600, "scope": SCOPES[0]}
            )
        )
    ) as http:
        with pytest.raises(YouTubeError, match="scope_missing"):
            OAuthConfig.load().exchange(http, {"code": "test"})
    calls = [0]

    def refresh_once(request):
        if request.url.path == "/youtube/v3/channels" and calls[0] == 0:
            calls[0] += 1
            return httpx.Response(401)
        return google(request)

    with httpx.Client(transport=httpx.MockTransport(refresh_once)) as http:
        assert YouTubePublisher(http, AccessTokens(ledger, http)).channel() == CHANNEL
    assert google.refreshes == 1


def test_missing_or_tampered_artifact_returns_structured_error(setup):
    client, ledger, google, body = setup
    row = draft(setup)
    storage.path_for(row["snapshot"]["sha256"]).write_bytes(b"tampered")
    response = client.post(
        f"/api/v1/publications/{row['id']}/approve",
        json={
            "snapshot_hash": row["snapshot_hash"],
            "channel_id": CHANNEL,
            "approve_live_upload": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "youtube_local_artifact_unavailable"
    assert google.starts == 0


def test_duplicate_drafts_and_cancel_are_explicit(setup):
    client, ledger, google, body = setup
    row = draft(setup)
    assert client.post("/api/v1/publications/youtube", json=body).status_code == 409
    response = client.post(f"/api/v1/publications/{row['id']}/cancel")
    assert response.status_code == 200 and response.json()["state"] == "CANCELLED"
    assert not worker(setup) and google.starts == 0
    assert (
        client.patch(
            "/api/v1/publications/" + row["id"],
            json={**body, "title": "Reviewed again"},
        ).json()["state"]
        == "DRAFT"
    )


def test_untrusted_upload_location_is_never_followed(setup, monkeypatch):
    _, ledger, google, _ = setup
    row = approved(setup)
    real = YouTubePublisher.request

    def poisoned(self, method, url, **kwargs):
        if method == "POST":
            return httpx.Response(
                200,
                headers={
                    "Location": "https://www.googleapis.com.evil.invalid/upload/youtube/v3/videos"
                },
            )
        assert "evil.invalid" not in url
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(YouTubePublisher, "request", poisoned)
    assert worker(setup)
    target, upload = get(ledger, row["id"])
    assert target["state"] == "NEEDS_RECONCILIATION"
    assert upload["error"] == "youtube_invalid_upload_session"


def test_bad_chunk_configuration_and_private_processing_status(setup, monkeypatch):
    _, _, _, _ = setup
    monkeypatch.setenv("YOUTUBE_UPLOAD_CHUNK_BYTES", "12345")
    with pytest.raises(YouTubeError, match="configuration_invalid"):
        OAuthConfig.load()
    from factory.youtube_publications import remote_state

    state, _ = remote_state(
        {
            "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
            "processing": {},
        },
        {},
    )
    assert state == "UPLOADED_PROCESSING"


def test_local_removal_does_not_claim_remote_schedule_cancelled(setup):
    client, ledger, google, _ = setup
    from factory.drive_worker import drive_metadata

    drive_metadata.create_all(ledger.engine)
    row = approved(
        setup, visibility="public", publish_at=(now() + timedelta(hours=2)).isoformat()
    )
    assert worker(setup)
    response = client.post(f"/api/v1/publications/{row['id']}/reconcile", json={})
    assert response.json()["state"] == "SCHEDULED"
    response = client.delete(f"/api/v1/packages/{row['snapshot']['job']}/1")
    assert response.status_code == 200, response.text
    assert get(ledger, row["id"])[0]["state"] == "SCHEDULED"
    storage.path_for(row["snapshot"]["sha256"]).unlink()
    due(ledger, row["id"])
    assert worker(setup)
    assert get(ledger, row["id"])[0]["state"] == "SCHEDULED"


def test_postgres_live_publication_lock_and_recovery(setup):
    import os
    from sqlalchemy import create_engine, text
    from factory.db import batches, digest, jobs
    from factory.contracts import YouTubeApproval, YouTubePublication
    from factory.youtube_publications import create, approve

    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL is required for PostgreSQL publisher integration"
        )
    _, original, google, body = setup
    schema = "youtube_test_" + uid().replace("-", "")
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    ledger = Ledger(url)
    ledger.engine.update_execution_options(schema_translate_map={None: schema})
    try:
        metadata.create_all(ledger.engine)
        youtube_metadata.create_all(ledger.engine)
        with ledger.write() as db:
            for table in [batches, jobs, stages, connections]:
                for row in original.list_rows(table):
                    db.execute(insert(table).values(**row))
        key = create(ledger, YouTubePublication.model_validate(body))
        target, _ = get(ledger, key)
        approve(
            ledger,
            key,
            YouTubeApproval(
                snapshot_hash=digest(target["snapshot"]),
                channel_id=CHANNEL,
                approve_live_upload=True,
            ),
        )
        google.lose_final = True

        def tick():
            with httpx.Client(transport=httpx.MockTransport(google)) as http:
                return dispatch(
                    ledger, YouTubePublisher(http, AccessTokens(ledger, http))
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: tick(), range(2)))
        assert sum(results) == 1
        assert get(ledger, key)[0]["state"] == "RETRY_PENDING"
        assert get(ledger, key)[1]["session"]
        due(ledger, key)
        assert tick()
        assert get(ledger, key)[0]["post_id"] == VIDEO
        assert google.starts == 1 and len(google.uploaded) == 1
    finally:
        ledger.engine.dispose()
        with admin.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def test_known_remote_id_reconnect_only_verifies_no_new_upload(setup):
    _, ledger, google, _ = setup
    row = approved(setup)
    assert worker(setup)
    with ledger.write() as db:
        db.execute(update(connections).values(revision=uid()))
    due(ledger, row["id"])
    assert worker(setup)
    assert get(ledger, row["id"])[0]["state"] == "UPLOADED_PRIVATE"
    assert google.starts == 1


def test_unlisted_upload_is_not_reported_as_public(setup):
    client, ledger, google, _ = setup
    row = approved(setup, visibility="unlisted")
    assert worker(setup)
    assert google.sessions["1"]["metadata"]["status"]["privacyStatus"] == "unlisted"
    response = client.post(f"/api/v1/publications/{row['id']}/reconcile", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "UPLOADED_UNLISTED"
    assert response.json()["upload"]["verified"]
    assert get(ledger, row["id"])[0]["state"] != "PUBLISHED"


def test_resumed_session_keeps_original_chunk_size(setup, monkeypatch):
    _, ledger, google, _ = setup
    row = approved(setup)
    google.fail_chunk = True
    assert worker(setup)
    state = get(ledger, row["id"])[1]
    assert decrypt(state["session"])["chunk_size"] == 256 * 1024
    monkeypatch.setenv("YOUTUBE_UPLOAD_CHUNK_BYTES", str(512 * 1024))
    sizes = []
    real = YouTubePublisher.request

    def capture(self, method, url, **kwargs):
        if method == "PUT" and kwargs.get("content"):
            sizes.append(len(kwargs["content"]))
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(YouTubePublisher, "request", capture)
    due(ledger, row["id"])
    assert worker(setup)
    assert sizes[0] == 256 * 1024 and google.starts == 1
