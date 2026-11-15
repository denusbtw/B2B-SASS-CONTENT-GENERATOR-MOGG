# CLIPS implementation verification

Implementation verification snapshot before committing. Baseline HEAD: `6b067b24dbc745a63d1354dad3ec97701972b614`.
At that verification snapshot the branch was `main`, changes were uncommitted,
and no branch, commit, push or PR had been created. Origin points to the original
denusbtw/B2B-SASS-CONTENT-GENERATOR-MOGG repository.

## Capability status

| Capability | Status | Evidence |
|---|---|---|
| Streamed local uploads and source registry | IMPLEMENTED_AND_TESTED | MP4/MOV/MKV/WebM, corrupt/unsupported/oversized inputs |
| Deterministic 1–50-part splitting | IMPLEMENTED_AND_TESTED | 60-second / five-part integration, fractional/VFR fixture |
| Vertical H.264/AAC processing | IMPLEMENTED_AND_TESTED | 1080x1920 demo, preview tests, silent input, A/V timing |
| Crop and blurred foreground framing | IMPLEMENTED_AND_TESTED | Decoded pixel checks preserve red/blue source edges in blur mode |
| Durable stages and render checkpoints | IMPLEMENTED_AND_TESTED | Expired lease takeover, stale fencing rejection, saved part reuse |
| Processing retry and cached source reuse | IMPLEMENTED_AND_TESTED | Two additional targeted API/worker tests |
| Drive folders/uploads/recovery | IMPLEMENTED_AND_TESTED | Mock HTTP transport: child folder, five clips + manifest, saved-session offset, no duplicates |
| YouTube implementation | IMPLEMENTED_NOT_LIVE_VERIFIED | URL/authorization validation, isolated yt-dlp, limits/progress/cache; unavailable-source unit test |
| Live YouTube download | BLOCKED | No operator-authorized real YouTube source supplied |
| Live Google Drive upload | BLOCKED | No configured credentials/consent used |
| Minimal operator UI | IMPLEMENTED_NOT_LIVE_VERIFIED | API tests and JavaScript syntax check; no browser interaction test |
| Existing functionality | IMPLEMENTED_AND_TESTED | Regression suite includes STORY, cartoon, synthetic clips, orchestration, Drive |
| PostgreSQL concurrency runtime | IMPLEMENTED_NOT_LIVE_VERIFIED | Existing PostgreSQL test skipped without TEST_DATABASE_URL |
| Migration | IMPLEMENTED_AND_TESTED | SQLite upgrade, downgrade to 0002, upgrade, alembic check |

## Commands and results

- `python -m pytest -q --cov=factory --basetemp=data/final-regression`:
  **63 passed, 1 skipped**, 77% aggregate factory statement coverage.
- After that suite collected its tests, two added recovery/cache tests were run
  separately: **2 passed, 23 deselected**. They were not counted in the 63.
- `python -m ruff check .`: passed.
- `python -m ruff format --check .`: 39 Python files formatted.
- `python -m mypy factory`: passed, 20 source files.
- Operator embedded JavaScript parsed successfully with Node `new Function`.
- `git diff --check`: passed.
- SQLite migration `upgrade head`, `downgrade 0002_drive_archive`, `upgrade head`
  passed. `alembic check`: no new upgrade operations detected.
- Existing GitHub Actions configuration remains unchanged; hosted CI was not run.
- Tests emitted the existing Starlette/httpx deprecation warning.

## Actual full-resolution demonstration

Command: `python -m scripts.clips_demo --resolution 1080x1920`.
Job: `5642b3d1-c337-4f8c-8a8c-07d9357143f3`.
Output directory:
`data/clips-demo/700e1f1f-edd5-4290-98dc-a3b312c08a59/jobs/5642b3d1-c337-4f8c-8a8c-07d9357143f3/`.
Input: real FFmpeg-generated 60-second 640x360 test pattern with 48 kHz sine audio.
Exactly five MP4 outputs, each H.264/AAC, yuv420p, 1080x1920, 30 FPS.
Local job state COMPLETE; archive LOCAL_COMPLETE_DRIVE_NOT_CONFIGURED.

| File | Boundaries (seconds) | Actual seconds | Bytes |
|---|---|---:|---:|
| part_01.mp4 | 0–12 | 12.000 | 2,733,831 |
| part_02.mp4 | 12–24 | 12.000 | 2,803,240 |
| part_03.mp4 | 24–36 | 12.000 | 2,800,622 |
| part_04.mp4 | 36–48 | 12.000 | 2,787,391 |
| part_05.mp4 | 48–60 | 12.000 | 2,809,330 |

The manifest, source, metadata, clips, database and artifact store remain locally
available under the run directory. No live external integration was exercised.

## File inventory

New:
- docs/CLIPS.md
- docs/CLIPS_VERIFICATION.md
- factory/clips.py — deterministic worker stages and per-part checkpoints
- factory/clips_api.py — authenticated uploads, source registry, job/part/manifest/retry routes
- factory/clips_archive.py — durable job-specific ordered archive
- factory/clips_db.py — source and archive table definitions
- factory/clips_media.py — probe, frame-grid plan, framing, encode, validation
- factory/ingestion.py — authorized and bounded yt-dlp subprocess
- migrations/versions/0003_real_clips.py
- scripts/clips_demo.py
- tests/test_clips.py
- tests/test_clips_drive.py

Modified:
- .env.example — resource limits and archive deployment flag
- README.md — real CLIPS instructions
- compose.yaml — source/worker limits and archive flag
- docs/GOOGLE_DRIVE.md — per-job folders and recovery
- factory/api.py — source-aware result validation and route registration
- factory/contracts.py — typed source/output/archive configuration, strict part count
- factory/db.py — source DAG, fenced checkpoints, bounded render/ingest claims
- factory/drive.py — child folders, JSON MIME support, persistent resumable sessions
- factory/drive_worker.py — new archive dispatch, legacy exclusion, autocommit advisory lock
- factory/generator.py — real CLIPS dispatch and durable error details
- factory/operator.html — source/framing/profile/archive controls and result links
- factory/storage.py — configurable large streamed artifact limit
- migrations/env.py — new source metadata registration
- requirements.lock — pinned yt-dlp 2026.10.26

## Endpoints and migration

All operator endpoints reuse the existing `/api/v1` server and authentication:
POST /clips/sources/upload; POST /clips/jobs; GET /clips/jobs/{job};
GET /clips/jobs/{job}/parts; GET /clips/jobs/{job}/parts/{part}/download;
GET /clips/jobs/{job}/manifest; POST /clips/jobs/{job}/retry;
POST /clips/jobs/{job}/archive/retry. Internal source-cache and fenced checkpoint
routes use existing worker authentication.

Migration 0003 adds `clip_sources` and `clip_archives`. Existing JSON stage
results carry plans/checkpoints/manifests; no unrelated table is rewritten.

## Remaining operational limitations

Live YouTube/Drive behavior and PostgreSQL runtime concurrency need their actual
external environment. UI browser interaction has not been exercised. Incomplete
client uploads restart from zero; finished worker parts and Drive chunk sessions
resume. Upload slots are per API process. Hard worker termination can leave
scratch files for later cleanup. Artifacts are retained indefinitely until
explicit operator cleanup. The archived manifest is an immutable local snapshot;
current Drive IDs/status are overlaid by the API. Disk preflight is an estimate,
not a reservation, and YouTube changes may require updating yt-dlp.
