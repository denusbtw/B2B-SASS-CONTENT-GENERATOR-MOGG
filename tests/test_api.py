import pytest
from fastapi.testclient import TestClient

from factory import api
from factory.db import Ledger, metadata
from main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    ledger = Ledger(f"sqlite:///{tmp_path / 'api.db'}")
    metadata.create_all(ledger.engine)
    monkeypatch.setattr(api, "ledger", ledger)
    monkeypatch.setenv("OPERATOR_TOKEN", "test-operator")
    monkeypatch.setenv("WORKER_AUTH_TOKEN", "test-worker")
    with TestClient(app, headers={"Authorization": "test-operator"}) as http:
        yield http
    ledger.engine.dispose()


def test_individual_rejections_and_job_graph(client):
    response = client.post(
        "/api/v1/batches",
        json={
            "jobs": [
                {"mode": "STORY", "prompt": "Привіт"},
                {"mode": "UNSUPPORTED", "prompt": "invalid"},
                {"mode": "CLIPS", "prompt": "Synthetic", "synthetic_clip": True},
            ]
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert [row["index"] for row in body["accepted"]] == [0, 2]
    assert [row["index"] for row in body["rejected"]] == [1]
    clip = client.get("/api/v1/jobs/" + body["accepted"][1]["id"]).json()
    assert [row["name"] for row in clip["stages"]] == [
        "clip_plan",
        "render",
        "qa",
        "package",
    ]
    assert "token" not in clip["stages"][0]


def test_worker_role_and_approval_gate(client):
    assert client.post("/api/v1/workers/claim", json={"worker": "a"}).status_code == 401
    worker = {"Authorization": "test-worker"}
    assert client.get("/api/v1/platform-accounts", headers=worker).status_code == 401
    response = client.post(
        "/api/v1/batches", json={"jobs": [{"mode": "STORY", "prompt": "Draft"}]}
    )
    job = response.json()["accepted"][0]["id"]
    assert (
        client.post(
            "/api/v1/publication-batches",
            json={
                "targets": [
                    {
                        "job": job,
                        "account": "mock-youtube-a",
                        "caption": "Too soon",
                        "scheduled_at": "2026-11-12T00:00:00Z",
                    }
                ]
            },
        ).status_code
        == 409
    )


def test_dashboard_and_path_guard(client):
    response = client.get("/operator")
    assert response.status_code == 200
    assert 'id="create"' in response.text
    assert 'data-view="library"' in response.text
    assert client.get("/api/v1/artifacts/bad-hash/download").status_code == 404


def test_remove_library_video_cancels_only_its_work(client, monkeypatch, tmp_path):
    from factory import storage
    from factory.db import now, targets
    from factory.drive_worker import archive_next, discover, drive_metadata, uploads

    ledger = api.ledger
    drive_metadata.create_all(ledger.engine)
    monkeypatch.setattr(storage, "ROOT", tmp_path / "archive")
    first = storage.put(b"first video")
    second = storage.put(b"second video")
    _, [job] = ledger.submit([{"mode": "STORY"}])
    for result in (
        {},
        {},
        {},
        {
            "parts": [{"sha256": first}, {"sha256": second}],
            "publishable": True,
            "manifest_hash": "example",
        },
    ):
        claim = ledger.claim("test")
        ledger.finish(claim["id"], "test", claim["token"], result=result)
    discover(ledger)
    schedule = {
        "job": job,
        "part": 1,
        "account": "mock-youtube-a",
        "caption": "Example",
        "scheduled_at": now().isoformat(),
    }
    assert (
        client.post(
            "/api/v1/publication-batches", json={"targets": [schedule]}
        ).status_code
        == 201
    )

    path = f"/api/v1/packages/{job}/1"
    assert (
        client.delete(path, headers={"Authorization": "test-worker"}).status_code == 401
    )
    assert client.delete(path).json() == {"deleted": True}
    assert client.delete(path).json() == {"deleted": True}
    [package] = client.get("/api/v1/packages").json()
    assert package["parts"][0]["deleted"]
    assert not package["parts"][1].get("deleted")
    assert (
        client.post(
            "/api/v1/publication-batches", json={"targets": [schedule]}
        ).status_code
        == 409
    )
    assert ledger.list_rows(targets)[0]["state"] == "CANCELLED"
    assert [item["sha256"] for item in ledger.list_rows(uploads)] == [second]
    assert client.delete(f"/api/v1/packages/{job}/2").status_code == 200
    assert client.get("/api/v1/packages").json() == []
    assert not archive_next(ledger, None, "{part}.mp4")
