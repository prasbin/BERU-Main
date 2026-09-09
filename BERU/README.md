# BERU

**BERU** is a modular personal AI operating system / agent backend. This
repository contains the **v0.1.0 foundation**: a clean, tested, extensible
FastAPI backend that can hold a conversation through a provider-agnostic LLM
layer and persist it — the base that every future capability (memory, agents,
tools, voice, device control) builds on.

> Status: **Agent + tool + automation-enabled.** A real Playwright browser
> engine, desktop control (mouse/keyboard/clipboard + window management,
> multi-monitor, durable global hotkeys), voice abstractions, the full proactive
> runtime (scheduler + event monitor + notifications with audit history), and a
> **durable** reliability ledger (DB-persisted tool-call activity + approval
> audit trail restored at startup) all work and are surfaced through the web
> UI. Sensitive tools stay gated behind
> explicit confirmation, and any subsystem whose backend is missing (e.g.
> Playwright or a voice provider) degrades gracefully to `unavailable` — real
> results are never faked. See [`docs/roadmap.md`](docs/roadmap.md).

---

## What works today

- **FastAPI backend** with health/status endpoints, versioned API, CORS, a
  per-request correlation id, and a consistent JSON error envelope.
- **Conversational chat**: send a message, get an AI reply, and have the whole
  turn persisted atomically.
- **Streaming (SSE)**: stream the reply token-by-token over Server-Sent Events.
  Short-lived DB sessions commit the conversation + user message before the
  first byte and the assistant message after the stream ends — no session is
  held open across the stream, so a long turn never pins the database.
- **Provider-agnostic LLM layer**: a built-in offline **mock** provider (default)
  and an **OpenAI-compatible** provider that works with OpenAI, Groq, OpenRouter,
  Ollama, LM Studio, and similar endpoints — switching is a config change.
- **Persistence** via SQLAlchemy 2.0 (async) + SQLite; conversations and messages
  with full history retrieval. Schema is managed by **Alembic migrations**
  (applied automatically at startup).
- **Short-term memory**: a recent-message window is assembled and fed to the model.
- **Summarising memory**: when history exceeds a threshold, older messages are
  compressed into a summary via the LLM; recent messages kept verbatim.
- **Agent architecture**: a registry with the default **BERU Core** agent; more
  agents plug in through the same interface.
- **Tool execution**: 66 tools across four agents (BERU Core, IGRIS, DHANUS,
  TANK) — clock, web search, file/document analysis, document generation,
  calendar, browser navigation/click/type/select/drag/cookies/storage/network/
  block/redirect/fulfill/console/download/uploads/frame-scoped actions/page-info/scroll/press/back/
  forward/refresh/screenshot/extract/close/snapshot-restore/launch/clear-downloads, system command launch/notification,
  desktop
  mouse/keyboard/clipboard/screenshot/processes, windows/monitors/durable
  hotkeys, and voice. Agents discover registered tools, execute them within a turn, and
  reason over the real results. Confirmation-required tools (command execution,
  app launching, notifications) return a `confirmation_required` result; the
  client approves via `POST /api/v1/chat/confirm` and only then is the action
  performed. Command execution enforces the destructive-command blocklist and
  subprocess timeouts. Subsystems whose backend is missing (e.g. Playwright or
  a voice provider) advertise their tools as unavailable instead of faking
  success.
- **Browser automation (real)**: a Playwright-backed engine
  (`backend/engines/browser.py`, headless Chromium) with page management,
  navigation, click/type, select-option (single or multi), drag & drop,
  session cookies (get/set/clear), localStorage (get/set/clear), a
  per-page network-response tap, session URL-glob blocking, **302 redirect
  rewriting, fix-response fulfilment (API mocking) — with extra response
  headers — and its own per-entry HTTP status, response size, and timing**,
  a per-page console/page-error tap (with source location), file download
  listing + upload, **download-directory clearing**, session-state
  snapshot/restore (cookies + localStorage to a named file that relaunches a
  fresh context), a **relaunch action (`browser_launch`) that toggles headless
  mode and sets viewport/user-agent/locale, plus device-scale factor,
  mobile/touch emulation, and timezone**, env-overridable screenshot/download/
  session directories, frame-scoped
  actions (name/URL/index targeting), popup-tab auto-tracking, scroll (wheel +
  scroll-to-selector), press-key, page inspection (URL/title/visible-text/
  links/interactive elements/frames), full- or viewport-page screenshots,
  text/HTML extraction, whole-session close, and a rich `/status`
  (mode/pages/globs/redirects/fulfills/dirs/device-options); wired into the tools
  (`backend/tools/browser.py`, 28 browser tools), the key-protected
  `/api/v1/browser/*` API, and the frontend **Browser** tab. Degrades
  gracefully to `unavailable` if
  Playwright is not installed (`playwright install chromium`).
- **Desktop control (Phase 1)**: real mouse (position/move/click/double-click/
  scroll), keyboard (type/press/hotkey), clipboard (read/write), screenshot, and
  process inspection via `/api/v1/desktop/*`, exposed as tools and in the
  frontend **Desktop** tab.
- **Desktop control (Phase 2)**: window management (list/focus/graceful-close by
  handle or title), multi-monitor geometry, live window streaming over SSE
  (`/api/v1/desktop/windows/stream`), and **durable global hotkeys** that persist
  to `data/hotkeys.json` and are re-bound to the OS on startup — all exposed as
  tools, API endpoints, and panel controls in the frontend **Desktop** tab.
- **Frontend panels**: a Browser tab (create/list/activate/close pages, navigate, interact, read), a Desktop tab, a searchable + filterable **Tools** tab (category grouping, permission/confirmation chips), a **Memory** tab (facts + projects), a **Plans** tab (goal → decomposed plan with step execution), a **Notify** tab (notification inbox), an **Automation** tab that manages tasks and triggers end to end — create, run/pause/resume/
  cancel, edit interval/retention via PATCH, history drill-down with pruning,
  and trigger create / pause / resume / disable / fire history — a
  **Reliability** tab that shows the activity ledger (recent tool-call
  outcomes) and approval audit trail with a live summary, a
  durable/in-memory storage badge, and a clear-ledger control, and a
  **Settings** tab (API key, LLM connection test).
- **Auth**: single-user API key via `BERU_API_KEY` / `X-API-Key` header; refuses
  non-localhost binding without a key.
- **Reliability & observability**: a **persistent activity ledger**
  (`backend/services/activity_ledger.py`) records every tool-call outcome —
  success, failure, permission denied, or confirmation required, with duration,
  request id, and a bounded argument/error summary — written by
  `BaseAgent.run_tool`; an **approval audit trail** logs every `confirm`/`deny`
  decision made through `ChatService.confirm_tool_call`/`deny_tool_call`. Both
  are mirrored to the `activity_records` / `audit_records` tables
  (Alembic migration `d4e5f6a7b8c9`) in a best-effort, fire-and-forget write,
  and `restore_activity_ledger()` reloads the newest entries into memory at
  startup (pruning rows beyond the bounded 500-activity / 200-audit window),
  so observability survives restarts. Exposed over the key-protected
  `/api/v1/reliability/*` routes (activity, audit, summary, and clear — which
  also purges the persisted rows), summarised inline in `GET /api/v1/status`,
  and readable in the frontend **Reliability** tab (which shows a
  durable/in-memory storage badge). Command-execution and OS-notification
  histories are bounded (`_MAX_HISTORY = 200`) so long sessions never grow
  without limit.
- **Proactive/autonomous behaviour**: an event **monitor** polls built-in
  sources (CPU/memory usage, unread-notification count, stored **facts**, recent
  **conversation activity**, plus **scoped** per-category/per-project/
  per-conversation variants and **cross-project aggregates** — total projects,
  new projects, message volume, distinct active projects in 24h) and evaluates
  **triggers** with cooldown + re-arm;
  a **scheduler** runs one-shot and periodic tasks; both share one runtime
  started with the app (`BERU_PROACTIVE_ENABLED`, default on). Tasks and
  triggers are **persisted to the database** (create/pause/resume/cancel/
  delete and execution state are write-through) and **restored automatically on
  startup**, so scheduled work survives restarts; every run and firing is
  recorded in an append-only **audit history** with **summaries** (including
  per-failure error **grouping**), **pruning** (per-object by age/count, plus
  an optional startup retention sweep), and **export/restore** (idempotent
  JSON backup). A task or
  trigger can create a **notification** (persisted to the inbox and pushed to
  connected clients over the WebSocket channel) or launch a real **agent
  turn** — sensitive tools stay gated behind the same confirmation flow as
  chat. Managed over the key-protected `/api/v1/scheduler/*` and
  `/api/v1/monitor/*` endpoints.
- **Rate limiting**: per-IP sliding-window limiter on chat endpoints (configurable).
- **Request size limits**: body size cap and message length validation.
- **Retry/backoff**: exponential backoff with jitter on transient LLM failures
  (429, 5xx, network errors); respects `Retry-After` headers.
- **CI**: GitHub Actions running ruff (lint + format check) and pytest across
  Python 3.10–3.12 on every push/PR.
- **Tests**: 840 tests covering config, LLM layer, agents/tools, real tool
  execution + confirmation flow, memory, streaming, auth, rate limits, retries,
  database migrations, the full chat flow, the proactive runtime (scheduler,
  event monitor, notifications, agent-turn actions, persistence + restart
  restore, fact/conversation trigger sources incl. scoped variants and
  cross-project aggregates, run/fire audit history + summaries + pruning/
  retention + error grouping + export/restore), the real Playwright browser
  engine (mocked for hermetic runs), the browser/desktop API routers, and the
  reliability surface (activity-ledger bounds/summary/clear, API endpoints,
  tool-call + confirm/deny integration, bounded command/notification
  histories, and DB persistence + restart restore + prune).

---

## Requirements

- Python **3.10+**
- The dependencies in [`requirements.txt`](requirements.txt) (FastAPI, Uvicorn,
  Pydantic v2, SQLAlchemy 2.0, aiosqlite, Alembic, httpx, Playwright).
  Browser automation additionally needs the Chromium runtime:
  `playwright install chromium`.

---

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 2b. (Optional) Chromium runtime for browser automation
playwright install chromium

# 3. Create your local config (safe offline defaults; no API key needed)
cp .env.example .env

# 4. Run the server
python -m backend.main
#   ...or:  uvicorn backend.main:app --reload
```

Then open the interactive API docs at **http://127.0.0.1:8000/docs**.

> **Deployment**: see [`docs/deployment.md`](docs/deployment.md) for production
> env/bind handling, TLS/reverse-proxy notes, `/health` vs `/ready` probes,
> graceful-shutdown behaviour, and Docker/`run.sh` usage.

Out of the box BERU uses the **mock** LLM provider, so it runs fully offline and
echoes a deterministic response. Configure a real provider (below) for genuine
reasoning.

### Try it

```bash
# Health check
curl http://127.0.0.1:8000/health

# Send a message (starts a new conversation)
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello BERU"}'

# Continue the conversation (use the conversation_id from the previous reply)
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What did I just say?", "conversation_id": "<id>"}'

# Stream the reply token-by-token (Server-Sent Events)
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "Tell me about yourself"}'

# Retrieve history
curl http://127.0.0.1:8000/api/v1/conversations/<id>

# Long-term memory: add a fact
curl -X POST http://127.0.0.1:8000/api/v1/facts \
  -H "Content-Type: application/json" \
  -d '{"key": "name", "value": "Alex", "category": "identity"}'

# List all facts
curl http://127.0.0.1:8000/api/v1/facts

# Forget a fact
curl -X DELETE http://127.0.0.1:8000/api/v1/facts/name

# Create a project for scoped memory
curl -X POST http://127.0.0.1:8000/api/v1/projects \
  -H "Content-Type: application/json" \
  -d '{"name": "beru-dev", "description": "BERU development project"}'

# List projects
curl http://127.0.0.1:8000/api/v1/projects

# Create a browser page (Playwright; requires the browser APIs to be keyed)
curl -X POST http://127.0.0.1:8000/api/v1/browser/pages \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key>" \
  -d '{"url": "https://example.com"}'

# Query the desktop status (key-protected)
curl http://127.0.0.1:8000/api/v1/desktop/status \
  -H "X-API-Key: <key>"
```

---

## Configuration

All configuration comes from environment variables (optionally via `.env`).
Secrets are **never** hardcoded. See [`.env.example`](.env.example) for the full,
commented list. Key variables:

| Variable          | Default                          | Purpose                                              |
| ----------------- | -------------------------------- | ---------------------------------------------------- |
| `LLM_PROVIDER`    | `mock`                           | `mock` or `openai_compatible`.                       |
| `LLM_BASE_URL`    | `https://api.openai.com/v1`      | Base URL for the OpenAI-compatible endpoint.         |
| `LLM_API_KEY`     | *(empty)*                        | API key/token (blank for local servers).             |
| `LLM_MODEL`       | `gpt-4o-mini`                    | Model identifier.                                    |
| `DATABASE_URL`    | `sqlite+aiosqlite:///./beru.db`  | Async SQLAlchemy database URL.                       |
| `MEMORY_WINDOW_SIZE` | `20`                          | Recent messages included as short-term memory.       |
| `BERU_MEMORY_STRATEGY` | `window`                    | `window` \| `summarise` \| `semantic` memory.        |
| `BERU_EMBEDDING_PROVIDER` | `mock`                  | Embedding provider for semantic memory.              |
| `BERU_PROACTIVE_ENABLED` | `true`                     | Start the scheduler + monitor loops with the app.   |
| `BERU_RATE_LIMIT_CHAT_RPM` | `30`                   | Max chat requests per minute per IP (`0` = unlimited). |
| `BERU_RATE_LIMIT_BURST` | `5`                       | Consecutive requests before the limiter kicks in.    |
| `BERU_MAX_TOOL_ITERATIONS` | `10`                  | Max tool-call iterations per request.                |
| `BERU_API_KEY`    | *(empty)*                        | Single-user API key; blank disables auth (localhost).|
| `BERU_VOICE_STT_PROVIDER` / `BERU_VOICE_TTS_PROVIDER` | `mock` | Voice STT/TTS provider behind the abstraction. |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `text`         | Logging verbosity and `text`/`json` output.          |
| `CORS_ORIGINS`    | `*`                              | Comma-separated allowed origins.                     |

### Using a real LLM

Set these in `.env` (examples):

```ini
# OpenAI
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini

# Groq
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.3-70b-versatile

# Ollama (local, no key)
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.1
```

---

## Database migrations

The schema is managed by **Alembic** (in [`migrations/`](migrations/)), not by
`create_all`. **You normally don't need to run anything** — BERU applies any
pending migrations automatically on startup, and a database created by an
earlier version is adopted (stamped) rather than rebuilt.

For manual control (e.g. deployments) or when changing the models:

```bash
# Apply all migrations (what startup does for you)
alembic upgrade head

# After editing an ORM model, generate a migration from the diff
alembic revision --autogenerate -m "describe the change"
#   ...then review the generated file in migrations/versions/ before committing.

# Roll back the most recent migration
alembic downgrade -1
```

Alembic reads the same `DATABASE_URL` as the app (converted to a synchronous
driver), so there is a single source of truth and no separate configuration.

> Upgrading from a pre-migration checkout? Your existing `beru.db` is adopted
> automatically on the next start. To start clean instead, just delete `beru.db`.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

The suite is hermetic: each test uses a fresh temporary SQLite database and the
deterministic mock provider, so no network access or API keys are required.

---

## Project structure

```text
BERU/
├── backend/
│   ├── api/            # FastAPI routers (chat, browser, desktop, scheduler,
│   │                   #   monitor, reliability, voice, sync, plans, facts,
│   │                   #   system…)
│   ├── core/           # config, logging, error handling
│   ├── database/       # async engine, session, Alembic init
│   ├── models/         # SQLAlchemy ORM models
│   ├── schemas/        # Pydantic request/response contracts
│   ├── services/       # chat orchestration, activity ledger + proactive
│   │                   #   runtime (store, service)
│   ├── engines/        # LLM, intelligence, memory, scheduler, monitor,
│   │                   #   notifications, browser (Playwright), desktop (mouse/
│   │                   #   keyboard/clipboard/screenshot/apps/windows/hotkeys),
│   │                   #   voice, command
│   ├── agents/         # agent interface, registry, BERU Core + IGRIS/DHANUS/TANK
│   ├── tools/          # tool interface + registry (66 tools across the agents)
│   ├── memory/         # short-term conversation memory
│   └── main.py         # application factory + entrypoint
├── migrations/         # Alembic migration environment + versions
├── tests/              # pytest suite (840 hermetic tests)
├── docs/               # architecture, roadmap, decisions
├── frontend/           # single-page UI (index.html + app.js)
├── alembic.ini         # Alembic config (DB URL resolved from settings)
├── requirements*.txt
├── pyproject.toml
└── .env.example
```

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — layers, data flow, extension points.
- [`docs/roadmap.md`](docs/roadmap.md) — what's next and in what order.
- [`docs/decisions.md`](docs/decisions.md) — key engineering decisions and rationale.
- [`docs/observability.md`](docs/observability.md) — `/metrics` endpoint and the structured log-format contract.
- [`docs/backup.md`](docs/backup.md) — SQLite snapshot/restore for the whole database.
- [`docs/release.md`](docs/release.md) — release checklist: version bump, lockfiles, and the pre-release gate.

---

## Security notes

- Secrets come from the environment only; `.env` is git-ignored.
- Tools declare explicit permissions; agents execute them during a turn, and
  confirmation-required tools (command execution, app launching, notifications,
  browser navigation/close/launch/clear-downloads)
  always return a `confirmation_required` result that the client must approve —
  they are never auto-confirmed.
- Local endpoints refuse to bind without a key; the proactive scheduler/monitor
  and the browser/desktop/voice APIs are key-protected.
- Destructive/sensitive actions stay gated behind the confirmation flow.
  Prompt-injection and malicious tool instructions are treated as security
  threats by design.

---

## License

Not yet specified. Add a `LICENSE` file before any public distribution.
