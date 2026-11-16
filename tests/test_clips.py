"""Offline real-CLIPS integration and recovery tests."""

import subprocess
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from factory import api, storage
from factory.clips_db import clips_metadata
from factory.clips_media import boundaries, probe, render_part
from factory.contracts import ClipOutput, JobSpec
from factory.db import Ledger, metadata, stages
from factory.drive_worker import drive_metadata
from factory.generator import tick
from factory.ingestion import youtube_id
from factory.media import ffmpeg
from main import app


@pytest.fixture
def setup(monkeypatch, tmp_path):
    ledger = Ledger(f"sqlite:///{tmp_path / 'clips.db'}")
    metadata.create_all(ledger.engine)
    drive_metadata.create_all(ledger.engine)
    clips_metadata.create_all(ledger.engine)
    monkeypatch.setattr(api, "ledger", ledger)
    monkeypatch.setattr(storage, "ROOT", tmp_path / "storage")
    monkeypatch.setenv("SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setenv("OPERATOR_TOKEN", "operator")
    monkeypatch.setenv("WORKER_AUTH_TOKEN", "worker")
    monkeypatch.setenv("DRIVE_ARCHIVE_ENABLED", "false")
    with TestClient(app, headers={"Authorization": "operator"}) as client:
        yield client, ledger, tmp_path
    ledger.engine.dispose()


def fixture_video(path, duration=60, audio=True):
    args = [
        ffmpeg(),
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate=30:duration={duration}",
    ]
    if audio:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration}",
        ]
    subprocess.run(
        args
        + [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path


def submit(client, path, **extra):
    with path.open("rb") as handle:
        response = client.post(
            "/api/v1/clips/sources/upload",
            content=handle,
            headers={"X-Filename": path.name},
        )
    assert response.status_code == 201, response.text
    source = response.json()["artifact_id"]
    spec = {
        "mode": "CLIPS",
        "source": {"type": "local_file", "artifact_id": source},
        "parts": 5,
        "output": {"resolution": "360x640", "preset": "ultrafast"},
        **extra,
    }
    response = client.post("/api/v1/clips/jobs", json=spec)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def drain(client):
    with TestClient(
        app, base_url="http://testserver/api/v1", headers={"Authorization": "worker"}
    ) as worker:
        for _ in range(12):
            if not tick(worker, "test-worker"):
                return
    pytest.fail("Worker did not drain")


def test_sixty_seconds_five_parts(setup):
    client, ledger, tmp = setup
    job = submit(client, fixture_video(tmp / "fixture.mp4"))
    drain(client)
    status = client.get(f"/api/v1/clips/jobs/{job}").json()
    assert status["state"] == "COMPLETE", status
    assert status["archive"]["state"] == "LOCAL_COMPLETE_DRIVE_NOT_CONFIGURED"
    manifest = client.get(f"/api/v1/clips/jobs/{job}/manifest").json()
    assert len(manifest["parts"]) == 5
    assert [p["start"] for p in manifest["parts"]] == [0, 12, 24, 36, 48]
    for index, p in enumerate(manifest["parts"], 1):
        assert abs(p["duration"] - 12) < 0.1
        assert (p["width"], p["height"], p["video_codec"], p["audio_codec"]) == (
            360,
            640,
            "h264",
            "aac",
        )
        assert p["job_id"] == job and p["size"] > 0
        assert abs(p["audio_duration"] - p["duration"]) < 0.12
        assert (
            client.get(f"/api/v1/clips/jobs/{job}/parts/{index}/download").status_code
            == 200
        )
    assert client.get(f"/api/v1/clips/jobs/{job}/parts/6/download").status_code == 404


@pytest.mark.parametrize("framing", ["center_crop", "blurred_background"])
def test_landscape_and_silent(setup, framing):
    _, _, tmp = setup
    source = fixture_video(tmp / "silent.mp4", 2, False)
    profile = ClipOutput(resolution="360x640", preset="ultrafast", framing=framing)
    result = render_part(
        source, tmp / "part.mp4", boundaries(2, 1, 30)[0], profile, probe(source)
    )
    assert (result["width"], result["height"], result["audio_codec"]) == (
        360,
        640,
        None,
    )


@pytest.mark.parametrize("count", [0, 51, 1.5, True])
def test_invalid_counts(count):
    with pytest.raises(ValueError):
        JobSpec(
            mode="CLIPS",
            source={"type": "local_file", "artifact_id": "a" * 64},
            parts=count,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/watch?v=abcdefghijk",
        "https://www.youtube.com/watch?v=no",
        "file:///video",
        "https://youtube.com@evil.test/watch?v=abcdefghijk",
    ],
)
def test_invalid_youtube(url):
    with pytest.raises(ValueError):
        youtube_id(url)


def test_authorization_and_short_segments():
    with pytest.raises(ValueError):
        JobSpec(
            mode="CLIPS",
            source={"type": "youtube", "url": "https://youtu.be/abcdefghijk"},
        )
    with pytest.raises(ValueError):
        boundaries(2, 5, 30)
    parts = boundaries(60.017, 7, 30)
    assert all(a["end"] == b["start"] for a, b in zip(parts, parts[1:]))


def test_bad_uploads_and_limits(setup, monkeypatch):
    client, _, tmp = setup
    assert (
        client.post(
            "/api/v1/clips/sources/upload",
            content=b"bad",
            headers={"X-Filename": "bad.exe"},
        ).status_code
        == 415
    )
    assert (
        client.post("/api/v1/clips/sources/upload", content=b"bad").status_code == 422
    )
    monkeypatch.setenv("CLIPS_MAX_SOURCE_BYTES", "2")
    assert (
        client.post("/api/v1/clips/sources/upload", content=b"bad").status_code == 413
    )
    assert client.get("/api/v1/clips/sources/../../secret").status_code == 404


def test_missing_ffprobe(setup, monkeypatch):
    _, _, tmp = setup
    monkeypatch.setenv("FFPROBE_BIN", str(tmp / "absent.exe"))
    with pytest.raises(ValueError, match="FFprobe"):
        probe(tmp / "input")


def test_render_checkpoint_survives_interruption(setup, monkeypatch):
    from factory import clips
    from factory.generator import execute

    client, ledger, tmp = setup
    job = submit(client, fixture_video(tmp / "fixture.mp4", 5))
    with TestClient(
        app, base_url="http://testserver/api/v1", headers={"Authorization": "worker"}
    ) as worker:
        for _ in range(4):
            assert tick(worker, "test-worker")
        claim = ledger.claim("test-worker")
        real = clips.render_part
        calls = []

        def interrupted(*args):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("interrupted")
            return real(*args)

        monkeypatch.setattr(clips, "render_part", interrupted)
        with pytest.raises(OSError):
            execute(worker, claim)
        with ledger.write() as db:
            db.execute(
                update(stages)
                .where(stages.c.id == claim["id"])
                .values(expires="2000-01-01")
            )
        resumed = ledger.claim("resumed")
        assert len(resumed["checkpoint"]["parts"]) == 1
        # Old lease cannot checkpoint after takeover.
        response = worker.post(
            f"/workers/stages/{claim['id']}/checkpoint",
            json={
                "lease": {"worker": "test-worker", "token": claim["token"]},
                "result": {"parts": []},
            },
        )
        assert response.status_code == 409
        monkeypatch.setattr(clips, "render_part", real)
        result = execute(worker, resumed)
        assert len(result["parts"]) == 5
        response = worker.post(
            f"/workers/stages/{resumed['id']}/complete",
            json={
                "lease": {"worker": "resumed", "token": resumed["token"]},
                "result": result,
            },
        )
        assert response.status_code == 200, response.text
    drain(client)
    assert client.get(f"/api/v1/clips/jobs/{job}").json()["state"] == "COMPLETE"


def test_framing_preserves_edges(setup):
    _, _, tmp = setup
    source = tmp / "edges.mp4"
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:size=320x180:rate=30:duration=1,drawbox=x=0:y=0:w=80:h=180:color=red:t=fill,drawbox=x=240:y=0:w=80:h=180:color=blue:t=fill",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    for framing in ["center_crop", "blurred_background"]:
        output = tmp / (framing + ".mp4")
        profile = ClipOutput(resolution="360x640", preset="ultrafast", framing=framing)
        render_part(source, output, boundaries(1, 1, 30)[0], profile, probe(source))
        raw = subprocess.run(
            [
                ffmpeg(),
                "-v",
                "error",
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
        left = raw[(320 * 360 + 10) * 3 : (320 * 360 + 10) * 3 + 3]
        right = raw[(320 * 360 + 350) * 3 : (320 * 360 + 350) * 3 + 3]
        if framing == "blurred_background":
            assert left[0] > 180 and left[2] < 50
            assert right[2] > 180 and right[0] < 50
        else:
            assert left[1] > 80 and right[1] > 80
            assert left[0] < 50 and right[2] < 50


def test_missing_ffmpeg_and_disk_space(setup, monkeypatch):
    from factory import clips
    from factory.generator import execute

    client, ledger, tmp = setup
    path = fixture_video(tmp / "source.mp4", 5)
    monkeypatch.setenv("FFMPEG_BIN", str(tmp / "absent.exe"))
    with pytest.raises((ValueError, OSError)):
        render_part(
            path,
            tmp / "out.mp4",
            boundaries(5, 1, 30)[0],
            ClipOutput(resolution="360x640"),
            probe(path),
        )
    monkeypatch.delenv("FFMPEG_BIN")
    submit(client, path)
    with TestClient(
        app, base_url="http://testserver/api/v1", headers={"Authorization": "worker"}
    ) as worker:
        for _ in range(4):
            tick(worker, "test-worker")
        claim = ledger.claim("test-worker")
        usage = shutil.disk_usage(tmp)
        monkeypatch.setattr(
            clips.shutil, "disk_usage", lambda path: usage._replace(free=1)
        )
        with pytest.raises(OSError, match="disk space"):
            execute(worker, claim)


def test_unavailable_youtube(setup, monkeypatch):
    from factory import ingestion

    _, _, tmp = setup
    monkeypatch.setattr(
        ingestion.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="unavailable"
        ),
    )
    with pytest.raises(ValueError, match="local authorized file"):
        ingestion.download("https://youtu.be/abcdefghijk", tmp)


def test_vfr_fractional_source(setup):
    _, _, tmp = setup
    source = tmp / "vfr.mp4"
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=5.17",
            "-vf",
            "select=if(lt(t\\,2)\\,not(mod(n\\,2))\\,1)",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    meta = probe(source)
    parts = boundaries(meta["duration"], 3, 30)
    assert parts[-1]["end"] == round(meta["duration"] * 30) / 30
    for part in parts:
        result = render_part(
            source,
            tmp / part["name"],
            part,
            ClipOutput(resolution="360x640", preset="ultrafast"),
            meta,
        )
        assert abs(result["duration"] - (part["end"] - part["start"])) < 0.12
        assert result["fps"] == "30"


@pytest.mark.parametrize("extension", ["mov", "mkv", "webm"])
def test_source_containers(setup, extension):
    client, _, tmp = setup
    source = fixture_video(tmp / "input.mp4", 2, False)
    target = tmp / ("input." + extension)
    codec = (
        ["-c", "copy"]
        if extension != "webm"
        else ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8"]
    )
    subprocess.run(
        [ffmpeg(), "-v", "error", "-y", "-i", str(source), *codec, str(target)],
        check=True,
        timeout=30,
    )
    with target.open("rb") as handle:
        response = client.post(
            "/api/v1/clips/sources/upload",
            content=handle,
            headers={"X-Filename": target.name},
        )
    assert response.status_code == 201, response.text
    assert response.json()["metadata"]["width"] == 320


def test_failure_retry_keeps_checkpoint_and_render_limit(setup):
    _, ledger, _ = setup
    specs = [
        JobSpec(
            mode="CLIPS", source={"type": "local_file", "artifact_id": "a" * 64}
        ).model_dump(mode="json")
    ] * 2
    _, ids = ledger.submit(specs)
    with ledger.write() as db:
        db.execute(
            update(stages)
            .where(stages.c.name == "validate_source")
            .values(state="PENDING")
        )
        db.execute(
            update(stages).where(stages.c.name == "render_parts").values(state="READY")
        )
    first = ledger.claim("worker-a")
    assert first["name"] == "render_parts"
    assert ledger.claim("worker-b") is None
    ledger.finish(
        first["id"], "worker-a", first["token"], error="transient", detail="interrupted"
    )
    retry = ledger.claim("worker-b")
    assert retry["name"] == "render_parts"


def test_operator_retry_preserves_render_checkpoint(setup):
    client, ledger, _ = setup
    spec = JobSpec(
        mode="CLIPS", source={"type": "local_file", "artifact_id": "a" * 64}
    ).model_dump(mode="json")
    _, [job] = ledger.submit([spec])
    with ledger.write() as db:
        db.execute(
            update(stages)
            .where(stages.c.job == job, stages.c.name == "validate_source")
            .values(state="SUCCEEDED")
        )
        db.execute(
            update(stages)
            .where(stages.c.job == job, stages.c.name == "render_parts")
            .values(state="READY")
        )
    claim = ledger.claim("worker-a")
    key = storage.put(b"completed-artifact-checkpoint")
    with ledger.write() as db:
        db.execute(
            update(stages)
            .where(stages.c.id == claim["id"])
            .values(result={"parts": [{"sha256": key}]})
        )
    ledger.finish(
        claim["id"],
        "worker-a",
        claim["token"],
        error="render_error",
        detail="disk full",
    )
    response = client.post(f"/api/v1/clips/jobs/{job}/retry")
    assert response.status_code == 200, response.text
    resumed = ledger.claim("worker-b")
    assert resumed["id"] == claim["id"]
    assert resumed["checkpoint"]["parts"] == [{"sha256": key}]


def test_worker_only_cache_and_source_reuse(setup, monkeypatch):
    from factory.clips import execute
    from factory.clips_db import sources
    from sqlalchemy import insert

    client, ledger, tmp = setup
    key = storage.put(b"cached-source")
    meta = {
        "youtube_id": "abcdefghijk",
        "title": "Authorized cached source",
        "width": 1920,
        "height": 1080,
    }
    oversized = storage.put(b"old-4k-download")
    with ledger.write() as db:
        db.execute(
            insert(sources).values(
                sha256=oversized, metadata={**meta, "width": 3840, "height": 2160}
            )
        )
        db.execute(insert(sources).values(sha256=key, metadata=meta))
    assert client.get("/api/v1/clips/youtube/abcdefghijk").status_code == 401
    spec = JobSpec(
        mode="CLIPS",
        source={
            "type": "youtube",
            "url": "https://youtu.be/abcdefghijk",
            "authorized": True,
        },
    )
    with TestClient(
        app, base_url="http://testserver/api/v1", headers={"Authorization": "worker"}
    ) as worker:
        result = execute(
            worker,
            {
                "name": "ingest_source",
                "outputs": {},
                "spec": spec.model_dump(mode="json"),
            },
        )
        assert result["sha256"] == key
