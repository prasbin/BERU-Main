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
| `DATABASE_URL` | `sqlite+aiosqlite:///./beru.db` | SQLite database path. Use an absolute path in containers; the `.db` file must be writable. |
| `LLM_PROVIDER` | `mock` | `mock` (offline) or `openai_compatible`. |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | — | Required when `LLM_PROVIDER=openai_compatible`. |
| `BERU_PROACTIVE_ENABLED` | `true` | Starts background scheduler/monitor loops at startup. |
| `BERU_HEALTH_LLM_PROBE` | `false` | When `true`, `/ready` performs a real LLM generation (costs API credits with a live provider). |
| `LOG_FORMAT` | `text` | `text` (human) or `json` (structured, one object per line). |

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

## Data persistence and backup

BERU stores everything in a single SQLite database (`beru.db`). To back up:

```sql
VACUUM INTO '/path/to/backup.db';
```

This captures conversations, facts, proactive scheduler state, task/trigger history, and the reliability ledger in one atomic snapshot. Restore by replacing the file before startup. See [Roadmap — Backup & Restore](roadmap.md) for a dedicated recovery runbook.
