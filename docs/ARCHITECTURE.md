# Architecture

The coordinator (FastAPI) owns the job ledger, approval and media transport. Generator workers poll the API with a worker token and never connect directly to the database. PostgreSQL 16 stores stage leases and outputs. The coordinator claims work transactionally with row locks and SKIP LOCKED. The local SQLite fallback uses an immediate write transaction and is intended for development on one host. Migration 0001_initial creates the ledger. The independent Publisher shares the coordinator's database and local archive and can run on the coordinator without a GPU.

The three modes create typed plans, then run render, QA and package stages. Completed stages stay committed across an expired lease. The worker uploads output by SHA-256 and validates downloads. The publisher persists a simulated receipt before reconciling it and writes a mock analytics snapshot after confirmation.

The archive lives at data/shared by default. Workers use data/scratch and can configure a distinct local path. Publication approval captures the final artifact hash, account, caption, visibility and UTC slot. An authenticated operator provides the snapshot explicitly. Publishing remains local and simulated for all account types. A worker token is trusted at the coordinator boundary and must be handled as a secret.

## Two-laptop topology

Run Docker/PostgreSQL, coordinator and Publisher on one laptop. Keep the database port bound to localhost. Configure a private encrypted network between laptops and expose the coordinator only on its private interface after configuring distinct random operator and worker tokens. Set WORKER_API_URL on the second laptop to that private coordinator address. The second laptop uses a local virtual environment and local scratch. The mock archive stays with the coordinator. Avoid publishing the dashboard or API to the public network. If the host sleeps, polling and publishing pause. The two-process run used one Windows host, so cross-laptop routing remains unverified.

## Trust boundaries

User content is rendered as text. No user-provided URL is fetched and FFmpeg receives a fixed argument vector without a shell. Artifact locators accept lowercase hexadecimal SHA-256 only. Approval checks rights, package QA and artifact integrity. Shared role tokens are a local-development mechanism. Per-operator identities, rotation tooling, account-level authorization and authentication rate limiting are pending.
