# AI Content Factory

Local mock build for STORY, CLIPS and CARTOON_2D. FFmpeg creates playable vertical MP4s. The model, narration tones, shared archive and publishing run locally. Social actions are simulations.

## Docker Compose quickstart

Start Docker Desktop with Linux containers. From the repository root:

```powershell
docker compose up --build -d --wait
```

Compose starts PostgreSQL, applies migrations, and starts the API, two Generator
workers, and MockPublisher. The first build downloads the base images and dependencies.

1. Open [the operator dashboard](http://127.0.0.1:8080/operator).
2. Enter `factory-local-operator` in **Operator token** and click **Submit batch**
   to send the prefilled STORY example.
3. Click **Review packages** after the worker finishes, then select the package to
   preview the MP4. **Workers** shows both Generators. **Scheduled and published**
   shows the mock publication targets.

Use `docker compose logs -f api worker-a worker-b publisher` to follow the services
and `docker compose down` to stop them while keeping PostgreSQL data and the media
under `data/shared`.

The included credentials are for local mock development. Both published ports
bind to localhost. Set `OPERATOR_TOKEN`, `WORKER_AUTH_TOKEN`, and
`POSTGRES_PASSWORD` in the environment if you need different local credentials.
Use a URL-safe database password. Changing it in Compose does not rotate an
existing PostgreSQL role password.


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
