# Delivery evidence

Verdict: PARTIAL_WITH_BLOCKERS against the full mandate. The local end-to-end demonstrator runs. The open items in DECISIONS_PENDING.md prevent the full acceptance verdict.

## Exercised

- Windows, Python 3.13.17, Docker PostgreSQL 16. Migration 0001_initial applied with Alembic.
- PowerShell `scripts/demo.ps1 -Recovery` ran against PostgreSQL. Batch `7cf1ea0d-6158-4325-a19e-b677d0c93294` finished 20 of 20 jobs in 22.29 seconds on this host. Worker A committed 43 stages and worker B committed 37. This is a synthetic fixture run, not a laptop throughput forecast.
- The sample exports under `data/demo` contain three real vertical MP4s and SRTs. The run decoded H.264 video with AAC tones at 360×640 and confirmed 4.0-second duration per exported sample. The QA filters passed black-frame and audio-level checks. A separate FFprobe run confirmed all three at 360×640, 24 fps, H.264/AAC and 4.0 seconds. The verification command is python -m scripts.verify_media with FFPROBE_BIN or a PATH executable.
- One STORY package was approved for three mock accounts. Repeated independent Publisher processes reconciled all three distinct mock IDs and stored synthetic analytics.
- Recovery driver terminated a Generator process at a deliberately paused render entry, waited for actual lease expiry, and checked that another worker completed attempt 2 without repeating the finished plan. Unknown rights caused approval to return HTTP 409.
- `python -m pytest -q --cov=factory` with `TEST_DATABASE_URL`: 22 passed, 69% overall coverage, 95% contracts, 89% ledger, 94% Director, 90% media. Starlette emitted one deprecation warning for the HTTPX-backed TestClient. The real PostgreSQL integration test used competing claims and rejected a second completion.
- `python -m ruff check .`, `python -m ruff format --check .`, `python -m mypy factory`, and `python -m pip check` passed locally. The checked-in GitHub Actions job has not run on GitHub.

## Remaining gates and proposed PRs

1. Persist full source manifests and add rights-verified local clip intake. Acceptance is a three- and eight-part render using one ingested original fixture, with source spans, indexed scenes and transcript evidence.
2. Add versioned scene and QA revision contracts. Acceptance is a planted technical defect corrected by a bounded revision while leaving earlier committed assets intact.
3. Complete publication states and authorization. Acceptance is draft review with immutable approval, per-account scheduling, ambiguous upload reconciliation and refusal of stale approvals.
4. Run private two-laptop tests and extend the acceptance matrix. Acceptance includes a coordinator outage, checkpoint reporting, corrupted transfers, resource limits, zero cloud calls and a reproducible benchmark.
