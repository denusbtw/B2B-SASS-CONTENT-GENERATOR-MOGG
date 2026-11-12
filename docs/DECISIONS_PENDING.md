# Decisions and open acceptance gates

- PostgreSQL is the supported coordinator database. SQLite is a single-machine development fallback. The first migration implements the stage ledger and approval snapshots. Separate project, source, provider call and analytics history entities are still needed.
- CLIPS currently renders original FFmpeg test footage using timestamped cuts and optional mirrored stacking. Authorized local-file ingest, source evidence, ASR, scene analysis and narrative clip selection remain incomplete. External URLs are rejected by omission of a URL input.
- STORY uses the original text and deterministic shortening to a few cues. Audio is a generated tone. TTS, translation and forced alignment are deferred. No real semantic analysis is claimed.
- CARTOON_2D uses a geometric two-character scene. A typed scene action vocabulary, independent asset packs and more expressive camera/rig controls remain incomplete.
- QA checks media container properties and timestamp contracts. QA also rejects sustained black frames and inaudible audio at the demo thresholds. Clipping detection, configurable thresholds, frame evidence and bounded revision workflows remain incomplete.
- Ledger stages have three attempts, expiring fenced leases and a heartbeat. Durable per-worker checkpoints during coordinator outages, selective retries from the operator UI, cancellation, complete budget policy, retention, metrics and a general idempotency-key API remain incomplete.
- Publisher simulates a scheduled receipt and durable reconciliation. Per-platform adapters, per-account pacing, cancellation, series ordering, separate draft/revision UI, independent provider failure injection and platform governance remain incomplete.
- Operator UI supports JSON submission, inspection, preview and batch approval. It needs more detailed stage controls, integration screens and calendar controls.
- Formal performance benchmarking, second-machine testing and full acceptance matrix coverage remain open.
