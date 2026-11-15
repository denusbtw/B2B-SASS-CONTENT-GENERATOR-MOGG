# YouTube publishing runbook

The YouTube adapter uses the real Data API v3. Default configuration remains
`PUBLISHER_PROVIDER=mock` and `YOUTUBE_ENABLED=false`; mock receipts never authorize
real uploads. Selecting YouTube without its configuration fails explicitly.
This implementation has offline transport tests, not live upload verification.
Do not run the private-test procedure below without the channel owner's approval.

## Google Cloud and channel connection

1. Create/select a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **YouTube Data API v3** under APIs & Services / Library.
3. Configure Google Auth Platform Branding, Audience and Data Access. For an
   External application in Testing, add the exact Google account as a test user.
4. Create an OAuth client of type **Web application**, separate from Drive's
   desktop client. Register the exact redirect URI, including port and path:
   `http://127.0.0.1:8080/api/v1/integrations/youtube/callback` for local use.
   Use HTTPS for deployments; the app only permits plain HTTP for loopback.
5. Request `https://www.googleapis.com/auth/youtube.upload` for videos.insert and
   `https://www.googleapis.com/auth/youtube.readonly` for channels.list(mine=true)
   and owner-authorized videos.list verification of private videos. No Drive,
   full YouTube management, TikTok or Instagram scope is requested.
6. Download the **web** OAuth JSON outside the repository. Configure either its
   `YOUTUBE_CLIENT_FILE` path or both client ID/secret environment variables.
7. Generate a Fernet encryption key outside the database. Both coordinator and
   publisher need the same key. Protect and back up the key separately from DB
   backups. Losing/changing it without re-encryption requires reconnection and
   can make previously stored upload sessions unrecoverable.
8. Enable the provider explicitly, migrate, and start the existing API. Open
   `/operator`, configure its operator token, select **YouTube LIVE**, then Connect.
   Open the returned Google consent link in the **same browser** that initiated
   connection. Use the exact redirect host for the dashboard too (127.0.0.1, not
   a localhost alias), so the browser-binding cookie is sent. Select the intended
   account/Brand channel and consent.
9. Return to the dashboard, refresh connection status, review channel name and ID,
   and type the channel ID to confirm it. OAuth alone leaves `CONFIRM_CHANNEL`;
   uploads are blocked until this separate operator confirmation succeeds.

The authorization flow uses offline access, PKCE S256, a ten-minute single-use
state, and an HttpOnly SameSite=Lax browser-binding cookie. Only the Google callback
is exempt from the operator header; it validates state and browser binding.
Reconnect uses the same action and invalidates old connection revisions. An
`AUTH_REQUIRED` publication must be explicitly reviewed/approved again after a
confirmed reconnection before further upload; it never silently adopts another
channel. A known remote ID needs only read-only reconciliation with the newly
confirmed same channel, without another upload or a new public-publication action.

## Windows PowerShell setup and Level 1/2 verification

These commands install/test locally. They do not connect Google or upload videos.

```powershell
Set-Location 'C:\Users\Eclipse\Desktop\7 семестр\робота\B2B-SASS-CONTENT-GENERATOR-MOGG'
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
$env:PUBLISHER_PROVIDER = 'mock'
$env:YOUTUBE_ENABLED = 'false'
$env:DATABASE_URL = 'sqlite:///data/youtube-local.db'
New-Item -ItemType Directory -Force data | Out-Null
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy factory
.\.venv\Scripts\python.exe -m pytest tests/test_youtube.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

The suite mocks HTTP **only in tests**. Production OAuth, token refresh, upload and
remote verification all perform actual HTTPS requests through httpx. FFmpeg and
FFprobe must be installed for the generated-media fixture and publication intake.
No production media is modified by the publisher.

## Explicitly authorized Level 3 setup

Only after owner authorization, configure secrets in a terminal. These are manual
instructions, not actions performed during implementation:

```powershell
$youtubeSecrets = Join-Path $env:LOCALAPPDATA 'AIContentFactory\youtube'
New-Item -ItemType Directory -Force $youtubeSecrets | Out-Null
# Save the downloaded Web application OAuth JSON as client.json in this directory.
$env:YOUTUBE_CLIENT_FILE = Join-Path $youtubeSecrets 'client.json'
$env:YOUTUBE_TOKEN_ENCRYPTION_KEY_FILE = Join-Path $youtubeSecrets 'publisher.fernet.key'
# Generate once. Do not overwrite an existing key used by the database.
if (-not (Test-Path -LiteralPath $env:YOUTUBE_TOKEN_ENCRYPTION_KEY_FILE)) {
  .\.venv\Scripts\python.exe -c 'import os; from pathlib import Path; from cryptography.fernet import Fernet; p=Path(os.environ["YOUTUBE_TOKEN_ENCRYPTION_KEY_FILE"]); p.open("xb").write(Fernet.generate_key())'
}
$env:YOUTUBE_REDIRECT_URI = 'http://127.0.0.1:8080/api/v1/integrations/youtube/callback'
$env:PUBLISHER_PROVIDER = 'youtube'
$env:YOUTUBE_ENABLED = 'true'
# Set your existing OPERATOR_TOKEN, WORKER_AUTH_TOKEN and migrated DATABASE_URL.
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8080 --no-access-log
```

Keep the API terminal open. Use a second terminal with the same database, provider
and secret-file configuration for `python -m factory.publisher`. Do not start the
live publisher until an approved test is intended to run. Disable access/query
logging for the callback in reverse proxies too: OAuth callback queries contain
authorization codes. Never enable HTTP body/header debug logging around OAuth.

For Compose, put `YOUTUBE_SECRETS_DIR` in your private `.env` as the host secret
directory, use `/run/youtube-secrets/client.json` and
`/run/youtube-secrets/publisher.fernet.key` for the corresponding `_FILE` variables,
and explicitly enable both provider flags. API/publisher mount this directory
read-only; the generator and Drive worker do not receive these credentials.
Compose disables API access logging. Do not print `docker compose config` with
actual environment secrets; use `docker compose config --quiet` for validation.

## Approve one private test MP4

1. Complete generation and technical QA. The job must declare rights `original`
   or `authorized` and nonempty evidence. Source-based CLIPS is eligible even
   though its legacy mock-social `publishable` flag is false; actual QA and job
   rights are checked independently. Deleted/missing/tampered media is blocked.
2. In the library choose **YouTube LIVE** on that exact part. Enter title,
   description, optional tags/category, and explicitly choose made-for-kids and
   realistic altered/synthetic-media declarations. Animation never supplies these
   declarations automatically. Default privacy is **private**.
3. Create a DRAFT, review its channel ID, source hash and metadata, then explicitly
   approve the real upload. Creating the draft never uploads. Public visibility
   requires a second explicit approval, including for scheduled publication.
4. Start the existing publisher in the live configuration. Watch bytes/progress,
   errors and state in YouTube LIVE. Upload acceptance records a real ID/URL but
   leaves `UPLOADED_PROCESSING`; the next status poll or Reconcile verifies it.
5. Require `UPLOADED_PRIVATE` with a verification timestamp, then independently
   open YouTube Studio and confirm that exact ID, source and privacy. Only this
   completes live verification; mocked tests cannot establish it.

The publisher never uploads to a separate Shorts endpoint or promises Shorts
eligibility. YouTube decides classification; an API upload alone is not a Short.

## Scheduling and publication states

Use an offset-aware ISO 8601 `publish_at`, or the dashboard's local datetime field.
The API stores UTC; the UI labels the browser's local timezone. Scheduled requests
must explicitly request `visibility=public` and approve public access. The upload
request uses `privacyStatus=private` plus `publishAt`; YouTube accepts scheduling
only for private videos that have never been published. Past times are rejected
locally rather than risking immediate publication. Scheduling rejection is a
failure, and a missing/mismatched remote schedule is `NEEDS_RECONCILIATION`.
See [official video status fields](https://developers.google.com/youtube/v3/docs/videos#status.publishAt).

`DRAFT → QUEUED → UPLOADING → UPLOADED_PROCESSING` precedes remote verification.
After verification: `UPLOADED_PRIVATE`, `UPLOADED_UNLISTED`, `SCHEDULED`, `PUBLISHED`
or `FAILED`. Only remote public privacy plus processed/succeeded status permits
`PUBLISHED`. This verifies the API state, not universal playback availability:
regional, copyright or other platform restrictions may still apply.
`AUTH_REQUIRED`, `RETRY_PENDING`, `NEEDS_RECONCILIATION` and `CANCELLED` are separate.
Legacy mock states/receipts remain compatible and are explicitly labeled MOCK.

## Idempotency, retries and recovery

`targets` remains the publication ledger, including approval, immutable snapshot,
due time, state and real post ID. `publication_runtime` stores fenced ownership,
phase, encrypted resumable URL, offset, attempts, error and timestamps. Other new
tables hold encrypted OAuth credentials and expiring OAuth states. Migration
`0004_youtube_publisher` adds these tables; it does not rewrite existing records.

One artifact/channel pair has a stable application publication ID. Duplicate
drafts conflict. PostgreSQL uses a session advisory lock in autocommit mode plus
a fenced, renewable lease; local SQLite uses a crash-released OS file lock plus
the same lease. Network I/O never holds a write transaction. Live and mock dispatch
queries are mutually exclusive, so old mock approval cannot authorize YouTube.

Before videos.insert the worker commits `INITIATING`. It persists the session
before sending video bytes, including the session's chunk size so restart-time
configuration changes do not alter the existing transfer protocol. After an interrupted chunk or lost final response it
queries **the same session** for accepted offset/completion. No fresh session is
created when that outcome is uncertain. An expired session or a crash before its
URL was saved becomes `NEEDS_RECONCILIATION`. This is duplicate prevention, not an
exactly-once guarantee: YouTube has no general upload idempotency key.

Transient errors use bounded exponential backoff with jitter and Retry-After.
Quota exhaustion and permanent metadata/permission failures stop automatic retry.
Fix quota/configuration and use Retry for allowed recoverable errors. Invalid
credentials require reconnect/confirmation and explicit reapproval. Metadata can
be PATCHed only before any session starts; edits clear approval and return DRAFT.
Changing source/rights/QA/manifest or approved metadata blocks an outdated approval.

For an ambiguous result, inspect **YouTube Studio first**. If a matching video
exists, Reconcile with its ID and explicit source-identity acknowledgement; the
adapter verifies its ID and channel. It cannot remotely prove an artifact's hash,
so the operator must check the content. Without a known ID or usable session it
does not start another upload. Resolve such cases with the channel owner rather
than resetting DB state and risking duplicates. Started uploads/remote schedules
must be cancelled in Studio; local cancellation is allowed only before initiation.
The publisher never deletes local artifacts or remote videos. Removing a local
library item does not cancel a real YouTube schedule; monitoring of a known remote
ID continues even after local retention/deletion. Cancel it in Studio explicitly.

## API under /api/v1

All routes require the existing raw `Authorization: <OPERATOR_TOKEN>` header,
except the state/cookie-protected OAuth callback:

- GET /integrations/youtube/status
- GET /integrations/youtube/connect (returns authorization_url; use the same browser)
- GET /integrations/youtube/callback
- POST /integrations/youtube/confirm (channel_id, revision)
- POST /integrations/youtube/disconnect (locally disables first, then revokes)
- POST /publications/youtube (creates DRAFT)
- GET /publications/{id}
- PATCH /publications/{id} (unsent metadata only; invalidates approval)
- POST /publications/{id}/approve (snapshot_hash, channel_id, approve_live_upload,
  approve_public when public)
- POST /publications/{id}/retry
- POST /publications/{id}/reconcile (optional video_id, confirm_remote_identity)
- POST /publications/{id}/cancel (before initiation only)

Publication payload example (intentionally private):

```json
{
  "job": "COMPLETED_JOB_UUID", "part": 1, "channel_id": "CONFIRMED_CHANNEL_ID",
  "title": "Owner-approved private test", "description": "Authorized test",
  "tags": ["test"], "category_id": "22", "visibility": "private",
  "made_for_kids": false, "contains_synthetic_media": false
}
```

After creating a DRAFT, a separate explicit approval can be submitted in PowerShell
only when the owner has approved this exact upload:

```powershell
$headers = @{ Authorization = $env:OPERATOR_TOKEN }
$publicationId = 'PUBLICATION_ID_RETURNED_BY_DRAFT_CREATION'
$record = Invoke-RestMethod "http://127.0.0.1:8080/api/v1/publications/$publicationId" -Headers $headers
$approval = @{ snapshot_hash=$record.snapshot_hash; channel_id=$record.snapshot.channel_id;
               approve_live_upload=$true; approve_public=$false } | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:8080/api/v1/publications/$publicationId/approve" -Method Post -Headers $headers -ContentType 'application/json' -Body $approval
# In a separately configured terminal, process at most one live worker action:
.\.venv\Scripts\python.exe -m factory.publisher --once
# Verify via the authenticated reconcile endpoint and then check YouTube Studio.
Invoke-RestMethod "http://127.0.0.1:8080/api/v1/publications/$publicationId/reconcile" -Method Post -Headers $headers -ContentType 'application/json' -Body '{}'
```

For scheduled publication supply future `publish_at` and `visibility=public` in
the DRAFT, review the UTC/local times, and explicitly set `approve_public=true`.
No real upload was performed as part of implementing this feature.

## Configuration

| Variable | Default/purpose |
|---|---|
| PUBLISHER_PROVIDER | mock; youtube explicitly selects live adapter |
| YOUTUBE_ENABLED | false; must also be true for live |
| YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET | optional alternative to web client file |
| YOUTUBE_CLIENT_FILE | external Web OAuth JSON path |
| YOUTUBE_REDIRECT_URI | loopback callback shown above |
| YOUTUBE_TOKEN_ENCRYPTION_KEY | Fernet key outside DB, or use key file |
| YOUTUBE_TOKEN_ENCRYPTION_KEY_FILE | external key file, read by API and publisher |
| YOUTUBE_SECRETS_DIR | Compose host directory mounted only in API/publisher |
| YOUTUBE_UPLOAD_CHUNK_BYTES | 8388608; positive 256 KiB multiple, at most 64 MiB |
| YOUTUBE_REQUEST_TIMEOUT | 35 seconds; validated range 1–60, below the 120-second lease |
| YOUTUBE_MAX_ATTEMPTS | 8; range 1–50, transient attempts before stopping |
| YOUTUBE_STATUS_POLL_SECONDS | 300; range 30–86400 seconds, remote verification cadence |

Uploads read one chunk at a time. Existing content-addressed storage and
`ARTIFACT_MAX_BYTES` govern intake; increase that limit when needed. The publisher
does not impose the old 256 MiB artifact limit. Auxiliary offsets/sizes use big
integers. Preserve DB and storage backups with the separate encryption key.

## Current official limits and verification requirements

Checked **2026-11-15** against official documentation; these values are documented
here, not hardcoded as application quota counters:

- The current [quota calculator](https://developers.google.com/youtube/v3/determine_quota_cost)
  lists default daily allocations of **100 videos.insert calls**, **100 search.list
  calls**, and **10,000 units for other endpoints**; midnight Pacific is the reset.
  The current [videos.insert reference](https://developers.google.com/youtube/v3/docs/videos/insert)
  lists one unit in the Video Uploads bucket and a 256 GB maximum file size.
  Check your own Cloud Console quotas; defaults can change.
- That videos.insert reference currently says unverified API projects are not
  restricted to private viewing mode. This does **not** replace OAuth consent/app
  verification. Public apps using sensitive user scopes may need
  [OAuth verification](https://developers.google.com/identity/protocols/oauth2/web-server).
  [Quota extensions](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)
  require a compliance audit. Follow the actual project/account restrictions,
  rather than assuming upload/publication permission from a successful OAuth flow.
- External OAuth apps in Testing receive refresh tokens expiring in **7 days**
  for these scopes. Tokens can also be revoked or otherwise invalidated; see
  [Google's refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

## Diagnostics and revocation

`youtube_auth_required` / `invalid_grant`: reconnect the intended channel, confirm
its ID, and explicitly reapprove affected publications. `youtube_quota_exhausted`:
inspect project quota in Cloud Console and account upload restrictions; correct
them before a manual Retry. Rate limiting retries with backoff. Metadata rejection
needs a corrected unsent DRAFT and new approval. A remote schedule not confirmed
or expired upload session requires reconciliation, not another videos.insert.

Disconnect first disables local use, calls Google's revocation endpoint, and
removes encrypted credentials only after accepted revocation. If revocation fails,
the disabled encrypted record remains so Disconnect can retry; alternatively revoke
the app in [Google Account connections](https://myaccount.google.com/connections).
Previously uploaded videos and schedules remain on YouTube. Disconnection is not
a remote video/schedule cancellation. Use YouTube Studio for those operations.

The application still uses a shared operator token and one confirmed channel per
installation. Multi-tenant roles, automatic video deletion, remote schedule editing,
and content-hash verification on YouTube are outside this integration.
