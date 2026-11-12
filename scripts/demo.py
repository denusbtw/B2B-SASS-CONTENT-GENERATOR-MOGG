"""Exercise the local mock workflow against a migrated, running coordinator."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep

import httpx

from factory.contracts import Mode

MODES = list(Mode)
TEXT = {
    Mode.STORY: "На березі моря я знайшов лист у пляшці. Виявилося, його написав мій дід.",
    Mode.CLIPS: "Original synthetic test signal with a visible split-screen crop.",
    Mode.CARTOON_2D: "Ты слышишь этот звук? Кажется, это наш маленький робот вернулся домой.",
}


def main():
    url = os.getenv("WORKER_API_URL", "http://127.0.0.1:8080")
    proof = Path("data/demo")
    proof.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    with httpx.Client(
        base_url=url.rstrip("/") + "/api/v1",
        headers={"Authorization": os.environ["OPERATOR_TOKEN"]},
        timeout=30,
        trust_env=False,
    ) as client:

        def api(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response

        jobs = [
            {
                "mode": mode,
                "prompt": f"{TEXT[mode]} ({i + 1})",
                "locale": ("uk", "en", "ru")[i % 3],
                "rights": "original",
                "evidence": "Self-created demo assets",
                "synthetic_clip": mode == Mode.CLIPS,
                "stacked": mode == Mode.CLIPS,
                "parts": 3 if i == 1 else 1,
            }
            for i in range(20)
            for mode in [MODES[i % 3]]
        ]
        batch = api("POST", "/batches", json={"jobs": jobs}).json()
        assert len(batch["accepted"]) == 20 and not batch["rejected"]
        processes = [
            subprocess.Popen([sys.executable, "-m", "factory.generator", "--id", name])
            for name in ("demo-worker-a", "demo-worker-b")
        ]
        try:
            deadline = monotonic() + 240
            while monotonic() < deadline:
                rows = api("GET", f"/batches/{batch['id']}").json()["jobs"]
                if all(row["state"] not in {"CREATED", "GENERATING"} for row in rows):
                    break
                assert all(worker.poll() is None for worker in processes)
                sleep(0.5)
            assert all(row["state"] == "COMPLETE" for row in rows), [
                api("GET", f"/jobs/{row['id']}").json()
                for row in rows
                if row["state"] != "COMPLETE"
            ]
        finally:
            for worker in processes:
                worker.terminate()
                worker.wait(timeout=10)

        completed_by_worker: dict[str, int] = {}
        for row in rows:
            job = api("GET", f"/jobs/{row['id']}").json()
            for stage in job["stages"]:
                worker = stage["worker"]
                completed_by_worker[worker] = completed_by_worker.get(worker, 0) + 1
        assert set(completed_by_worker) == {"demo-worker-a", "demo-worker-b"}
        assert sum(completed_by_worker.values()) == 80
        examples = {}
        for row in rows:
            mode = row["spec"]["mode"]
            if mode in examples:
                continue
            job = api("GET", f"/jobs/{row['id']}").json()
            manifest = job["stages"][-1]["result"]
            artifact = manifest["parts"][0]
            (proof / f"{mode}.mp4").write_bytes(
                api("GET", f"/artifacts/{artifact['sha256']}/download").content
            )
            (proof / f"{mode}.srt").write_bytes(
                api("GET", f"/artifacts/{artifact['captions']}/download").content
            )
            examples[mode] = {"job": row["id"], "manifest": manifest}

        accounts = ["mock-youtube-a", "mock-tiktok-a", "mock-instagram-a"]
        schedule = datetime.now(timezone.utc).isoformat()
        targets = [
            {
                "job": examples["STORY"]["job"],
                "account": account,
                "caption": "Local simulation",
                "scheduled_at": schedule,
            }
            for account in accounts
        ]
        approval = api("POST", "/publication-batches", json={"targets": targets}).json()
        for _ in range(3):
            subprocess.run(
                [sys.executable, "-m", "factory.publisher", "--once"], check=True
            )
        sleep(1.1)
        for _ in range(4):
            subprocess.run(
                [sys.executable, "-m", "factory.publisher", "--once"], check=True
            )
        published = [
            target
            for target in api("GET", "/publication-targets").json()
            if target["approval"] == approval["approval"]
        ]
        assert (
            len(
                {
                    target["post_id"]
                    for target in published
                    if target["state"] == "PUBLISHED"
                }
            )
            == 3
        )
        report = {
            "batch": batch,
            "completed": len(rows),
            "completed_stages_by_worker": completed_by_worker,
            "media": examples,
            "targets": published,
            "workers": api("GET", "/workers").json(),
            "duration_seconds": monotonic() - started,
        }
        (proof / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"{len(rows)} complete, {len(examples)} modes, {len(published)} mock targets. {proof}"
        )


if __name__ == "__main__":
    main()
