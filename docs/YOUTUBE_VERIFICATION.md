# YouTube publisher implementation and verification

Implementation base: `06325df03d5c53a1bb697c3da6bbe526c9f6a668` (existing CLIPS).
The repository's main branch was still at `6b067b24dbc745a63d1354dad3ec97701972b614`
when this work began. The delivery branch includes that earlier CLIPS commit as a
prerequisite; YouTube changes do not modify its rendering modules or Drive code.
Commit/PR identifiers are reported separately at delivery.

## Implementation

The existing `targets` table, approval snapshots and Publisher process are reused.
A provider dispatch interface selects MockPublisher or the real YouTubePublisher;
provider-specific queries prevent old mock approvals from being used live.

New modules:
- factory/youtube.py — actual official HTTPS API transport, bounded chunk uploads,
  resumable status queries, channel checks and remote video-state verification.
- factory/youtube_oauth.py — web OAuth authorization code + PKCE, single-use
  browser-bound state, encrypted tokens, refresh, reconnection and revocation.
- factory/youtube_publications.py — rights/part QA checks, draft/approval snapshots,
  per-artifact/channel identity, persistent leases and crash-released locks,
  retry/backoff, uncertain-result handling and honest publication states.
- factory/youtube_api.py — authenticated integration/publication endpoints.
- factory/youtube_db.py — auxiliary tables supplementing the existing targets ledger.

Migration: migrations/versions/0004_youtube_publisher.py adds youtube_connections,
youtube_oauth_states and publication_runtime. No existing table is rewritten.
Its predecessor is the existing 0003_real_clips migration.

Other new files: tests/test_youtube.py, docs/YOUTUBE.md, this verification report.

Modified files:
- factory/publisher.py — explicit provider selection and preserved mock dispatch.
- factory/api.py — YouTube routes/errors; local library removal does not falsely
  cancel a real remote YouTube schedule.
- factory/contracts.py — live metadata, explicit compliance/public approval models.
- factory/operator.html — separate LIVE connection/configuration/review/monitor UI;
  MOCK receipts remain labeled MOCK, and remote schedules require Studio cancellation.
- migrations/env.py — new metadata registration.
- requirements.lock — pinned cryptography/cffi/pycparser dependencies.
- compose.yaml — opt-in provider config, secret mounts only for API/publisher,
  and callback-safe API access logging defaults.
- .env.example — documented provider, client, encryption and upload/retry settings.
- .gitignore — generated YouTube secret/key file patterns.
- README.md, docs/ARCHITECTURE.md, docs/INTEGRATION_READINESS.md — integration docs.

Routes (existing /api/v1 prefix):
- GET /integrations/youtube/status
- GET /integrations/youtube/connect
- GET /integrations/youtube/callback
- POST /integrations/youtube/confirm
- POST /integrations/youtube/disconnect
- POST /publications/youtube
- GET /publications/{id}
- PATCH /publications/{id}
- POST /publications/{id}/approve
- POST /publications/{id}/retry
- POST /publications/{id}/reconcile
- POST /publications/{id}/cancel

Configuration: PUBLISHER_PROVIDER, YOUTUBE_ENABLED, YOUTUBE_CLIENT_ID,
YOUTUBE_CLIENT_SECRET, YOUTUBE_CLIENT_FILE, YOUTUBE_REDIRECT_URI,
YOUTUBE_TOKEN_ENCRYPTION_KEY, YOUTUBE_TOKEN_ENCRYPTION_KEY_FILE,
YOUTUBE_SECRETS_DIR, YOUTUBE_UPLOAD_CHUNK_BYTES, YOUTUBE_REQUEST_TIMEOUT,
YOUTUBE_MAX_ATTEMPTS, YOUTUBE_STATUS_POLL_SECONDS. Defaults are mock/disabled.
The runbook documents external secret files, exact PowerShell commands, scopes,
Cloud Console steps, private testing, scheduling, quotas verified on 2026-11-15,
recovery and revocation. No credentials or generated media belong in the commit.

## Verification status

| Capability | Status | Evidence |
|---|---|---|
| OAuth URL, PKCE and browser-bound state | IMPLEMENTED_AND_TESTED | Mock HTTP; wrong browser, replay, expiration and channel confirmation |
| Token exchange, encryption, refresh and revocation | IMPLEMENTED_AND_TESTED | Encrypted DB values, invalid grants, missing scope, 401 refresh and disconnect |
| MP4 intake, rights/QA and live approval | IMPLEMENTED_AND_TESTED | Real FFmpeg fixture; missing rights, missing/tampered artifact, changed source/snapshot |
| Resumable upload and persisted real ID/URL | IMPLEMENTED_AND_TESTED | Mock Google transport, multiple chunks and database records |
| Private/unlisted/public metadata and scheduled publication | IMPLEMENTED_AND_TESTED | Private payload, future timezone-aware schedule, separate public approval, remote schedule match |
| Idempotency, concurrent workers and crash recovery | IMPLEMENTED_AND_TESTED | OS lock + lease, accepted chunk crash, lost final response, uncertain initiation and expired session |
| Reconciliation and real-provider state separation | IMPLEMENTED_AND_TESTED | No PUBLISHED on upload acceptance, remote processed/public proof, no mock-to-live dispatch |
| Local deletion and reconnect of known remote ID | IMPLEMENTED_AND_TESTED | Existing remote schedule remains monitored, no replacement upload |
| Minimal operator UI | IMPLEMENTED_NOT_LIVE_VERIFIED | API tests and embedded JavaScript syntax check; no browser interaction test |
| Live OAuth/channel/upload/Studio verification | IMPLEMENTED_NOT_LIVE_VERIFIED | No real credentials/owner-authorized Level 3 test used |
| PostgreSQL publication concurrency/recovery | IMPLEMENTED_NOT_LIVE_VERIFIED locally | Dedicated TEST_DATABASE_URL-gated integration test supplied; no local server configured |
| Existing rendering, mock Publisher and Drive | IMPLEMENTED_AND_TESTED | Full local regression suite |

Final local full suite: `python -m pytest -q --basetemp=data/youtube-pr-verification`:
**98 passed, 2 skipped**. Both skips require TEST_DATABASE_URL (the existing
PostgreSQL claim test and the new YouTube PostgreSQL lock/recovery test).
After the final persisted-chunk-size change, the targeted YouTube suite was run
again: **34 passed, 1 skipped** (`data/youtube-session-settings`). This is a
repeated subset, not 34 additional full-suite tests. The suite also emits the
existing Starlette/httpx deprecation warning.

Other checks passed:
- `python -m ruff check .`
- `python -m ruff format --check .` (46 Python files)
- `python -m mypy factory` (25 source files)
- `python -m compileall -q factory migrations`
- Node parsing of the embedded operator script with `new Function`
- `docker compose config --quiet`
- SQLite fresh migration to 0004, downgrade to 0003, upgrade back to 0004
- `alembic check`: no new upgrade operations detected
- `git diff --check`

No live upload, Google OAuth consent, YouTube Studio operation, or channel mutation
was performed. Test Google responses exist only in the test transport; production
code performs actual HTTPS requests and never substitutes them.

## Real blockers and operator actions

Level 3 requires the owner to configure a Google Cloud Web OAuth client and
YouTube Data API, provide an external encryption key, consent in the browser,
confirm the destination channel, and explicitly approve one private test MP4.
Only then should the live publisher process it and verify the ID in Studio.

PostgreSQL runtime verification requires TEST_DATABASE_URL pointing at a disposable
PostgreSQL database with schema-creation rights, or the repository's CI service.
Hosted CI results are reported separately after the PR is created.

Operational boundaries are intentional: one connected channel per installation;
no automatic exactly-once claim, remote deletion or schedule editing. An unknown
upload with neither a usable session nor known video ID stops for operator Studio
inspection. Local cancellation cannot cancel an already configured remote schedule.
