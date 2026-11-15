"""Offline end-to-end demonstration through the real API and generation worker.

python -m scripts.clips_demo --resolution 1080x1920
"""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resolution", choices=["360x640", "1080x1920"], default="360x640"
    )
    args = parser.parse_args()
    root = (Path("data/clips-demo") / str(uuid4())).resolve()
    root.mkdir(parents=True)
    os.environ.update(
        DATABASE_URL="sqlite:///" + str(root / "demo.db"),
        MOCK_STORAGE_ROOT=str(root / "artifacts"),
        SCRATCH_ROOT=str(root / "scratch"),
        OPERATOR_TOKEN="demo-operator",
        WORKER_AUTH_TOKEN="demo-worker",
        DRIVE_ARCHIVE_ENABLED="false",
    )
    from fastapi.testclient import TestClient
    from factory import api, storage
    from factory.clips_db import clips_metadata
    from factory.db import metadata
    from factory.generator import tick
    from factory.media import ffmpeg
    from main import app

    metadata.create_all(api.ledger.engine)
    clips_metadata.create_all(api.ledger.engine)
    source = root / "fixture.mp4"
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=60",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=60",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=120,
    )
    with TestClient(app, headers={"Authorization": "demo-operator"}) as client:
        with source.open("rb") as handle:
            uploaded = client.post(
                "/api/v1/clips/sources/upload",
                content=handle,
                headers={"X-Filename": "fixture.mp4"},
            )
        uploaded.raise_for_status()
        response = client.post(
            "/api/v1/clips/jobs",
            json={
                "mode": "CLIPS",
                "source": {
                    "type": "local_file",
                    "artifact_id": uploaded.json()["artifact_id"],
                },
                "parts": 5,
                "output": {"resolution": args.resolution},
            },
        )
        response.raise_for_status()
        job = response.json()["id"]
        with TestClient(
            app,
            base_url="http://testserver/api/v1",
            headers={"Authorization": "demo-worker"},
        ) as worker:
            for _ in range(12):
                if not tick(worker, "demo-worker"):
                    break
        status = client.get(f"/api/v1/clips/jobs/{job}").json()
        if status["state"] != "COMPLETE":
            raise RuntimeError(json.dumps(status))
        manifest = client.get(f"/api/v1/clips/jobs/{job}/manifest").json()
        destination = root / "jobs" / job
        (destination / "clips").mkdir(parents=True)
        (destination / "source").mkdir()
        shutil.copyfile(source, destination / "source" / "original.mp4")
        (destination / "source" / "metadata.json").write_text(
            json.dumps(manifest["source_metadata"], indent=2), encoding="utf-8"
        )
        for part in manifest["parts"]:
            shutil.copyfile(
                storage.verified(part["sha256"]), destination / "clips" / part["name"]
            )
        (destination / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "job": job,
                    "output": str(destination),
                    "archive": status["archive"]["state"],
                    "parts": [
                        {
                            k: p[k]
                            for k in [
                                "name",
                                "duration",
                                "width",
                                "height",
                                "size",
                                "sha256",
                                "video_codec",
                                "audio_codec",
                            ]
                        }
                        for p in manifest["parts"]
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
