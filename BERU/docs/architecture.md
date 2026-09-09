# BERU Architecture (v0.1.0)

This document describes the foundation architecture. It reflects what is
**actually implemented** in this repository, plus the seams designed for future
growth.

## Goals

- A clean, modular backend that is easy to extend without rewrites.
- LLM providers and external services replaceable behind narrow interfaces.
- Correctness and security first; scalability designed in from the start.
- Every subsystem (intelligence, agents, tools, memory) has a real, minimal
  implementation — no fake functionality presented as production behaviour.

## Layered overview

BERU is organised as layers with a strict dependency direction (upper layers
depend on lower layers, never the reverse):

```text
        HTTP clients / future UI / mobile
                     │
┌────────────────────┼─────────────────────────────────────────┐
│  api/           FastAPI routers + dependencies (thin)          │
├────────────────────┼─────────────────────────────────────────┤
│  services/      ChatService, ConversationService (DB-aware)    │
├──────────┬─────────┴───────────────┬───────────────────────────┤
│ engines/ │ IntelligenceEngine      │ memory/  ConversationMemory│
│          │ (agent orchestration)   │ (short-term window)        │
│          ├─────────────────────────┤                            │
│ agents/  │ BaseAgent, CoreAgent,   │                            │
│          │ AgentRegistry           │                            │
│          ├─────────────────────────┤                            │
│ engines/llm/  LLMProvider (mock, openai_compatible), registry   │
├─────────────────────────────────────────────────────────────── ┤
│  models/ + database/   SQLAlchemy 2.0 async ORM + SQLite        │
├─────────────────────────────────────────────────────────────── ┤
│  core/          config, logging, errors (cross-cutting)         │
└──────────────────────────────────────────────────────────────── ┘
```

`schemas/` (Pydantic) defines the API contract and sits beside the API layer;
it is deliberately separate from `models/` (ORM) so the wire format and the
database schema can evolve independently.

## Request lifecycle: `POST /api/v1/chat`

1. **Router** (`api/routers/chat.py`) validates the body against `ChatRequest`
   and resolves dependencies: a DB session, settings, and a `ChatService`.
2. **ChatService** (`services/chat_service.py`) runs one turn atomically:
   - resolve or create the `Conversation`;
   - assemble prior context via **ConversationMemory** (the recent-message
     window) *before* persisting the new message;
   - persist the **user** message;
   - call the **IntelligenceEngine** to generate a reply;
   - persist the **assistant** message;
   - `commit()` once, so the turn is all-or-nothing.
3. **IntelligenceEngine** (`engines/intelligence.py`) resolves the target agent
   from the **AgentRegistry** and delegates generation.
4. **Agent** (`agents/base.py`) builds the prompt — system prompt + history +
   current message — and calls the configured **LLMProvider**.
5. **LLMProvider** (`engines/llm/*`) returns a provider-neutral `LLMResponse`.
6. The router maps the persisted assistant message to `ChatResponse`.

This keeps the **engine and agents database-agnostic** (pure reasoning) while
the **service layer** owns persistence and memory assembly.

## Streaming lifecycle: `POST /api/v1/chat/stream`

Streaming reuses the same layers, adding a parallel path that never disturbs the
buffered one:

- Each provider implements `stream_chat(...)` yielding `LLMStreamChunk`s; the ABC
  ships a default that degrades to a single `chat()` call, so any provider
  streams through the same interface. The agent re-emits these as
  `AgentStreamChunk`s (tagged with the agent name) via `BaseAgent.stream(...)`.
- `ChatService.stream_process(...)` resolves the conversation and **validates the
  agent before the first byte**, then emits typed `ChatStreamEvent`s: one
  `start`, many `delta`s, and a final `end` (or `error`). **No database session
  is ever held across the LLM stream**: a short-lived session creates/resolves
  the conversation and commits the user message before the first token, the
  tokens stream with no session open, and a second short-lived session persists
  the assistant message when the stream finishes (so a request is never lost to
  an interrupted stream). A mid-stream failure emits `error` with no partial
  assistant text written; the committed user message is retained. Tests swap the
  session factory via `set_stream_session_factory` so stream tests never touch
  the app database.
- The router pulls the `start` event eagerly, so an unknown conversation or agent
  raises **before** the response begins and returns a normal JSON 404. The
  remaining events are serialised as Server-Sent Events
  (`event: <kind>\ndata: <json>\n\n`) with `Cache-Control: no-cache` and
  `X-Accel-Buffering: no`.

## Data model

String (UUID) primary keys, timezone-aware timestamps:

- **conversations**: `id`, `title?`, `agent`, `created_at`, `updated_at`.
- **messages**: `id`, `conversation_id` (FK, indexed, cascade delete), `role`
  (`user`/`assistant`/`system`), `content`, `model?`, `token_count?`,
  `created_at`, `updated_at`.
- **notifications**: `id`, `title`, `message`, `level`, `channel`, `agent?`,
  `payload` (JSON), `read`, `created_at` — the persisted inbox behind the
  proactive layer (added by migration `a3b5f7c9e1d2`).
- **activity_records** / **audit_records**: the durable reliability ledger
  (migration `d4e5f6a7b8c9`) — one row per tool-call `ActivityEntry` /
  approval `AuditEntry`, keyed by a UUID `id` with a monotonic indexed `seq`
  ordering column (see "Reliability & observability").
- **task_runs** / **trigger_fires**: append-only history for the scheduler and
  monitor (migrations `c1d2e3f4a5b6` + `b5c6d7e8f9a1`) — each run/fire carries
  the same monotonic indexed `seq` counter so "newest first" ordering stays
  deterministic even when rows share a `run_at`/`fired_at` timestamp (the
  previous tie-breaker was a random UUID `id`).

A conversation has many messages (ordered by `created_at`); deleting a
conversation cascades to its messages via the ORM relationship.

### Schema management (Alembic)

The schema is owned by **Alembic migrations** in `migrations/`, not by
`create_all`. A single helper, `backend/database/migrations.py`, builds the
Alembic config from the app's `DATABASE_URL` (converted to a **sync** driver via
`to_sync_url`) and is used by both the `alembic` CLI and the application:

- **Startup** (`init_db`) runs `run_migrations()` in a worker thread
  (`asyncio.to_thread`), bringing the database to `head`. A database created by
  the old `create_all` path — tables present but no `alembic_version` — is
  **adopted by stamping it to head** rather than rebuilt, so upgrades are
  seamless.
- **`migrations/env.py`** targets `Base.metadata` (all models imported for
  registration), so `alembic revision --autogenerate -m "..."` produces
  accurate diffs. `render_as_batch=True` keeps SQLite `ALTER`s working.
- The migration URL is always resolved from settings (or an explicit override
  used by tests) — never hardcoded.

### Database connections (SQLite)

- `backend/database/base.py` `get_engine` forces `check_same_thread=False` on
  SQLite and applies the `foreign_keys=ON`, `busy_timeout=5000`, and WAL pragmas
  on every new connection, so `STATIC_BUSY_TIMEOUT` protects multi-writer
  turns instead of raising `database is locked`. No explicit pool size is set —
  aiosqlite resolves `NullPool`, so each short-lived session owns its own
  connection and no long-running stream ever pins the pool.

## The LLM abstraction (replaceability)

Everything above `engines/llm` speaks only in `LLMMessage` / `LLMResponse` and
the `LLMProvider` interface — never a vendor SDK. Providers are selected purely
by configuration and cached as a process singleton by the registry.

- `MockProvider` — deterministic, offline, no key. The default, so the app and
  tests run with zero setup.
- `OpenAICompatibleProvider` — one adapter for every OpenAI-compatible
  `/chat/completions` endpoint (OpenAI, Groq, OpenRouter, Ollama, LM Studio, …).

## Extension points

**Add an LLM provider** — implement `LLMProvider.chat(...)`, then register it in
`engines/llm/registry.build_provider`. No caller changes.

**Add an agent** — subclass `BaseAgent`, set `name`/`description`/
`capabilities`/`default_system_prompt`, and register it in
`agents/registry.get_agent_registry`. Callers select it via the `agent` field on
a chat request. Agents needing tools or multi-step planning override `generate`.

**Add a tool** — subclass `Tool`, declare `permissions` and `parameters`,
implement async `run(...)`, and register it in `tools/registry`. Tools are
discoverable via `GET /api/v1/tools`. Agents execute registered tools during a
turn (see `BaseAgent.run_tool`); tools flagged with `requires_confirmation`
gate execution behind a `PendingConfirmation` — the client approves via
`POST /api/v1/chat/confirm` before the tool actually runs. Tool results are
serialised back into the conversation so the agent reasons over real output.

**Add a memory strategy** — implement `BaseMemory.build_context(...)` (e.g.
summarisation or semantic recall) and use it in `ChatService`.

## Proactive / autonomous subsystem

BERU can act on its own schedule and react to observed conditions, through three
engines coordinated by one shared runtime:

```text
    app lifespan (BERU_PROACTIVE_ENABLED)
                 │ start / stop
   services/proactive_service.py      (singletons: get_scheduler,
     │  session-factory indirection,     get_event_monitor,
     │  source samplers, dispatch)       get_notification_service)
     ├──────────┐         ┌──────────────┘
     ▼          ▼         ▼
 scheduler.py  monitor.py  notifications.py
 (tasks, one-  (sources +  (DB inbox + WS
  shot/interval, triggers → broadcast via
  pause/resume/ actions)    ws_manager)
  manual run)
```

- **Scheduler** (`engines/scheduler.py`): in-memory `ScheduledTask`s
  (`notify` or `agent_turn` handlers, one-shot/interval/cron-style `run_at`,
  `max_runs`). A background loop ticks every second; each task can be run,
  paused, resumed, cancelled, or deleted through the engine or the API.
- **Event monitor** (`engines/monitor.py`): registers **sources** whose current
  value comes from a sampler — CPU usage (`os.times` deltas), memory percent
  (Windows `GlobalMemoryStatusEx` via ctypes, else unavailable), unread
  notification count, stored **facts** (`memory.facts_count`,
  `memory.facts_text` for pattern matching), and recent **conversation
  activity** (`conversations.new_24h`, `conversations.messages_24h`) — the
  DB-backed ones fetch through the swappable session factory — plus arbitrary
  values pushed over the API. **Scoped (parameterised) sources** let a trigger
  watch one dimension: `register_scoped_source(name, fetcher(scope))` is
  referenced as `name:scope`, e.g. `memory.facts_count:work`,
  `memory.facts_project_count:<id>`, `memory.facts_project_text:<id>`,
  `conversations.messages_24h:<conversation_id>`,
  `conversations.new_24h:<project_id>`, and
  `conversations.project_messages_24h:<project_id>`. The monitor resolves the
  base name from each active trigger's source string and fetches scoped values
  on every check; values pushed for unrecognized sources are never clobbered.
  **Cross-project aggregate sources** (`projects.count`, `projects.new_24h`,
  `projects.messages_24h`, `projects.active_24h`) summarize reactor-wide
  state — total/new projects, message volume across project conversations
  (unaffiliated conversations are excluded), and the distinct projects with any
  message in the window.
  **Triggers** evaluate a condition against the refreshed values each check and
  fire configured **actions**; `cooldown_seconds` suppresses repeats, and zero
  cooldown re-arms immediately after firing.
- **Notifications**: `NotificationService` persists a `NotificationRecord`
  (commit happens at the caller) and broadcasts a `{"type":"notification",...}`
  message over the shared WebSocket manager. Read/unread is tracked; the monitor
  exposes `notifications.unread` as a source so other triggers can react to
  audible/inbox state.
- **Actions & safety**: the monitor dispatches `notify`, `agent_turn`, or a
  custom callable. `agent_turn` runs a real turn through `ChatService.process`
  with its own conversation and the chosen agent; the mock provider is the safe
  default, and any tool that `requires_confirmation` still surfaces as a
  `PendingConfirmation` *alert* rather than executing — autonomous runs inherit
  the chat confirmation gate.
- **Runtime wiring** (`services/proactive_service.py`): the app lifespan starts
  and stops both loops. The session factory is swappable (`set_session_factory`)
  so tests can point sources at a temp DB without touching global settings; the
  process singletons are shared by the routers' module-level references, so
  API state and background state are the same.
- **Persistence** (`services/proactive_store.py`): scheduled tasks and monitor
  triggers are **write-through persisted** to the `scheduled_tasks` /
  `monitor_triggers` tables (migration `b1e2f3a4c5d6`): the API routers save
  after every create/update/delete, and optional in-engine **state hooks**
  (`set_state_hook`) persist execution state (run_count/status/last_run/fired
  counts) after each run/check. `start_proactive_runtime()` restores tasks and
  triggers before starting the loops, so autonomous work survives restarts.
  Store reads normalize SQLite datetimes back to aware UTC. Direct engine
  mutations made without going through the API or a state hook are the
  documented boundary and are not persisted.
- **Audit history** (`task_runs`, `trigger_fires`, migration `c1d2e3f4a5b6`):
  append-only logs written by dedicated engine hooks — the scheduler **run
  hook** records every execution (status, `duration_ms`, error, post-run
  count) and the monitor **fire hook** records every firing (condition snapshot
  + the source value that satisfied it). Exposed as
  `GET /api/v1/scheduler/tasks/{id}/runs` and `GET /api/v1/monitor/triggers/{id}/fires`
  (newest first, `limit` capped at 500) with a real `total` and a **summary**
  (for runs: outcome counts + `success_rate` + duration stats + first/last +
  per-failure **error groups**: exact error text grouped with count + last
  occurrence, sorted by frequency; for fires: first/last). Manual **pruning**
  per task/trigger (`POST .../prune-runs`, `POST .../prune-fires` with
  `retention_seconds` and/or `keep_last`) and an optional startup retention
  sweep (`prune_old_history()`, driven by `BERU_PROACTIVE_AUDIT_RETENTION_DAYS`,
  default 0 = off) keep the logs bounded. The logs can also be **backed up and
  restored** with `GET/POST /api/v1/scheduler/history/export|import` (and the
  monitor equivalents): exports serialize every row with its stable `id`, and
  imports are idempotent — rows whose id already exists are skipped unless
  `replace` is set, so a backup can be applied repeatedly without
  duplicating history.

## Browser & desktop engines

Both capabilities follow the same pattern as the proactive layer: a real engine
with an availability probe, tools that reflect that state, key-protected API
routers, and frontend panels.

- **Browser** (`engines/browser.py`): a **Playwright** (headless Chromium)
  engine. `get_browser_engine()` is an `lru_cache` singleton; the first
  `ensure_ready()` probe lazily launches the browser and marks the engine
  `available`, or degrades to `unavailable` when Playwright/Chromium is missing
  so the app still runs. It manages named `Page`s (create/activate/close, a
  current active page) and executes actions (navigate, click, type, select
  option by value/label/index or a `values` list, drag & drop, session cookies
  get/set/clear, localStorage get/set/clear, wheel scroll, scroll-to-selector,
  press-key, extract text/HTML, structured page inspection in a single
  `evaluate` — bounded visible text, links, interactive elements with ready
  selectors, plus a frame map — viewport/full-page screenshot, back/forward,
  refresh, whole-session `close_browser`). Click/type/press/select/extract/
  scroll-to accept a **frame target** (name, URL substring, or index). A
  context-wired `response` tap keeps a bounded per-page network log (read via
  `network`); each entry carries URL, HTTP status, response size, and request
  timing. A URL-glob router blocks matching requests (`block_url`), fulfils
  302 redirect rewrites (`redirect_url`), or answers with fixed content —
  status, body, content-type, and extra headers — for API mocking (`fulfill`),
  popup/child tabs are
  auto-registered as pages, a per-page **console/page-error tap** records
  bounded `console` entries (with source location when available), downloads
  save to a local directory (read via `downloads`, emptied via the
  confirm-gated `clear_downloads`),
  file inputs accept paths
  (`upload`), and `snapshot`/`restore`
  persist cookies + localStorage to named JSON files in a session dir, with
  `restore` relaunching a fresh context from that storage state. A
  confirm-gated `launch` action closes and relaunches the browser with new
  options — headless/headed toggle, viewport, user agent, locale, device-scale
  factor, mobile/touch emulation, timezone — stored in
  `_launch_opts`/`_context_opts` and honoured by `ensure_ready`; screenshot/
  download/session directories are overridable via
  `BERU_BROWSER_SCREENSHOT_DIR`/`BERU_BROWSER_DOWNLOADS_DIR`/`BERU_BROWSER_SESSION_DIR`.
  Session-scoped actions (launch, restore) skip the active-page requirement.
  `status_info()`
  powers a rich `/status` (mode, page count, blocked globs, redirects, fulfil
  count, device options, started time, download/session dirs). Twenty-eight
  tools in `tools/browser.py` (navigate, close, launch, and
  clear-downloads `requires_confirmation=True`); the
  `api/routers/browser.py` surface (`/status`, `/pages*`, `/action`, `/close`)
  returns consistent `ok` envelopes.
- **Desktop** (`engines/desktop_platform.py` + `input.py`, `clipboard.py`,
  `screenshot.py`, `apps.py`, `windows.py`, `hotkeys.py`): native platform
  engines with availability probes (Windows/macOS/Linux). `windows.py` drives
  top-level window enumeration/focus/graceful-close and multi-monitor geometry
  via `ctypes`+`user32`; `hotkeys.py` persists durable OS-wide hotkeys to
  `data/hotkeys.json` and re-binds them on startup from a background
  `RegisterHotKey` + `PeekMessage` listener thread. Exposed as 21 tools in
  `tools/desktop.py` and a 23-endpoint `api/routers/desktop.py` (including an
  SSE `GET /windows/stream` live window snapshot feed). All confirm-capable,
  sensitive operations inherit the same confirmation gate as chat tools.
- **Frontend panels** (`frontend/`): the Browser/Desktop/Tools/Memory/Plans/
  Notify/Automation/Reliability/Settings tabs call these routers through
  `apiFetch()`; availability badges, live page and desktop-status views, full
  task/trigger management (create, control, edit, history, prune), a fact/
  project manager, a goal → plan viewer with step execution, a notification
  inbox, and the reliability ledger (activity + audit, live summary,
  durable/in-memory storage badge, clear control) are rendered in `app.js`.

## Cross-cutting concerns

- **Configuration** (`core/config.py`): a single cached Pydantic `Settings`
  object sourced from env/`.env`. No secret is ever hardcoded.
- **Logging** (`core/logging.py`): stdlib logging with a per-request
  `request_id` correlation id and `text`/`json` formats.
- **Errors** (`core/errors.py`): all app errors derive from `BeruError` (status
  code + stable `error_type`); handlers emit a consistent
  `{"error": {"type", "message", "detail?"}}` envelope.

### Reliability & observability (durable ledger)

A lightweight, always-on observability layer built on a process singleton
(`services/activity_ledger.py`) with an optional DB-backed durability path:

- `ActivityLedger` keeps two **bounded ring buffers** — `_ACTIVITY_MAX = 500`
  tool-call entries and `_AUDIT_MAX = 200` approval entries — so long sessions
  stay bounded and the newest activity is always the most recent. Entries carry
  `timestamp`, the per-request correlation id (`request_id_ctx`), the agent and
  tool name, a 200-char argument summary (`args_summary`), outcome (one of
  `success` / `failure` / `permission_denied` / `confirmation_required`),
  `duration_ms`, and a 200-char error summary (`error_summary`);
  `truncate`/`args_summary`/`error_summary` cap long values.
- **Write points**: `BaseAgent.run_tool()` records an `ActivityEntry` for every
  resolution — unknown tool, permission-denied, confirmation-required,
  invalid-JSON, and genuine execution (success or failure, with duration);
  `ChatService.confirm_tool_call()` records an `approved` `AuditEntry` and
  `deny_tool_call()` a `denied` one.
- **Durability**: when a session factory is installed via
  `set_ledger_session_factory(...)`, every record is also written best-effort
  to the `activity_records` / `audit_records` tables (migration
  `d4e5f6a7b8c9`) as a fire-and-forget background task — recording never blocks
  tool execution, and a persistence failure never crashes the caller. The
  background queue is **bounded** (`_MAX_PENDING_PERSISTS = 256`): under extreme
  burst it drops new persistence work (with a warning) rather than growing
  unbounded memory, and the in-memory record still succeeds.
  `restore_activity_ledger()` (called on startup) reads the newest rows back
  into the in-memory buffers, ordered by a monotonic `seq` column assigned in
  Python, **prunes any rows beyond the bounded window** (logging how many were
  pruned) so the tables stay capped. Each table is keyed by a UUID `id` with
  `TimestampMixin` and an indexed `seq` ordering column; migrations
  `1a2b3c4d5e6f` / `b5c6d7e8f9a1` add `seq >= 0` check constraints to the
  ledger and history tables.
- **Read points**: the key-protected `api/routers/reliability.py` exposes
  `GET /api/v1/reliability/activity`, `GET /api/v1/reliability/audit` (both
  `limit`-capped list), `GET /api/v1/reliability/summary` (totals, outcome
  counts, approval/denial counts, top tools, and a `persisted` flag), and
  `DELETE /api/v1/reliability/activity` to clear the in-memory ledger and purge
  its persisted rows. `GET /api/v1/status` embeds the same summary under a
  `reliability` field (`StatusResponse.reliability: dict | None`).
- **Wiring**: `backend/main.py` lifespan installs the app's DB session factory
  on the ledger and calls `restore_activity_ledger()` after `init_db()`, so
  observability survives restarts the same way scheduled tasks/triggers do.
- **Bounded histories**: `CommandExecutor` and `OSNotifier` cap their in-memory
  history at `_MAX_HISTORY = 200` (append helper trims to the last 200), so the
  command and notification logs used by the system tools cannot grow unbounded.
- **Testability**: `conftest.py` resets the ledger (and its session factory)
  after every test, the API router resolves the ledger via
  `get_activity_ledger()` at request time rather than at import, and the HTTP
  `client` fixture points the ledger's factory at the test's temp DB. Tests
  await in-flight writes with `ActivityLedger.flush()` for deterministic
  assertions.

## API surface (v1)

| Method | Path                                    | Purpose                       |
| ------ | --------------------------------------- | ----------------------------- |
| GET    | `/health`                               | Liveness probe                |
| GET    | `/`                                     | Service root / links          |
| POST   | `/api/v1/chat`                          | Send a message, get a reply   |
| POST   | `/api/v1/chat/stream`                   | Stream a reply via SSE        |
| POST   | `/api/v1/chat/confirm`                  | Approve a pending tool call   |
| POST   | `/api/v1/chat/deny`                     | Deny a pending tool call      |
| GET    | `/api/v1/chat/confirmations`            | List pending confirmations    |
| GET    | `/api/v1/conversations`                 | List conversations            |
| GET    | `/api/v1/conversations/{id}`            | Conversation with messages    |
| GET    | `/api/v1/conversations/{id}/messages`   | Messages only                 |
| DELETE | `/api/v1/conversations/{id}`            | Delete a conversation         |
| GET    | `/api/v1/status`                        | Runtime status                |
| GET    | `/api/v1/agents`                        | Discover agents               |
| GET    | `/api/v1/tools`                         | Discover tools                |
| GET    | `/api/v1/llm`                           | Current LLM configuration     |
| POST   | `/api/v1/llm/test`                      | Run a one-shot connectivity test |
| POST   | `/api/v1/facts`                         | Create/update a fact          |
| GET    | `/api/v1/facts`                         | List facts (optional `?category=`) |
| GET    | `/api/v1/facts/{key}`                   | Get a single fact             |
| DELETE | `/api/v1/facts/{key}`                   | Delete (forget) a fact        |
| POST   | `/api/v1/projects`                      | Create a project              |
| GET    | `/api/v1/projects`                      | List projects                 |
| GET    | `/api/v1/projects/{id}`                 | Get a project                 |
| PATCH  | `/api/v1/projects/{id}`                 | Update a project              |
| DELETE | `/api/v1/projects/{id}`                 | Delete a project              |
| POST   | `/api/v1/plans`                         | Create a plan (auto-decompose) |
| POST   | `/api/v1/plans/manual`                  | Create a plan from explicit steps |
| GET    | `/api/v1/plans` / `/api/v1/plans/active`| List (all / active) plans     |
| GET    | `/api/v1/plans/{id}`                    | Get a plan                    |
| POST   | `/api/v1/plans/{id}/steps/{sid}/start\|complete\|fail\|skip` | Drive a step |
| POST   | `/api/v1/plans/{id}/cancel` / `DELETE`  | Cancel / delete a plan        |
| GET    | `/api/v1/browser/status`                | Browser engine availability + active page |
| *      | `/api/v1/browser/pages*`                | Create / list / activate / close pages |
| POST   | `/api/v1/browser/action`                | Run a browser action (navigate/click/type/extract/…) |
| POST   | `/api/v1/browser/close`                 | Close the whole browser session |
| *      | `/api/v1/desktop/*`                     | Desktop control: status, screenshot, mouse (position/screen-size/move/click/double-click/scroll), keyboard (type/press/hotkey), clipboard (read/write), processes (list/find), windows (list/focus/close/stream), monitors (list), hotkeys (list/register/remove) |
| *      | `/api/v1/system-control/*`              | Command execution + history + clear, app launch/aliases, OS-notification history |
| *      | `/api/v1/voice/*`                       | Voice engine: sessions, listen/transcribe, TTS respond/audio, status |
| *      | `/api/v1/sync/*`                        | Phone/laptop sync: devices, deltas, messages, mailbox + ack |
| POST   | `/api/v1/auth/login` / `/api/v1/auth/logout` | API-key session login / logout |
| GET    | `/api/v1/auth/status`                   | Session status                 |
| *      | `/api/v1/scheduler/tasks*`              | Scheduled-task CRUD + run/pause/resume/cancel + run history/prune |
| *      | `/api/v1/scheduler/history*`          | Task run history export/import (JSON backup/restore) |
| *      | `/api/v1/scheduler/notifications*`      | Notification inbox (list/unread-count/read/read-all/delete/clear) |
| GET    | `/api/v1/monitor/status`                | Monitor runtime + sources     |
| *      | `/api/v1/monitor/sources*`              | List sources / push a value   |
| *      | `/api/v1/monitor/triggers*`             | Trigger CRUD + pause/resume/disable + fire history/prune |
| *      | `/api/v1/monitor/history*`             | Trigger fire history export/import (JSON backup/restore) |
| POST   | `/api/v1/monitor/tick`                  | Refresh sources + evaluate once |
| POST   | `/api/v1/monitor/start` / `/stop`       | Start/stop the monitor loop   |
| GET    | `/api/v1/reliability/summary`           | Reliability summary (tool-call totals, approval/denial counts) |
| GET    | `/api/v1/reliability/activity`          | Recent tool-call activity (bounded ledger) |
| GET    | `/api/v1/reliability/audit`             | Approval/denial audit trail   |
| DELETE | `/api/v1/reliability/activity`          | Clear the ledger (in-memory + persisted rows) |
| WS     | `/ws/{client_id}`                       | Live push (chat stream, notifications) + voice `ws` streams |

Every `/api/v1/*` router except the unversioned `/health`, `/`, and static
frontend serves carries `dependencies=[Depends(require_api_key)]`; the chat
router also applies the chat rate limiter. The auth router provides the
`X-API-Key`-driven session flow used by `apiFetch()` via the login/logout/
status endpoints.

## Testing approach

`pytest` + `pytest-asyncio` with `httpx.ASGITransport`. Each test gets a fresh
temp SQLite DB (via a dependency override) and the mock provider is forced, so
the suite is fully hermetic and deterministic. Test databases are built directly
from `Base.metadata` (fast, and the app lifespan is not run), while a dedicated
`tests/test_migrations.py` separately proves the Alembic migration produces
exactly that same schema and round-trips cleanly.

The proactive layer is also hermetic: the suite forces
`BERU_PROACTIVE_ENABLED=false`, `conftest.py` calls
`reset_proactive_runtime()` after every test,
`proactive_service.set_session_factory(...)` points the monitor's DB source at
a temp engine, and `tests/test_proactive_wiring.py` drives the same
`start/stop_proactive_runtime()` calls the lifespan makes (running the full
FastAPI lifespan under TestClient is avoided on Windows — it tripped a
pre-existing exit-time access violation in anyio's portal join + aiosqlite).

The reliability ledger is isolated per test the same way: `conftest.py` resets
the ledger singleton (and its DB session factory) after every test so a test
never observes another test's recorded activity or touches a prior test's
database. The HTTP `client` fixture points the ledger's session factory at the
test's temp DB, and persistence tests use `ActivityLedger.flush()` /
`restore_activity_ledger()` to make durable-write and restart-restore
assertions deterministic.
