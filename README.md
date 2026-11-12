# AI Content Factory

Local mock build for STORY, CLIPS and CARTOON_2D. FFmpeg creates playable vertical MP4s. The model, narration tones, shared archive and publishing run locally. Social actions are simulations.

## PowerShell quickstart

Use Python 3.13.17 and Docker Desktop with Linux containers. From the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
$env:POSTGRES_PASSWORD = [guid]::NewGuid().ToString('N')
$env:DATABASE_URL = "postgresql+psycopg://factory:$env:POSTGRES_PASSWORD@127.0.0.1:55432/factory"
docker compose up -d --wait
.\scripts\demo.ps1 -Recovery
```

Keep the database password for the existing volume. Setting another password in Compose does not change the role password inside an initialized volume. Use `docker compose stop` to stop PostgreSQL and preserve its volume. The demo stops its own API and worker processes. It exports MP4, SRT, and JSON evidence into `data/demo`.

### Interactive operator

Set the two tokens and database URL in each participating terminal using local environment variables. Use the same operator token in the dashboard, and the same worker token in the API and both worker terminals. Generate unique tokens and keep them local.

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8080
# Separate terminals
.\.venv\Scripts\python.exe -m factory.generator --id worker-a
.\.venv\Scripts\python.exe -m factory.generator --id worker-b
.\.venv\Scripts\python.exe -m factory.publisher
```

Open http://127.0.0.1:8080/operator. Submit a JSON array of up to 20 jobs, then open Review packages and preview a finished video. The demo script shows a complete input array and approval format. Example approval payload for the UI is an array containing an object with `job` (a completed UUID), `part`, `account` (for example `mock-youtube-a`), `caption`, and `scheduled_at` (an ISO timestamp with UTC offset). A second approval of the same media, account, and UTC slot is rejected.

### Tests

```powershell
$env:TEST_DATABASE_URL = $env:DATABASE_URL
.\.venv\Scripts\python.exe -m pytest -q --cov=factory
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy factory
```

Place FFprobe on PATH or set `FFPROBE_BIN` to its absolute path. If absent, the worker verifies dimensions, codecs and duration by decoding via FFmpeg. `FFMPEG_BIN` overrides the executable. Otherwise the worker prefers the system FFmpeg, then the binary supplied by the locked imageio-ffmpeg package.

See [architecture](docs/ARCHITECTURE.md), [decisions and gaps](docs/DECISIONS_PENDING.md), [animation capabilities](docs/ANIMATION_CAPABILITIES.md), and [integration readiness](docs/INTEGRATION_READINESS.md).
