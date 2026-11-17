
# Content Factory v6 implementation and verification

This branch is an incremental implementation, **not a completed production v6 release**.
The existing FastAPI API, PostgreSQL ledger, fenced worker leases, artifact storage,
Drive OAuth and YouTube approval/upload machinery are preserved. No production database
was replaced and no cloud permissions were changed.

## Implemented workflow

- Real local MP4/MOV/MKV/WebM, YouTube and completed Twitch VOD inputs.
- Remote metadata probe with a strict platform URL allowlist.
- Default remote selection: 720p and the first 30 minutes. 480p and 1080p are available.
  A three-hour source is not rejected merely for its original duration; only the selected
  interval is downloaded with yt-dlp download sections. This is a **bounded video-segment
  fallback**, not an audio-first/selective-highlight downloader.
- Local faster-whisper small, CPU int8, automatic language detection, word timestamps,
  bounded 15-minute extraction chunks and persistent JSON/SRT/VTT artifacts.
- Ollama qwen2.5:3b selects IDs of sentence-aligned 20–60 second candidate ranges.
  It cannot invent timestamps. Responses, IDs, scores and confidence are validated.
  Invalid/unavailable inference has bounded retries and an explicit transcript-heuristic
  fallback. Scores are internal estimates. Fewer than the requested clips is valid.
- Real FFmpeg center crop, blurred background and authorized gameplay split.
  Center crop is **not face tracking**. Gameplay loops an uploaded background and keeps
  only source audio. H.264/AAC, audio normalization, karaoke ASS and reusable SRT.
- Basic Longs: chronological manual source intervals, one 16:9 720p/1080p output,
  optional captions. This is interval assembly, not automated storytelling.
- Transcript and per-clip subtitle artifacts join the existing resumable Drive archive.
- Optional Drive-backed YouTube source: verified managed folder/file IDs are checked;
  authenticated HTTP Range reads feed existing resumable YouTube uploads in bounded
  chunks. Default review/approval and uncertain-upload reconciliation remain intact.
- CLIPS-only Compose deployment; old generators remain recoverable in code.
- Separate worker-only Compose file. One worker process per PC; additional same-host
  worker is opt-in. Whisper uses CPU, so it never competes with Ollama for GPU memory.
- Settings show actual Ollama model availability. Configuration remains environment based.

## Upstream verification and license

Default foundation inspected: https://github.com/mutonby/openshorts
at commit af5ea0170fcea605b5f2d2db6dfb3e3636faeed0.

Confirmed blocking dependency for this project's Python 3.13 target:
upstream pins mediapipe==0.10.14, whose binary cannot be installed for this interpreter:
`pip download --no-deps --only-binary=:all: mediapipe==0.10.14` returned
“No matching distribution found.” The full upstream application was **not verified
as runnable**. Its independent cloud queue/storage application was not merged.

The smallest adapter reuses upstream build_transcript_windows, dedupe_overlapping
and snap_clip_to_words. These pure MIT helpers are in factory/vendor/openshorts_selection.py;
the complete upstream MIT notice is in factory/vendor/OPENSHORTS_LICENSE.
Only these helpers are copied. Existing download/render/job/Google components are reused.
Chopify and Clipper were inspected as references; their complete applications are not vendored.
PyAV 19 also failed real faster-whisper decoding with
“open() got an unexpected keyword argument metadata_errors”; av==16.1.0 is pinned
and the real speech check passed with it.

## PC A: first startup (PowerShell)

Run from the checked-out v6 branch, not another project's terminal.

```powershell
Copy-Item .env.example .env
notepad .env
```

For a fresh local installation set POSTGRES_PASSWORD to a chosen value and replace
OPERATOR_TOKEN and WORKER_AUTH_TOKEN with separate random values. Keep those values
private. Do not overwrite an existing working .env; edit its necessary keys instead.
The host DATABASE_URL is for host scripts; Compose sets the internal DB connection.

One-time model setup:

```powershell
docker compose --profile local-ai up -d --build
docker compose exec ollama ollama pull qwen2.5:3b
```

Daily startup:

```powershell
docker compose --profile local-ai up -d
Start-Process http://127.0.0.1:8080/operator
```

Use **Доступ / Налаштування** and enter OPERATOR_TOKEN from your local .env.
Choose a real source, verify source metadata, select semantic analysis, confirm rights,
and generate. A local source defaults to at most its first 30 minutes.
First Whisper use downloads its model once into the named asr-models volume.
For a fast smoke test use a 30–120 second video and the economical output checkbox.
An unconfigured Drive destination does not become an “uploaded” result.

Status and logs:

```powershell
docker compose ps
docker compose logs --tail 80 api worker-a ollama
docker compose stop
```

Optional NVIDIA inference, after Docker Desktop/WSL2 GPU access has been verified:

```powershell
docker compose -f compose.yaml -f compose.gpu.yaml --profile local-ai up -d
docker compose exec ollama ollama ps
```

Whisper intentionally remains CPU int8. RTX 3050 VRAM/performance was not verified in this run.
Do not enable a second same-PC heavy worker to accelerate a single job.

## Existing Google Drive authorization

Do not replace working client/token files. Point DRIVE_SECRETS_DIR at their existing
directory if they already exist. Otherwise enable Drive API in your Google Cloud project,
create a Desktop OAuth client, add your account as a test user when the consent app
is in Testing, and download the client into data/drive-secrets/client.json.

Host authorization needs a browser on the same PC:

```powershell
New-Item -ItemType Directory -Force data/drive-secrets | Out-Null
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install httpx==0.28.1 cryptography==50.0.2
$env:DRIVE_OAUTH_CLIENT_FILE = "$PWD/data/drive-secrets/client.json"
$env:DRIVE_OAUTH_TOKEN_FILE = "$PWD/data/drive-secrets/token.json"
.\.venv\Scripts\python.exe -m factory.drive_oauth
```

Open the printed Google consent URL and authorize the intended account.
Set DRIVE_ARCHIVE_ENABLED=true and DRIVE_SECRETS_DIR=./data/drive-secrets in .env:

```powershell
docker compose --profile local-ai --profile drive up -d
docker compose logs --tail 50 drive
```

Only drive.file scope is requested. Application logic validates the existing managed
AI Video Publisher root and its job folders. That scope does not itself restrict access
to a single folder. The required new AIContentFactory hierarchy has **not** replaced
the current destination, to preserve existing folder IDs and working archives.
Each operator should authorize their account; do not transfer refresh tokens.
Revoke access in the Google account's third-party app connections.

## YouTube connection and approved test

Enable YouTube Data API v3. Use a **Web application** OAuth client with exact redirect
http://127.0.0.1:8080/api/v1/integrations/youtube/callback.
A JSON file with an installed root is a Desktop client and is not the required Web client.
Save the Web client at data/youtube-secrets/client.json.
Generate the token-encryption key locally without printing it:

```powershell
New-Item -ItemType Directory -Force data/youtube-secrets | Out-Null
.\.venv\Scripts\python.exe -c 'from pathlib import Path; from cryptography.fernet import Fernet; p=Path("data/youtube-secrets/token.fernet.key"); assert not p.exists(), "Preserve existing encryption key"; p.write_bytes(Fernet.generate_key())'
```

In .env set:

```dotenv
YOUTUBE_ENABLED=true
PUBLISHER_PROVIDER=youtube
YOUTUBE_CLIENT_FILE=/run/youtube-secrets/client.json
YOUTUBE_TOKEN_ENCRYPTION_KEY_FILE=/run/youtube-secrets/token.fernet.key
YOUTUBE_REDIRECT_URI=http://127.0.0.1:8080/api/v1/integrations/youtube/callback
YOUTUBE_SOURCE_BACKEND=drive
```

```powershell
docker compose --profile local-ai --profile drive up -d
```

Open **YouTube LIVE**, connect the intended channel, create a private draft for an
already archived clip, review its immutable snapshot and explicitly approve it.
No test upload or public publishing was performed by this implementation run.
Existing local validation is still required when creating/approving a draft; the
publisher then reads from Drive. Do not delete the local output before approval.

## PC B

Use the same code/image version as PC A. Put only the controller private URL,
WORKER_AUTH_TOKEN, WORKER_ID=worker-b and local model choices in PC B's .env.
Do not send YouTube/Drive refresh tokens or OpenAI keys to PC B.

```powershell
docker compose -f compose.worker.yaml up -d --build
docker compose -f compose.worker.yaml exec ollama ollama pull qwen2.5:3b
docker compose -f compose.worker.yaml logs --tail 50 worker
```

Set WORKER_API_URL=http://CONTROLLER_PRIVATE_IP:8080.
The controller binds localhost by default. Expose it **only** on its specific private
LAN/Tailscale address, using a local Compose override and a Windows firewall rule limited
to the other PC. There is no public worker port. Physical PC B validation remains pending.
Workers exchange artifacts through the existing controller; direct worker-to-Drive storage
and shared-account destinations are not completed.

## Configuration added

WHISPER_MODEL: tiny/base/small/medium (small default).
ASR_MODEL_ROOT: persistent model cache.
OLLAMA_URL: private Ollama service URL (not an operator-submitted URL).
OLLAMA_MODEL: qwen2.5:3b default.
OLLAMA_REQUEST_TIMEOUT: bounded inference deadline, 180 seconds default.
CLIPS_SCRATCH_BUDGET_BYTES: 8 GiB default.
CLIPS_DISK_RESERVE_BYTES: 4 GiB default.
YOUTUBE_SOURCE_BACKEND: drive in Compose; local remains available for legacy compatibility.
All secrets and generated data remain outside Git.

## Explicit live-test gates

1. Local-only test: review preview, transcript, cut boundaries, source-language captions,
   audio, framing and rights. No publication approval is implied by Generate.
2. Drive: operator connects the intended account and enables Drive archiving; use only
   an owned test fixture. Verify file ID, private permissions and playable MP4.
3. YouTube: operator checks the actual connected channel and selects **private**;
   explicitly approves that exact clip and metadata snapshot.
4. Verify YouTube ID, private state, processing result and retry reconciliation.
5. Public or scheduled-public publishing requires a separate explicit operator approval.
6. Validate PC B on the private network before relying on automatic failover.

## Remaining release blockers

- Real 30-minute YouTube/Twitch end-to-end and >2-hour remote bounded retrieval not verified.
- Audio-first analysis plus download-only-selected-highlight retrieval not implemented.
- Spanish/Russian/Ukrainian speech accuracy on real recordings not verified.
- GPU subject/face tracking, multiple caption styles, adjustable hooks/silence trimming,
  and an interactive timestamp/rank editor with rerender are not implemented.
- Existing HTML dashboard is extended; upstream React application was not transplanted.
- Full Settings configuration, model installation UI and storage/resource dashboard remain.
- Exact AIContentFactory Drive hierarchy, automatic verified-cloud local retention/GC
  and bounded failed-job retention remain. Temporary stage directories are cleaned;
  controller artifacts still persist for preview/recovery and must not be blindly deleted.
- Real Drive credentials/token health and real private YouTube publication require operator
  authorization. No successful real cloud IDs can be reported.
- Existing publisher supports one connected channel; multi-channel unattended automation,
  per-channel limits and automatic metadata/schedule generation remain.
- Physical dual-PC/GPU acceptance and direct worker-to-Drive output storage remain.
- PostgreSQL is retained rather than replacing the working database with SQLite; it is
  the equivalent existing component for the current ledger and Drive advisory locking.
