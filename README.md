# AI Content Factory

Local mock build for STORY, CLIPS and CARTOON_2D. FFmpeg creates playable vertical MP4s. The model, narration tones, shared archive and publishing run locally. Social actions are simulations.

See [CARTOON_2D with OpenAI](docs/CARTOON_PIPELINE.md) for the optional live planning and narration path.

## Docker Compose quickstart

Start Docker Desktop with Linux containers. From the repository root:

```powershell
docker compose up --build -d --wait
```

Compose starts PostgreSQL, applies migrations, and starts the API, two Generator
workers, and MockPublisher. The first build downloads the base images and dependencies.

1. Open [the operator dashboard](http://127.0.0.1:8080/operator).
2. In **Студія**, click **Запустити генерацію** to send the prefilled STORY example. The queue refreshes while jobs run.
3. Open **Бібліотека** and click **Переглянути**. Use **Доступ** to enter a custom operator token if configured.

Use `docker compose logs -f api worker-a worker-b publisher` to follow the services
and `docker compose down` to stop them while keeping PostgreSQL data and the media
under `data/shared`.

The included credentials are for local mock development. Both published ports
bind to localhost. Set `OPERATOR_TOKEN`, `WORKER_AUTH_TOKEN`, and
`POSTGRES_PASSWORD` in the environment if you need different local credentials.
Use a URL-safe database password. Changing it in Compose does not rotate an
existing PostgreSQL role password.


Optional: connect a personal Google Drive and archive completed MP4s automatically. [Google Drive setup](docs/GOOGLE_DRIVE.md)

## Run Python on the host

For host-side development, stop the full Compose stack first if it is running. Use Python 3.13.17 and Docker Desktop with Linux containers. From the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
$env:POSTGRES_PASSWORD = [guid]::NewGuid().ToString('N')
$env:DATABASE_URL = "postgresql+psycopg://factory:$env:POSTGRES_PASSWORD@127.0.0.1:55432/factory"
docker compose up -d --wait db
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

Open http://127.0.0.1:8080/operator. Submit up to 20 jobs from **Студія**. In **Бібліотека**, preview a video or use **Запланувати** to approve a mock publication for selected accounts. **Публікації** shows the resulting targets.

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
# Real video CLIPS

CLIPS accepts streamed MP4/MOV/MKV/WebM uploads or explicitly authorized YouTube
sources, divides them into 1–50 equal-duration vertical MP4 parts using FFmpeg,
and optionally archives each job in a dedicated Drive folder. No LLM is used.
See [CLIPS setup, API, demonstration and recovery](docs/CLIPS.md).

Offline demonstration: `python -m scripts.clips_demo --resolution 360x640`.
Service deployments must run `python -m alembic upgrade head`.

## Real YouTube publishing (opt-in)

The existing publisher now supports a real YouTube Data API v3 adapter alongside
MockPublisher. Defaults remain `PUBLISHER_PROVIDER=mock` and `YOUTUBE_ENABLED=false`.
Live operation requires separate OAuth consent, encrypted token storage, channel
confirmation, rights/QA and explicit per-video approval. Uploads use persistent
resumable sessions; upload acceptance never by itself means public availability.
See [YouTube setup, private test, scheduling and recovery](docs/YOUTUBE.md).
Run migration `0004_youtube_publisher` before using the integration. Offline tests
need no Google credentials. No real video was uploaded during implementation.
