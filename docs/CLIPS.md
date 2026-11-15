# Real CLIPS

Source-based CLIPS uses FFmpeg and FFprobe for deterministic equal-duration
splitting. It does not select highlights, call an LLM, generate narration, or
publish to social accounts. `synthetic_clip: true` remains an explicit demo.
Failures never substitute synthetic footage. STORY and CARTOON_2D are unchanged.

## Installation

Use Python 3.13, install `requirements.lock`, and install FFmpeg **and FFprobe**
on PATH (`sudo apt install ffmpeg` in WSL2). Windows users need a full FFmpeg
distribution with both executables. `FFMPEG_BIN` and `FFPROBE_BIN` override paths.
The existing Docker image includes both. Run `python -m alembic upgrade head`.
Migration `0003_real_clips` adds source and archive registries without changing
legacy tables. Start the existing FastAPI server and `factory.generator` workers;
the existing operator and worker tokens still apply.

## Complete offline demonstration

```powershell
.\.venv\Scripts\python.exe -m scripts.clips_demo --resolution 360x640
# Full production resolution:
.\.venv\Scripts\python.exe -m scripts.clips_demo --resolution 1080x1920
```

The script creates a real 60-second landscape test video with audio, streams it
through the actual upload API, submits five parts, drains the durable worker,
checks completion, and prints dimensions, durations, codecs, sizes and checksums.
Outputs persist under `data/clips-demo/<run>/jobs/<job>/`: source/original.mp4,
source/metadata.json, clips/part_01.mp4 through part_05.mp4, and manifest.json.
The demo database and content-addressed artifacts also remain in that run folder.
No external services or credentials are needed.

## Process one source into five parts

Upload raw bytes, **not multipart form data**. Supported containers: MP4, MOV,
MKV and WebM. FFprobe verifies contents independently of extension/MIME.

```python
import httpx
with httpx.Client(base_url="http://127.0.0.1:8080/api/v1",
                  headers={"Authorization": "YOUR_OPERATOR_TOKEN"}, timeout=1800) as api:
    with open("authorized-video.mp4", "rb") as video:
        uploaded = api.post("/clips/sources/upload", content=video,
                            headers={"X-Filename": "authorized-video.mp4"})
    uploaded.raise_for_status()
    job = api.post("/clips/jobs", json={
        "mode": "CLIPS",
        "source": {"type": "local_file", "artifact_id": uploaded.json()["artifact_id"]},
        "parts": 5, "split_strategy": "equal_duration",
        "output": {"format": "mp4", "aspect_ratio": "9:16", "resolution": "1080x1920",
                   "fps": 30, "framing": "blurred_background", "preset": "veryfast", "crf": 23},
        "google_drive": {"enabled": True, "create_source_folder": True}
    })
    job.raise_for_status()
    print(job.json())
```

Uploads stream to managed disk and are incrementally hashed. Source IDs are
SHA-256 locators, never client filesystem paths. Original metadata is retained.
`X-Filename` may be URL encoded. The operator page supports the same workflow,
including framing, preview profile, Drive toggle, stage errors, per-part preview
and download, archive retry and folder link. `/batches` accepts the same contracts.

## YouTube authorization and restrictions

Replace source with `{"type":"youtube", "url":"https://www.youtube.com/watch?v=VIDEO_ID",
"authorized":true}`. Explicit acknowledgement is mandatory. Ingest only material
you own or are authorized to download and reuse, and only where platform rules
permit downloading or the necessary permissions apply. Ownership does not by
itself override platform restrictions. There is no DRM, paywall or access-control
bypass, cookie import, password or account-token configuration. Restricted,
unavailable, rate-limited or interrupted sources fail clearly; an authorized local
export remains available. Playlists and live streams are unsupported.

An isolated yt-dlp subprocess fetches metadata first, prefers MP4, uses bounded
network retries, and applies duration, byte, time and disk limits. Ingest progress
reports downloaded bytes. Sources registered successfully are reused by video ID.
Ingest stages are serialized to avoid duplicate concurrent downloads. A crash
before registration can require downloading again. YouTube changes can require
updating yt-dlp. See [yt-dlp options](https://github.com/yt-dlp/yt-dlp) and
[YouTube terms](https://www.youtube.com/static?template=terms).

## Splitting and profiles

`parts` is a strict integer from 1 to 50. Exactly one source is required. Only
`equal_duration` is supported. Shared boundaries quantize D*i/N to the output
frame grid; the final end differs from source duration by at most half a frame.
Minimum duration is one second per part by default. VFR sources are decoded into
constant-frame-rate output. Naming and ordering are deterministic.

Outputs: MP4/H.264/yuv420p, source audio encoded as AAC, fast-start metadata,
1080x1920 at 30 FPS by default. Preview: 360x640; 24 FPS is also supported. Quality
is controlled by `preset` and CRF (16–35). `blurred_background` preserves the full
source frame; `center_crop` fills the vertical canvas. Neither stretches the image.
Sample aspect ratio is normalized. Silent sources remain silent. Source audio is
resampled for timing and limited without make-up gain; short audio is silence-padded.
Validation checks dimensions, codecs, pixel format, rate, duration and A/V timing.

## Durability and recovery

Stages: validate_source, ingest_source, probe_source, plan_splits, render_parts,
validate_outputs, package, archive_to_drive. Existing leases, fencing tokens,
heartbeats and retry limits apply. Long downloads/renders run outside transactions.
Finished parts have fenced checkpoints; resumed claims skip completed renders.
The unfinished part is rendered again. Correct missing tools, space or access,
then `POST /clips/jobs/{job}/retry` resumes a failed stage without discarding
successful stages or checkpoints. Three exhausted leases fail a stage.

Local completion and archiving are separate. Archive status is DISABLED,
LOCAL_COMPLETE_DRIVE_NOT_CONFIGURED, PENDING, ARCHIVE_FAILED or ARCHIVED. PENDING
requires a running Drive worker. Backoff retries and explicit archive retry do not
re-render local clips. Enable `DRIVE_ARCHIVE_ENABLED=true` on the API before
submitting archive-enabled jobs when the Drive worker is configured.

The manifest records source/job identity, boundaries, actual durations, frame
counts, dimensions, codecs, byte sizes, SHA-256 and validation status. The API
overlays current Drive IDs and URLs. The archived manifest is an immutable local
completion snapshot; current archive status stays in the database/API, avoiding
a manifest that changes its own checksum. See [Drive setup](GOOGLE_DRIVE.md).

## Routes under /api/v1

- POST /clips/sources/upload
- POST /clips/jobs
- GET /clips/jobs/{job}
- GET /clips/jobs/{job}/parts
- GET /clips/jobs/{job}/parts/{part}/download
- GET /clips/jobs/{job}/manifest
- POST /clips/jobs/{job}/retry
- POST /clips/jobs/{job}/archive/retry

Worker-only cache/checkpoint routes reuse worker authentication. Session URLs and
internal paths are not exposed to operators. This remains a single-operator app;
it does not add tenant isolation or public upload access.

## Limits and operational constraints

| Variable | Default |
|---|---:|
| CLIPS_MAX_SOURCE_BYTES | 2147483648 |
| ARTIFACT_MAX_BYTES | 2147483648 |
| CLIPS_MAX_DURATION | 7200 seconds |
| CLIPS_MIN_PART_SECONDS | 1 |
| CLIPS_MAX_CONCURRENT_UPLOADS | 2 per API process |
| CLIPS_MAX_CONCURRENT_RENDERS | 1 ledger-wide render stage |
| CLIPS_UPLOAD_TIMEOUT | 1800 seconds |
| CLIPS_DOWNLOAD_TIMEOUT | 1800 seconds |
| CLIPS_RENDER_TIMEOUT | 3600 seconds per part |
| SCRATCH_ROOT | data/scratch |
| DRIVE_ARCHIVE_ENABLED | false |

CPU rendering uses two encoding threads, one filter thread and sequential parts;
GPU and model downloads are unnecessary. Disk preflight estimates capacity rather
than reserving it. Set matching source/artifact limits on coordinator and workers.
Use one API process or reverse-proxy limits for aggregate upload control. The API
does not implement resumable client uploads: interrupted uploads must restart.
No automatic retention deletion occurs; sources and outputs remain until explicit
operator cleanup. Hard termination can leave scratch files for manual cleanup
after confirming that no worker is using them. Backup storage and the database.
Offline tests do not establish live YouTube or Drive availability.
