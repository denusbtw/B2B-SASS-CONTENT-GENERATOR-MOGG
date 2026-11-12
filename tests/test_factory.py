from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from factory import storage
from factory.contracts import Cue, JobSpec, Mode, Timeline
from factory.db import Conflict, Ledger, metadata, now, stages
from factory.director import MockGPT, NeedsReview


@pytest.fixture
def ledger(tmp_path):
    value = Ledger(f"sqlite:///{tmp_path / 'test.db'}")
    metadata.create_all(value.engine)
    yield value
    value.engine.dispose()


def spec(mode="STORY", **changes):
    return {
        "mode": mode,
        "prompt": "Мій власний сюжет.",
        "rights": "original",
        "evidence": "Original test",
        **changes,
    }


@pytest.mark.parametrize("mode", list(Mode))
def test_plan_deterministic(mode):
    job = JobSpec.model_validate(spec(mode, synthetic_clip=True))
    director = MockGPT()
    assert director.plan(job) == director.plan(job)
    assert director.plan(job).timelines[0].mode == mode
    assert director.plan(job).cloud_calls == 0


def test_timecode_validation():
    with pytest.raises(ValidationError, match="overlaps or exceeds"):
        Timeline(
            mode=Mode.STORY,
            part=1,
            duration_ms=2000,
            cues=[Cue(start_ms=0, end_ms=2100, text="Late")],
        )


def test_clips_require_supported_source():
    with pytest.raises(NeedsReview):
        MockGPT().plan(JobSpec.model_validate(spec("CLIPS")))


@pytest.mark.parametrize("parts", [3, 8])
def test_clips_ordered_source_spans(parts):
    plan = MockGPT().plan(
        JobSpec.model_validate(spec("CLIPS", parts=parts, synthetic_clip=True))
    )
    assert [item.part for item in plan.timelines] == list(range(1, parts + 1))
    assert len({item.source_start_ms for item in plan.timelines}) == parts


def test_no_filler_parts_for_short_story():
    with pytest.raises(NeedsReview):
        MockGPT().plan(JobSpec.model_validate(spec(parts=8)))


def test_storage_verifies_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "ROOT", tmp_path)
    key = storage.put(b"test")
    assert storage.verified(key).read_bytes() == b"test"
    storage.path_for(key).write_bytes(b"corruption")
    with pytest.raises(ValueError, match="corrupt"):
        storage.verified(key)
    with pytest.raises(ValueError, match="Invalid"):
        storage.verified("../outside")


def test_concurrent_claim_and_expired_lease(ledger):
    ledger.submit([spec()])
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(ledger.claim, ["a", "b"]))
    [claim] = [claim for claim in claims if claim]
    worker = next(worker for worker, result in zip(["a", "b"], claims) if result)
    ledger.finish(claim["id"], worker, claim["token"], result={"plan": 1})
    running = ledger.claim("crashed")
    with ledger.write() as db:
        db.execute(
            update(stages)
            .where(stages.c.id == running["id"])
            .values(expires=(now() - timedelta(seconds=1)).isoformat())
        )
    recovered = ledger.claim("replacement")
    assert recovered["id"] == running["id"]
    assert recovered["outputs"] == {"story_plan": {"plan": 1}}
    assert recovered["token"] != running["token"]
    with pytest.raises(Conflict):
        ledger.finish(running["id"], "crashed", running["token"], result={})


def test_approval_all_or_nothing(ledger):
    snapshot = {"account": "test", "sha256": "abc", "scheduled_at": now().isoformat()}
    with pytest.raises(Conflict):
        ledger.approve([snapshot, snapshot])
    assert ledger.approve([snapshot])
