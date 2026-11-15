# Architecture

The coordinator (FastAPI) owns the job ledger, approval and media transport. Generator workers poll the API with a worker token and never connect directly to the database. PostgreSQL 16 stores stage leases and outputs. The coordinator claims work transactionally with row locks and SKIP LOCKED. The local SQLite fallback uses an immediate write transaction and is intended for development on one host. Migration 0001_initial creates the ledger. The independent Publisher shares the coordinator's database and local archive and can run on the coordinator without a GPU.

The three modes create typed plans, then run render, QA and package stages. Completed stages stay committed across an expired lease. The worker uploads output by SHA-256 and validates downloads. MockPublisher persists a simulated receipt before reconciling it and writes a mock analytics snapshot. The explicitly enabled YouTube provider writes a real upload ID and separately verifies remote processing/privacy before public success.

The archive lives at data/shared by default. Workers use data/scratch and can configure a distinct local path. Publication approval captures the final artifact hash, account, caption, visibility and UTC slot. An authenticated operator provides the snapshot explicitly. Legacy targets remain simulated. Explicitly selected YouTube targets use separate live approval snapshots in the same targets table; OAuth and upload runtime tables supplement that existing ledger. A worker token is trusted at the coordinator boundary and must be handled as a secret.

## Two-laptop topology

Run Docker/PostgreSQL, coordinator and Publisher on one laptop. Keep the database port bound to localhost. Configure a private encrypted network between laptops and expose the coordinator only on its private interface after configuring distinct random operator and worker tokens. Set WORKER_API_URL on the second laptop to that private coordinator address. The second laptop uses a local virtual environment and local scratch. The mock archive stays with the coordinator. Avoid publishing the dashboard or API to the public network. If the host sleeps, polling and publishing pause. The two-process run used one Windows host, so cross-laptop routing remains unverified.

## Trust boundaries

User content is rendered as text. The publisher contacts only fixed Google endpoints or validated Google resumable URLs. Source ingestion follows the CLIPS URL restrictions. FFmpeg receives a fixed argument vector without a shell. Artifact locators accept lowercase hexadecimal SHA-256 only. Approval checks rights, package QA and artifact integrity. Shared role tokens are a local-development mechanism. Per-operator identities, rotation tooling, account-level authorization and authentication rate limiting are pending.

## YouTube publisher

`PUBLISHER_PROVIDER` selects a common dispatch interface. MockPublisher and
YouTubePublisher filter each other's targets out. Existing batch approvals and
mock receipts remain backward compatible; live DRAFT/approve operations use the
same targets/approval/audit model with an immutable metadata/source fingerprint.
The generator, renderers and Drive OAuth/transport are unchanged.

Migration 0004 adds youtube_connections, youtube_oauth_states and
publication_runtime. Only encrypted tokens, PKCE state and upload-session URLs
are stored; the Fernet key is supplied outside the database. Operator API responses
exclude credential/session/lease fields. Channel confirmation and connection
revisions prevent silently authorizing a newly connected account.

A publication has both a renewable fenced lease and a crash-released exclusive
lock (PostgreSQL session advisory lock in autocommit, OS file lock for SQLite).
No database write transaction spans OAuth, video upload or remote-status calls.
The worker persists INITIATING before videos.insert, SESSION before bytes, then
per-chunk offsets and the real video ID. Resumption queries the saved session;
unknown outcomes become NEEDS_RECONCILIATION rather than a new videos.insert.

All live outputs first remain UPLOADED_PROCESSING; videos.list verification can
confirm private, unlisted, scheduled or processed public status. Requested privacy
and UTC publish_at are distinct from observed remote state. Metadata editing is
restricted to unsent records and clears approval. A per-artifact/channel key
prevents duplicate drafts; it is not a YouTube exactly-once guarantee.

See [YouTube operational runbook](YOUTUBE.md) for scope rationale, limits, private
verification, scheduling, reconnection and ambiguous-upload recovery.
