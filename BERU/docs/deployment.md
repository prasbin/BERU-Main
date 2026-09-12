# Deployment

Operational notes for running BERU outside localhost development.

## Environment handling

All configuration is read from environment variables (see [`.env.example`](../.env.example)).
Key production variables:

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Non-localhost **requires** `BERU_API_KEY`. |
| `PORT` | `8000` | Server port. |
| `BERU_API_KEY` | *(blank)* | Single-user API key. When set, all `/api/v1` routes require `X-API-Key` header. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./beru.db` | SQLite database path. Use an absolute path in containers; the `.db` file must be writable. Postgres is supported too (see below). |
| `LLM_PROVIDER` | `mock` | `mock` (offline) or `openai_compatible`. |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | — | Required when `LLM_PROVIDER=openai_compatible`. |
| `BERU_PROACTIVE_ENABLED` | `true` | Starts background scheduler/monitor loops at startup. |
| `BERU_HEALTH_LLM_PROBE` | `false` | When `true`, `/ready` performs a real LLM generation (costs API credits with a live provider). |
| `LOG_FORMAT` | `text` | `text` (human) or `json` (structured, one object per line). |

## PostgreSQL

BERU supports PostgreSQL as the backend database. Point `DATABASE_URL` at an
async Postgres URL:

```
DATABASE_URL=postgresql+asyncpg://beru:beru@localhost:5432/beru
```

A single command starts a local server matching that URL:

```
docker compose up -d postgres
```

- Schema is managed by Alembic; migrations convert the async URL to the sync
  driver (`psycopg2`) automatically. Migrations are applied automatically at
  application startup (`backend.database.init_db`); to apply them manually from
  the CLI run `alembic upgrade head`.
- The instance lock (`backend/database/lock.py`) and the backup service
  (`backend/services/backup.py`) work against Postgres: backups use native
  `pg_dump`/`pg_restore`, and the lock uses PostgreSQL advisory locks.
- Run the full test suite against Postgres with the embedded-runner harness:
  `pip install -r requirements-pg.txt`, then
  `python -m scripts.test_postgres` (skips the SQLite-only tests, runs every
  other test against a freshly-created throwaway cluster).

## Bind behaviour and safety

BERU **refuses to start** when bound to a non-localhost interface (`HOST` is not `127.0.0.1` / `localhost` / `::1`) and no `BERU_API_KEY` is set. This prevents exposing an unauthenticated server on a reachable interface.

To serve externally:

1. Set `BERU_API_KEY` to a long random string.
2. Bind behind a reverse proxy (Nginx, Caddy, Traefik) with TLS termination.
3. Set `BERU_TRUST_PROXY_HEADERS=true` only when the proxy sets `X-Forwarded-For`; otherwise clients can spoof the header and bypass rate limits.

## TLS / reverse proxy

The app itself does not terminate TLS. Place a reverse proxy in front:

- Terminate TLS (HTTPS).
- Forward `X-Forwarded-For` to BERU only if `BERU_TRUST_PROXY_HEADERS=true`.
- Enable WebSocket upgrade forwarding for `/ws`, `/api/v1/chat/stream`, and voice endpoints.
- Set a reasonable body-size limit (`client_max_body_size 1m` in Nginx matches `BERU_MAX_REQUEST_BODY_BYTES=1048576`).

### Session cookie behind a proxy

The `beru_session` cookie is marked `secure` **only** when the request is seen
over HTTPS (`request.url.scheme == "https"` at the app). Because BERU sits behind
a TLS-terminating proxy, set the proxy to forward the `X-Forwarded-Proto` header
and configure BERU so the app sees HTTPS; otherwise the cookie may be issued
without `Secure` and travel in the clear. Nginx example:

```
proxy_set_header X-Forwarded-Proto $scheme;   # https on TLS connections
proxy_set_header X-Forwarded-For  $remote_addr;  # only with BERU_TRUST_PROXY_HEADERS=true
```

Session cookies are `HttpOnly` + `SameSite=Lax` (see `backend/api/security.py`),
and are validated server-side against a SHA-256 hash — changing `BERU_API_KEY`
invalidates all outstanding sessions.

## Healthchecks

| Endpoint | Purpose | What it proves |
|---|---|---|
| `GET /health` | **Liveness** (lightweight) | Process is up, can accept requests. |
| `GET /ready` | **Readiness** (DB + optional LLM probe) | Database reachable (`SELECT 1`); LLM answering when probed. |

`/ready` returns HTTP **200** when ready and **503** otherwise (with diagnostic details in the JSON body).

**LLM probing**: By default `/ready` only exercises the database. The offline mock provider is always probed (instant, free). For live providers, set `BERU_HEALTH_LLM_PROBE=true` to have `/ready` perform one minimal generation (max 1 token). Keep this off in production healthcheck loops to avoid billing; use it only for scheduled validation or on container start.

## Graceful shutdown

BERU installs a `lifespan` handler (ASGI scope) that on shutdown:

1. Stops the OS hotkey listener.
2. Stops the proactive scheduler/monitor loops (if enabled).
3. Flushes any pending reliability-ledger rows to the database.
4. Closes the LLM client session.
5. Disposes the SQLAlchemy engine (releases all DB connections).
6. Releases the single-instance DB file lock.

Configure your process manager / container runtime to send **SIGTERM** and allow at least 30 seconds for drain (`--timeout-graceful-shutdown 30` in the `run.sh` and `Dockerfile` CMD).

## Running with Docker

Build and run (replace `<API_KEY>` with a random string):

```
docker build -t beru .

docker run --rm -p 8000:8000 \
  -e BERU_API_KEY=<API_KEY> \
  -e LLM_PROVIDER=mock \
  -v beru-data:/app \
  beru
```

SQLite data persists at `/app/beru.db` — mount a volume for production.

## Running directly

```
./run.sh          # sources .env, starts uvicorn (or python3 -m backend.main)
python3 -m backend.main   # equivalent for localhost dev
```

## Installing as a package / extension platform

`pyproject.toml` declares a `[build-system]` (setuptools), so the project can be
installed from source or a wheel:

```
pip install .            # installs runtime dependencies, then run `python -m backend.main`
python -m scripts.release bump patch   # bump drives the version across backend/__init__.py,
                                       # pyproject.toml and Settings before packaging
```

### Plugins (entry-point discovery)

Third-party extensions ship as their own wheels and register through standard
`importlib.metadata` entry-point groups — no BERU code edits needed:

| Group | Contract | Example |
| --- | --- | --- |
| `beru.tools` | `()` → `backend.tools.base.Tool` | `greeting = "beru_sample_tool:GreetingTool"` |
| `beru.agents` | `()` → `backend.agents.base.BaseAgent` | plugin agent classes |
| `beru.llm_providers` | `(settings)` → `LLMProvider` | extra model backends |
| `beru.embedding_providers` | `(settings)` → `EmbeddingProvider` | vector backends |
| `beru.stt_providers` | `(settings)` → `STTProvider` | `whisper` (first-party) |
| `beru.tts_providers` | `(settings)` → `TTSProvider` | `edge_tts` (first-party) |

Built-in providers/tools always win on a name collision (plugins cannot shadow a
reserved name); unloadable plugins are skipped with a warning instead of
crashing startup. `scripts/verify_plugin_install.py` builds BERU's wheel plus a
sample tool wheel, installs both into a fresh virtual environment, and proves
the sample tool is discovered and runnable there — the Stage 5.3 gate.

### Real voice (optional extra)

Whisper (STT) and edge-tts (TTS) are first-party plugins shipped as an optional
extra; without them BERU keeps its hermetic mock providers:

```
pip install -e .[voice]
```

Configure via `.env`:

```
BERU_VOICE_STT_PROVIDER=whisper
BERU_VOICE_STT_MODEL=base        # tiny | base | small | medium | large
BERU_VOICE_TTS_PROVIDER=edge_tts
BERU_VOICE_TTS_VOICE=en-US-JennyNeural
```

Whisper loads its chosen model lazily on first transcription; edge-tts streams
synthesized audio from the `communicate.stream()` API. TTS providers may declare
a `format` class attribute (e.g. edge-tts emits `mp3`), and clips are served
with the matching `audio/<format>` media type; unknown/legacy PCM providers keep
`wav`. `.env.example` documents the same variables.

## Tool command sandbox (host hardening)

Tool commands run through `CommandExecutor`, an allowlist + pattern-blocklist
gate. Guardrail tests (see `tests/test_host_hardening.py`) prove destructive
commands such as privilege escalation (`sudo`, `su`), credential exfiltration
(`curl|sh` piping to network hosts, `nc`/`telnet`, `whoami`, printing of
`/etc/passwd`/`.ssh`/`BERU_API_KEY`, `git` credential reads) are blocked in
every backend — including in-flight pipes and shell forks.

Two backends expose the same interface:

| Backend | Variable | Behaviour |
| ------- | -------- | --------- |
| `host` (default) | `BERU_COMMAND_EXECUTOR=host` | Runs directly on the host. On POSIX you can hang the process under a least-privilege account via `BERU_COMMAND_RUN_USER` / `BERU_COMMAND_RUN_GROUP`. On Windows this account mode is refused (never silently ignored) — use the container backend instead. |
| `container` | `BERU_COMMAND_EXECUTOR=container` + `BERU_COMMAND_CONTAINER=<image>` | Executes inside `docker exec` with `--workdir` and only the explicitly requested environment variables (`-e`) — the host `os.environ` is never leaked in. Falls back to `host` with a warning if no container is configured. |

### Credential scoping

Tools never receive the global API key. When the agent runs a tool it injects a
`ScopedCredentials` view built from `scope_for_tool(settings, required_credentials)`:
only the provider keys the tool declares (`llm_api_key`, `embedding_api_key`)
and that are actually configured are granted; the owner credential
(`BERU_API_KEY`) is structurally absent from `PERMITTED_CREDENTIAL_NAMES`, and a
tool's own `repr`/`str`/history can never render key material because scoped
values are redacted and the scope object is read-only (`MappingProxyType`).

## Data persistence and backup

BERU stores everything in a single SQLite database (`beru.db`). To back up:

```sql
VACUUM INTO '/path/to/backup.db';
```

This captures conversations, facts, proactive scheduler state, task/trigger history, and the reliability ledger in one atomic snapshot. Restore by replacing the file before startup. See [Roadmap — Backup & Restore](roadmap.md) for a dedicated recovery runbook.
