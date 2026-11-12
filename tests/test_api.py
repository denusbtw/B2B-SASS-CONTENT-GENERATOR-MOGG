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
    assert "Mock operator" in client.get("/operator").text
    assert client.get("/api/v1/artifacts/bad-hash/download").status_code == 404
