
# Verified status — 2026-11-17

Full v6 release: **NOT READY**. The connected local clipping path works, but the
entire supplied mandate is not complete. No Google Cloud settings changed,
no real Drive/YouTube upload occurred, and no PR was merged.

## Evidence already obtained

- Windows: **121 tests passed**, including real FFmpeg integration, PostgreSQL
  competing claims, OAuth/refresh mocks, upload recovery and Drive Range safety.
- Linux Docker: **121 tests passed in 62.78 seconds**, using the built image and PostgreSQL.
- Ruff lint, formatter and mypy checks passed on the final code; both Compose configurations validated.
- Docker image built with the pinned dependencies; API and PostgreSQL health checks passed.
- Real own 39.033-second speech/video fixture: faster-whisper small CPU int8 detected
  English and produced 94 word timestamps.
- Real qwen2.5:3b inference on CPU selected a validated source interval.
  One standalone run took 164.72 seconds, including retry; this is not a GPU benchmark.
- Real connected Docker job **128f2cbb-e7d4-4737-a3ba-995e8de0ee1e** completed:
  upload → source probe → Whisper → Ollama → selected clip → captions → render →
  validation → package → library. Manifest reports analyzer=ollama, language=en,
  one output and archive_status=DISABLED.
- Real output artifacts in data/v6-live:
  api-end-to-end.mp4; blurred_background.mp4; gameplay_split.mp4; longs.mp4.
- Standalone 1080×1920 30 FPS H.264/AAC renders passed media validation.
  38.667-second blurred render took 38.70 s and gameplay split took 25.46 s
  on this machine. The standalone renders used the explicitly recorded heuristic
  selection; the separate API output used actual Ollama selection.
- Basic Longs artifact: 1280×720, 22 seconds, source intervals 0–12 and 20–30.
- Headless Chromium: Create controls and defaults, actual library, and no JavaScript
  page errors. This does not constitute acceptance of the missing Editor/Settings screens.
- Existing main working tree and its configuration were preserved.
  v6 feature work is kept in D:/projects/AI-Content-Factory-v6.

## Strict 15-category acceptance result

A category counts only when its complete required acceptance can be supported.
A passing smaller smoke test does not make an entire category pass.

1. Source ingestion — **BLOCKED**: local media and bounded-interval tests pass;
   authorized real 30-minute URLs and a real >2-hour VOD have not been validated.
2. Transcription — **BLOCKED**: real English passed; required multilingual recordings
   and target-GPU fallback behavior have not been validated.
3. Local LLM — **BLOCKED**: real CPU inference and honest fallback passed;
   RTX 3050 4 GB inference/OOM acceptance is not verified.
4. Highlight selection — **PASS**: real local ranking selected a valid, bounded,
   nonoverlapping interval; candidate IDs/ranges, retries and deterministic fallback tested.
5. Shorts renderer — **FAIL**: real render works; automatic primary-subject tracking
   required by the full Smart Crop preset remains unimplemented.
6. Longs renderer — **FAIL**: real chronological manual assembly works; configurable
   silence removal and complete edit/re-render acceptance remain.
7. Gameplay Split — **PASS**: real 50/50 MP4, looping authorized background, correct
   output duration/dimensions and source audio; missing-background input validation tested.
8. Subtitles — **FAIL**: word timestamps, Unicode, SRT/VTT/ASS and burned karaoke work;
   several configurable style presets and the full caption editor remain.
9. Google Drive — **BLOCKED**: resumable transport/refresh/folder mocks pass;
   no usable configured OAuth files discovered in the checked project configuration,
   and no live verified file ID exists.
10. YouTube Publisher — **BLOCKED**: existing approved resumable upload machinery and
    bounded Drive source tests pass; no authorized live private video ID exists.
11. UI workflow — **FAIL**: Create/Process/Library smoke test passes; full editor,
    settings, dashboard, approval-to-live-cloud acceptance remain.
12. Worker A — **PASS**: isolated Docker worker executed the complete real local job,
    with persistent stage results and artifacts.
13. Worker B — **BLOCKED**: worker-only Compose and ledger concurrency tests exist;
    physical PC B and full isolated second-worker acceptance are not verified.
14. Docker deployment — **PASS**: built and started the isolated stack, checked health,
    migrated the test database and restarted it from the permanent project folder.
15. Recovery/cleanup — **FAIL**: fenced leases, checkpoints and upload retry mocks pass;
    verified-cloud garbage collection and bounded failed-job retention remain incomplete.

Strict verified readiness: **4 / 15 = 26.7%**. This percentage measures completion of
the entire broad mandate, not whether the local clipping smoke test works.

## External/manual gates

- Supply or locate the existing **Desktop Drive OAuth** client/token paths; authorize
  the intended account if needed. Preserve existing working tokens and destination IDs.
- Supply the **Web YouTube OAuth** client and a locally generated encryption key;
  connect and confirm the actual channel in YouTube LIVE.
- Use an owned test fixture; verify Drive destination and private permissions.
- Explicitly approve one **private** YouTube draft before a live upload.
- Public publishing requires separate operator approval.
- Validate Windows/WSL NVIDIA access and physical PC B before declaring hardware acceptance.

Full PowerShell setup and all known implementation gaps:
[CONTENT_FACTORY_V6.md](CONTENT_FACTORY_V6.md).
