import argparse
import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep

import httpx

from factory.cartoon import render_episode
from factory.contracts import EpisodeScript, JobSpec, Plan, Timeline
from factory.director import MockGPT, NeedsReview
from factory.media import inspect_video, render

from factory.openai_director import ProviderError, plan_story

DIRECTOR = MockGPT()


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response


def upload(client, payload):
    remote = request(client, "POST", "/artifacts/upload", content=payload).json()[
        "sha256"
    ]
    if remote != hashlib.sha256(payload).hexdigest():
        raise ValueError("Uploaded artifact hash mismatch")
    return remote


def upload_file(client, path):
    with path.open("rb") as handle:
        expected = hashlib.file_digest(handle, "sha256").hexdigest()
        handle.seek(0)
        remote = request(client, "POST", "/artifacts/upload", content=handle).json()
    if remote["sha256"] != expected:
        raise ValueError("Uploaded artifact hash mismatch")
    return expected


def execute(client, claim):
    name = claim["name"]
    spec = JobSpec.model_validate(claim["spec"])
    if name.endswith("_plan"):
        plan = plan_story(spec) if spec.provider == "openai" else DIRECTOR.plan(spec)
        return plan.model_dump(mode="json")
    if name == "package":
        return {}
    results = claim["outputs"]
    plan = Plan.model_validate(
        next(value for key, value in results.items() if key.endswith("_plan"))
    )
    scratch = Path(os.getenv("SCRATCH_ROOT", "data/scratch"))
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=scratch) as directory:
        work = Path(directory)
        if name == "render":
            outputs = []
            if spec.provider == "openai":
                assert plan.story is not None
                target = spec.duration_seconds / (
                    spec.parts if spec.duration_scope == "series" else 1
                )
                for index, data in enumerate(plan.story["episodes"], 1):
                    directory = work / str(index)
                    directory.mkdir()
                    episode = EpisodeScript.model_validate(data)
                    video, captions, timeline = render_episode(
                        episode, index, spec.voice, directory, target
                    )
                    outputs.append(
                        {
                            "sha256": upload_file(client, video),
                            "captions": upload(client, captions.encode()),
                            "timeline": timeline.model_dump(mode="json"),
                            "speech_calls": len(episode.scenes),
                        }
                    )
                return {"parts": outputs}
            for timeline in plan.timelines:
                part = work / str(timeline.part)
                part.mkdir()
                payload, captions = render(timeline, part)
                outputs.append(
                    {
                        "sha256": upload(client, payload),
                        "captions": upload(client, captions.encode("utf-8")),
                    }
                )
            return {"parts": outputs}
        media = []
        artifacts = results["render"]["parts"]
        for index, artifact in enumerate(artifacts):
            timeline = (
                Timeline.model_validate(artifact["timeline"])
                if spec.provider == "openai"
                else plan.timelines[index]
            )
            key = artifact["sha256"]
            video = work / f"{timeline.part}.mp4"
            with client.stream("GET", f"/artifacts/{key}/download") as remote:
                remote.raise_for_status()
                with video.open("wb") as output:
                    for chunk in remote.iter_bytes():
                        output.write(chunk)
            with video.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != key:
                    raise ValueError("Downloaded artifact hash mismatch")
            media.append(inspect_video(video, timeline))
        return DIRECTOR.review(media).model_dump(mode="json")


def tick(client, worker):
    claim = request(client, "POST", "/workers/claim", json={"worker": worker}).json()
    if claim is None:
        return False
    path = "/workers/stages/" + claim["id"]
    lease = {"worker": worker, "token": claim["token"]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(execute, client, claim)
        try:
            from concurrent.futures import TimeoutError

            while True:
                try:
                    output = task.result(timeout=8)
                    break
                except TimeoutError:
                    if task.done():
                        raise
                    request(client, "POST", path + "/heartbeat", json=lease)
            request(
                client,
                "POST",
                path + "/complete",
                json={"lease": lease, "result": output},
            )
        except ProviderError as error:
            request(client, "POST", path + "/fail", json={**lease, "code": error.code})
        except NeedsReview:
            request(
                client,
                "POST",
                path + "/fail",
                json={**lease, "code": "review_required"},
            )
        except (subprocess.TimeoutExpired, httpx.TransportError):
            request(client, "POST", path + "/fail", json={**lease, "code": "transient"})
        except (ValueError, OSError, httpx.HTTPStatusError):
            request(
                client, "POST", path + "/fail", json={**lease, "code": "render_error"}
            )
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default=os.getenv("WORKER_ID", "worker-a"))
    parser.add_argument("--drain", action="store_true")
    args = parser.parse_args()
    url = os.getenv("WORKER_API_URL", "http://127.0.0.1:8080")
    with httpx.Client(
        base_url=url.rstrip("/") + "/api/v1",
        trust_env=False,
        timeout=30,
        headers={"Authorization": os.environ["WORKER_AUTH_TOKEN"]},
    ) as client:
        while True:
            try:
                if not tick(client, args.id):
                    if args.drain:
                        return
                    sleep(1)
            except httpx.TransportError as exc:
                print(f"Coordinator unavailable: {type(exc).__name__}", flush=True)
                sleep(2)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {409, 502, 503, 504}:
                    raise
                sleep(2)


if __name__ == "__main__":
    main()
