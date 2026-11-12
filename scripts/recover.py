"""Kill a Generator at the render boundary and wait for actual lease expiry."""

import json
import os
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import httpx

from factory.generator import tick

CHILD = """
import os
from pathlib import Path
from time import sleep
import httpx
from factory import generator
def stalled_render(*args):
    Path(os.environ["CRASH_MARKER"]).touch()
    sleep(300)
generator.render = stalled_render
with httpx.Client(base_url=os.environ["CRASH_API"], trust_env=False,
                  headers={"Authorization": os.environ["WORKER_AUTH_TOKEN"]}) as client:
    generator.tick(client, "crash-worker")
"""


def main():
    base = os.getenv("WORKER_API_URL", "http://127.0.0.1:8080").rstrip("/") + "/api/v1"
    proof = Path("data/demo")
    proof.mkdir(parents=True, exist_ok=True)
    marker = (proof / "render-crash.marker").resolve()
    marker.unlink(missing_ok=True)
    with (
        httpx.Client(
            base_url=base,
            trust_env=False,
            headers={"Authorization": os.environ["OPERATOR_TOKEN"]},
        ) as operator,
        httpx.Client(
            base_url=base,
            trust_env=False,
            headers={"Authorization": os.environ["WORKER_AUTH_TOKEN"]},
        ) as worker,
    ):
        auth = operator.post("/workers/claim", json={"worker": "untrusted"})
        assert auth.status_code == 401
        response = operator.post(
            "/batches",
            json={
                "jobs": [
                    {
                        "mode": "STORY",
                        "prompt": "This render must survive a worker exit.",
                        "rights": "unknown",
                    }
                ]
            },
        )
        response.raise_for_status()
        job = response.json()["accepted"][0]["id"]

        def status():
            response = operator.get(f"/jobs/{job}")
            response.raise_for_status()
            return response.json()

        assert tick(worker, "recovery-planner")
        original_plan = status()["stages"][0]
        assert original_plan["state"] == "SUCCEEDED"
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD],
            env={**os.environ, "CRASH_API": base, "CRASH_MARKER": str(marker)},
        )
        try:
            deadline = monotonic() + 15
            while (
                not marker.exists() and monotonic() < deadline and child.poll() is None
            ):
                sleep(0.1)
            assert marker.exists(), "Generator did not reach render"
            before = status()["stages"][1]
            assert before["state"] == "LEASED" and before["worker"] == "crash-worker"
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        assert not tick(worker, "recovery-worker")
        deadline = monotonic() + 45
        while not tick(worker, "recovery-worker"):
            assert monotonic() < deadline, "Lease did not expire"
            sleep(0.5)
        assert tick(worker, "recovery-worker")
        assert tick(worker, "recovery-worker")
        recovered = status()
        assert recovered["state"] == "BLOCKED"  # Rights are intentionally unknown.
        assert recovered["stages"][0] == original_plan
        assert recovered["stages"][1]["attempt"] == 2
        response = operator.post(
            "/publication-batches",
            json={
                "targets": [
                    {
                        "job": job,
                        "account": "mock-youtube-a",
                        "caption": "Must block",
                        "scheduled_at": "2026-11-12T00:00:00Z",
                    }
                ]
            },
        )
        assert response.status_code == 409
        evidence = {
            "terminated_pid": child.pid,
            "plan_reused": True,
            "attempts": 2,
            "rights_gate": response.status_code,
            "job": recovered,
        }
        (proof / "recovery.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )
        print(
            "Killed a Generator at render entry; lease recovered; plan reused; rights gate blocked."
        )


if __name__ == "__main__":
    main()
