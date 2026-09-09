# BERU Roadmap

This roadmap sequences BERU's growth from the current **v0.1.0 foundation**
toward the long-term vision of a reliable personal AI operating system. It is
deliberately incremental: build the smallest working version, verify, integrate,
document, then extend — never a big-bang rewrite.

Statuses: ✅ done · 🔜 next · 🔲 planned · 💤 deferred (explicitly out of scope for now)

---

## Stage 0 — Foundation ✅ (this release)

The runnable, tested base everything else builds on.

- ✅ FastAPI app factory, lifespan, CORS, request-id middleware.
- ✅ Config via Pydantic settings + `.env`; no hardcoded secrets.
- ✅ Async SQLAlchemy 2.0 + SQLite; conversations & messages persisted.
- ✅ Provider-agnostic LLM layer (mock default + OpenAI-compatible).
- ✅ Intelligence engine + agent registry (BERU Core).
- ✅ Tool interface + registry (example `clock` tool, discoverable).
- ✅ Short-term conversation memory (recent-message window).
- ✅ Consistent error envelope, structured logging.
- ✅ Hermetic pytest suite (30 tests) + docs.

---

## Stage 1 — Harden the core ✅

Make the foundation production-solid before adding surface area.

- ✅ **Streaming responses** (SSE) so replies arrive incrementally;
  `stream_chat()` added to the provider interface with a graceful fallback,
  native streaming in the OpenAI-compatible provider, and a `/chat/stream`
  endpoint. No database session is ever held across the stream: short-lived
  sessions commit the user message before the first token and the assistant
  message after the last (so a turn is preserved even if the stream dies).
- ✅ **Database migrations** with Alembic — `create_all` is replaced by
  versioned migrations. Startup applies them automatically (adopting any
  pre-migration database by stamping it), and `alembic revision --autogenerate`
  is wired to the ORM metadata for future schema changes. See
  [`decisions.md`](decisions.md).
- ✅ **Auth**: a single-user API key/token to protect the API before any
  network exposure.
- ✅ **Rate limiting & request size limits** on the chat endpoint.
- ✅ **Retry/backoff & timeouts** hardening for the OpenAI-compatible provider.
- ✅ **CI**: GitHub Actions running ruff + pytest on every push.

---

## Stage 2 — Real memory ✅

Move from a raw window to intentional, controllable memory.

- ✅ **Conversation summarisation** to compress long histories within the token
  budget (new `BaseMemory` strategy).
- ✅ **Long-term user memory**: durable facts/preferences with explicit
  add/forget controls and an API to inspect them.
- ✅ **Semantic recall**: embeddings + a vector store (start with SQLite-based
  vectors to avoid new infra) behind the same `BaseMemory` interface.
- ✅ **Memory scoping**: per-agent and per-project memory partitions.

---

## Stage 3 — Agents that act ✅

Give agents the ability to *use* tools, safely.

- ✅ **Tool-calling loop**: let an agent select and invoke tools, feed results
  back, and iterate (function-calling where the provider supports it).
- ✅ **Permission enforcement**: honour each tool's declared permissions;
  require confirmation for destructive/sensitive actions.
- ✅ **More agents**: IGRIS (study/academic), DHANUS (spiritual knowledge),
  TANK (coding) — each with scoped tools, permissions, and prompts.
- ✅ **Core tools**: web search, file/document analysis, document generation,
  calendar/tasks — added one at a time, each tested.
- ✅ **Planning/task decomposition** for multi-step goals.

---

## Stage 4 — Interfaces ✅

- ✅ **WebSocket channel** for live streaming chat responses.
- ✅ **Frontend UI**: a clean, futuristic "system" interface (System/Solo
  Leveling aesthetic) showing conversation, agent states, tool execution,
  tasks, memory, and logs. Real-time updates via WebSockets.

---

## Long-term capabilities ✅

Powerful capabilities, each built on top of the confirmation-gated permission
model established in Stage 3:

- ✅ **Voice system** — STT/TTS provider abstractions with hermetic mock
  providers, wake word detection ("beru"), continuous listening buffers,
  interruption, session lifecycle, REST + WebSocket endpoints. Real offline
  speech (whisper/vosk) plugs into the same protocols later. See
  `backend/engines/speech.py`, `backend/engines/voice.py`,
  `backend/api/routers/voice.py`.
- ✅ **Device & system control** — execute approved commands (allowlist +
  destructive-pattern blocking), launch apps, OS notifications. See
  `backend/engines/command.py`, `backend/engines/app_launcher.py`,
  `backend/engines/notifier.py`, `backend/tools/system.py`.
- ✅ **Android app / phone sync** — laptop ↔ phone sync contract: device
  registry, delta pulls keyed by per-device cursors, messages authored on the
  phone, and a push-notification mailbox with explicit ack. The Android client
  is a thin consumer of this API. See `backend/engines/sync.py`,
  `backend/services/sync_service.py`, `backend/api/routers/sync.py`.
- ✅ **Browser automation.** Real page management, navigation, click, type,
  scroll, screenshot, text/HTML extraction, back/forward navigation, refresh —
  now backed by **Playwright** (headless Chromium) via `backend/engines/browser.py`,
  wired into `backend/tools/browser.py` (the five browser tools are `available`
  to the LLM) and `backend/api/routers/browser.py` (the shared
  `get_browser_engine()` singleton). If Playwright is not installed, the engine
  degrades gracefully to `unavailable` instead of crashing. Install:
  `pip install -r requirements.txt` + `playwright install chromium`.
- ✅ **Desktop control (Phase 1).** Screenshot, screen size, mouse
  position/move/click/double-click/scroll, keyboard type/press/hotkey,
  clipboard read/write, and process list/find — via
  `backend/engines/desktop_platform.py`, `screenshot.py`, `input.py`,
  `clipboard.py`, `apps.py`, exposed as 14 tools in `backend/tools/desktop.py`
  and a 23-endpoint authenticated UI in `backend/api/routers/desktop.py`.
- ✅ **Browser panel (frontend).** A "Browser" tab in the UI mirroring the
  Desktop panel: capability badge, active-page display, page management
  (create new / list / activate / close tabs), navigation (go / back / forward
  / refresh), interaction (click / type / scroll by selector), and read
  (extract text / screenshot saved locally). The browser router
  (`backend/api/routers/browser.py`) now returns consistent `ok` envelopes, a
  `/status` availability endpoint, and the async `create_page` fix; covered by
  `tests/test_browser_api.py`.
- ✅ **Desktop panel (frontend).** A "Desktop" tab mirroring the browser panel
  for the Phase 1 desktop engines: capability status, screenshot capture, mouse
  position / screen size / move / click / double-click / scroll, keyboard
  type / press / hotkey, clipboard read/write, and process list / find.
  Driven by `frontend/app.js` (`renderDesktop()`, `loadDesktopStatus()`) against
  the 23-endpoint `backend/api/routers/desktop.py`.
- ✅ **Tools tab (frontend).** A searchable, filterable tool list in the UI:
  live search box, availability filter buttons, category grouping
  (`toolCategory()`: Browser, Desktop, System, Voice, Web, Study, Code & Docs,
  Calendar — every one of the 66 registered tools maps to a group, none fall to
  "Other"), plus permission chips and a confirmation-required chip per tool.
- ✅ **Automation UI polish.** The Automation tab now manages the full
  proactive surface end to end:
  - **Triggers section** — lists every monitor trigger (status, condition,
    fire count, cooldown, retention policy) with History / Pause / Resume /
    Disable / Delete controls and a create-trigger form (name, source,
    condition type, operator, value, cooldown).
  - **Trigger fire history** — a drill-down mirror of task history
    (`GET /api/v1/monitor/triggers/{id}/fires`) with a summary (total, last
    value) and a per-fire table (time, condition, value).
  - **Task Edit** — an inline form that PATCHes `/api/v1/scheduler/tasks/{id}`
    (`name`, `interval_seconds`, `max_runs`, `retention_days`, `keep_last`).
  - **Retention policy badges** on task rows (`retain Nd` / `keep N`).
  - **Prune control** in the task history view — POST
    `/api/v1/scheduler/tasks/{id}/prune-runs` with `retention_seconds` and/or
    `keep_last` to bound history from the UI.
- ✅ **Proactive/autonomous behaviour** — scheduler, notifications, event
  monitoring with triggers, now fully wired into the app runtime and exposed
  over the API:
  - **Scheduler** (`backend/engines/scheduler.py`) — one-shot and periodic
    tasks with pause/resume/cancel/delete, manual run, and a background loop
    started/stopped with the app.
  - **Notifications** (`backend/engines/notifications.py`) — DB-persisted
    inbox with read/unread state, pushed live to connected clients over a
    WebSocket broadcast.
  - **Event monitor** (`backend/engines/monitor.py`) — pluggable data sources
    (CPU usage, memory percent, unread notifications, stored **facts**, recent
    **conversation activity**) plus **scoped (parameterised) sources** — a
    trigger can reference `memory.facts_count:work`,
    `memory.facts_project_count:<project_id>`,
    `conversations.messages_24h:<conversation_id>`, and friends to watch a
    single fact category, project, or conversation — plus **cross-project
    aggregate sources** (`projects.count`, `projects.new_24h`,
    `projects.messages_24h`, `projects.active_24h`) for reactor-wide volume and
    velocity trends. Condition triggers with cooldown + re-arm, and manual
    `tick`.
  - **Shared runtime** (`backend/services/proactive_service.py`) — both loops
    run under the FastAPI lifespan (`BERU_PROACTIVE_ENABLED`); actions dispatch
    to a `notify` helper or a real confirmation-gated **agent turn**
    (`agent_turn`). APIs: `/api/v1/scheduler/tasks*`,
    `/api/v1/scheduler/notifications*`, `/api/v1/monitor/*` (all key-protected).
  - **Persistence** (`backend/services/proactive_store.py`) — tasks and
    triggers are written through to the database (`scheduled_tasks`,
    `monitor_triggers` tables via migration `b1e2f3a4c5d6`); every create,
    pause/resume/cancel, delete, and execution-state change is persisted, and
    `start_proactive_runtime()` restores them on startup. Execution happens on
    the in-memory engine, so the store keeps each subsequent state change in
    sync with the running runtime.
  - **Audit history** (`task_runs`, `trigger_fires` via migration
    `c1d2e3f4a5b6`) — every scheduler execution (status, duration_ms, error)
    and every trigger firing (condition + satisfying source value) is appended
    by in-engine hooks. History is **summarised** (counts, outcome ratio,
    duration stats, first/last event, per-failure **error grouping**),
    **prunable** per task/trigger by age (`retention_seconds`) or count
    (`keep_last`), and **exportable/restorable** (`GET/POST
    /api/v1/scheduler/history/export|import` and the monitor equivalents) —
    exports are idempotent (stable row ids) so the logs can be safely backed
    up and restored elsewhere; a startup retention sweep
    (`BERU_PROACTIVE_AUDIT_RETENTION_DAYS`) drops rows older than N days.
  - See also `backend/api/routers/scheduler.py`, `backend/api/routers/monitor.py`.

---

## Recommended next step

The proactive/history story is **complete end to end** (capture → summarise →
error-group → per-source prune → export/restore), and the Automation tab exposes
the whole surface (tasks, triggers, history, edits, retention, prune). Recent
deliveries folded into the features above: reactor-level notifications (a
`reactor` scheduled-task type that streams one task's run outcome, error, and
error-group count to the inbox), a `history.*` source family so triggers react
to their own track record (`history.task_failure_rate`, `history.task_total_runs`,
`history.task_last_error`, `history.task_error_groups`,
`history.trigger_fire_count`, `history.trigger_last_value`), richer run-log
outcomes, frontend reactor + history views, and per-source retention via
`retention_days` / `keep_last` (migration `3a8ba1ca1bb8`).

All of the candidates below are now **shipped** (in rough priority order):

- ✅ **Agent tool-calling E2E** — an end-to-end test proving the agent invokes
  real registry tools through the full stack (HTTP `/api/v1/chat` and
  `/api/v1/chat/stream` → ChatService → IntelligenceEngine → shared agent
  registry → real tool execution → result fed back → reply persisted), plus
  engine-level grounding proof that the real tool output reaches the model
  (`tests/test_agent_tool_e2e.py`, 6 tests).
- ✅ **Browser extra actions** — page title/URL read (`page_info`), scroll to a
  selector (`scroll_to`), press-key / form submit (`press`), plus dedicated
  `browser_{page_info,scroll,press,back,forward,refresh}` tools (11 browser
  tools total) and new panel controls (Info, Scroll to, Press). Back/forward/
  refresh were already wired end to end.
- ✅ **Desktop control Phase 2** — window management (list/focus/graceful-close
  by `hwnd` or title), multi-monitor geometry, a live window snapshot feed over
  SSE (`GET /api/v1/desktop/windows/stream`), and **durable global hotkeys**
  persisted to `data/hotkeys.json` and re-applied to the OS on startup by a
  background `RegisterHotKey` + `PeekMessage` listener thread
  (`backend/engines/windows.py`, `backend/engines/hotkeys.py`). Exposed as 7
  new tools (`list_windows`, `focus_window`, `close_window`, `list_monitors`,
  `list_hotkeys`, `register_hotkey`, `unregister_hotkey` → 21 desktop tools, 49
  total) plus panel controls (Windows/Monitors/Global hotkeys sections) in
  `frontend/app.js`; covered by `tests/test_desktop_windows.py` and extended
  `tests/test_desktop_api.py` / `test_desktop_control.py`.
- ✅ **Browser Phase 1 — close the automation gaps** — select-option
  (`browser_select`: value/label/index), whole-session close
  (`browser_close`, confirmation-gated), full-page screenshots (`full_page`
  flag), and structured page inspection (`page_info` now returns bounded
  visible text, top links, and interactive elements with ready-to-use
  selectors). 13 browser tools, 51 total; `POST /api/v1/browser/close` plus
  panel controls (Select, Full-page screenshot, Close browser) in
  `frontend/app.js`; engine covered not only by the fake-page unit tests but
  also by live probes of the real Playwright — `tests/test_browser.py`,
  `tests/test_browser_api.py`, extended `tests/test_system_dashboard.py`.
- ✅ **Browser Phase 2 — deep pages** — frame-scoped actions (click/type/press/
  select/extract/scroll-to by frame name, URL substring, or index) with a
  frame map in `page_info`; drag & drop (`browser_drag`); session cookies
  get/set/clear (`browser_cookies`); localStorage get/set/clear
  (`browser_storage`); a bounded per-page network-response tap
  (`browser_network`); session URL-glob request blocking (`browser_block_url`);
  file download to a local dir (`browser_download`) and upload
  (`browser_upload`); multi-option select (`values` list); and popup/child
  tabs auto-registered as pages via a context `page` handler. 7 new tools →
  20 browser tools, 58 total; Deep pages section (Frame, Drag, Cookies,
  Storage, Block glob, Network log, Upload, Download) in the Browser panel of
  `frontend/app.js`.
- ✅ **Browser Phase 3 — debugging & session state** — per-page console and
  uncaught-page-error tap (`browser_console`, bounded, deduped); a download
  listing tool (`browser_downloads`); session URL-glob 302 redirect rewrites
  (`browser_redirect_url`); and session-state `browser_snapshot`/`browser_restore`
  that persist cookies + localStorage to named JSON files and relaunch a fresh
  context from that storage state on restore. `status_info()` drives a richer
  `/status` (mode, pages, blocked globs, redirects, started time, download/
  session dirs) surfaced in the Browser panel status line. 5 new tools →
  25 browser tools, 63 total; Console/Downloads/Redirect/snapshot/restore
  controls added to the Deep pages section of `frontend/app.js`.
- ✅ **Browser Phase 4 — request fulfilment & browser control** — intercept
  URL globs and answer with fixed status/body/content-type for API mocking
  (`browser_fulfill`, bounded `_FULFILLS_MAX=50`); a confirm-gated
  `browser_launch` that closes and relaunches the browser toggling headless
  mode and setting viewport/user-agent/locale (`_launch_opts`/`_context_opts`
  honoured by `ensure_ready`); console entries now capture their source
  location; screenshot/download/session dirs are env-overridable; `/status`
  adds headless mode + fulfil count; session-scoped launch/restore skip the
  active-page guard. 2 new tools → 27 browser tools, 65 total; Fulfill +
  Relaunch controls and a mode/fulfil-count status line in `frontend/app.js`.
- ✅ **Browser Phase 5 — fulfil refinement & emulation breadth** — fulfil now
  serves extra response `headers`; every network-log entry carries HTTP status,
  response size (from `content-length`), and `timing_ms`; `browser_launch`
  gains device emulation — `device_scale_factor`, `is_mobile`, `has_touch`,
  `timezone_id` — stored via `_context_opts` and reported in `/status`; a new
  confirm-gated `browser_clear_downloads` empties the session download dir and
  reports how many files were removed. 1 new tool → 28 browser tools, 66 total;
  fulfil headers input, emulation row, and Clear-downloads button in
  `frontend/app.js`.
- ✅ **Reliability pass — observability & audit** — an in-memory **activity
  ledger** (`backend/services/activity_ledger.py`): `BaseAgent.run_tool`
  records every resolution — success / failure / permission_denied /
  confirmation_required, plus duration, request id, and bounded argument/error
  summaries — into a 500-entry ring buffer; `ChatService` writes an
  **approval audit trail** (200 entries) for every `confirm`/`deny` decision.
  Key-protected `GET /api/v1/reliability/{activity,audit,summary}` plus
  `DELETE /activity` (clear) and a `reliability` summary embedded in
  `GET /api/v1/status`; a frontend **Reliability** tab renders activity +
  audit with a live summary and clear control. Command-execution and
  OS-notification histories are capped at 200 entries (`_MAX_HISTORY`).
  Coverage: `tests/test_reliability.py` (ledger bounds/summary/clear, API
  endpoints, status field, tool-call + confirm/deny integration) and
  `tests/test_system_control.py` history-bound tests. No new tools (28 browser
  tools, 66 total). 819 → 833 tests.
- ✅ **Reliability pass 2 — durable ledger** — the activity ledger and approval
  audit trail now persist to the database instead of living only in memory:
  new `activity_records` / `audit_records` tables (Alembic
  `d4e5f6a7b8c9`), each row keyed by a UUID `id` with a monotonic indexed `seq`
  ordering column. Every recorded entry is mirrored best-effort (fire-and-forget
  background task; recording never blocks tool execution), and
  `restore_activity_ledger()` reloads the newest rows into memory at startup,
  pruning anything beyond the bounded 500-activity / 200-audit window so the
  tables stay capped. `SET` a session factory at runtime (the ledger's
  `set_ledger_session_factory`) to enable durability; `GET /api/v1/status` and
  the Reliability tab summary now report a `persisted` flag / storage badge, and
  `DELETE /api/v1/reliability/activity` purges the persisted rows too.
  Coverage: `tests/test_reliability_persistence.py` (in-memory-only when no
  factory, persist + restart restore for activity and audit, run_tool writes a
  row, DB prune to bounds, clear/purge, endpoint purge, idempotent restore).
  No new tools (28 browser tools, 66 total). 833 → 840 tests.
- ✅ **Docs polish** — full documentation audit against the live codebase:
  every roadmap/architecture/README/decisions entry re-verified, plus a
  frontend correctness fix surfaced by that audit.
  - **Audited**: tool counts and categories (re-derived from `_seed_registry`:
    28 browser / 21 desktop / 66 unique across 4 agents), the complete API
    surface (all routers, incl. confirm/deny/confirmations, llm + llm/test,
    facts, projects, plans, system-control, voice, sync, auth, and the
    WebSocket channels), agent descriptions, the durable-ledger lifespan
    wiring + `persisted` flag + `/status` field + frontend storage badge,
    persistence/migration ids (single Alembic head `d4e5f6a7b8c9`, no
    branches), config/env defaults vs `settings`, `.env.example`, README curl
    payloads vs request schemas, and the conftest hermeticity claims.
  - **Fixed in docs**: README status line now covers desktop Phase 2; README
    Reliability-tab bullet + frontend-panels list now include the Memory, Plans,
    Notify, and Settings panels; README config table gained memory, embedding,
    rate-limit, tool-iteration, API-key, and voice rows; README security note
    now lists browser navigation/close/launch/clear-downloads as
    confirmation-gated; `.env.example` gained the missing voice block;
    architecture API-surface table was completed (facts, projects, plans, voice,
    sync, system-control, auth, llm/test, chat confirm/deny, WS) and now notes
    that every router is key-protected; the browser `requires_confirmation`
    count corrected from 3 → 4 tools (navigate added); the extension-points doc
    now points tool registration to where it actually happens (per-agent
    `register_tool` in `agents/registry._seed_registry`, not `tools/registry`);
    roadmap Tools-tab entry updated "36 tools" → "66 tools"; `decisions.md` now
    reads "browser (28) and desktop (21) tools".
  - **Fixed in code (from the audit, drives the Tools tab)**: `frontend/app.js`
    `toolCategory()` was stale — the 7 desktop Phase 2 tools (list/focus/close
    window, list monitors, and hotkeys list/register/unregister) fell to
    "Other"; they now map to Desktop, so all 66 tools group correctly
    (repo claim restored: none fall to "Other").
  - **Verified**: full suite 840 passed, ruff clean, `node --check` clean,
    Alembic head single (`d4e5f6a7b8c9`) with no branches, migration tests
    round-trip green. No test count change (the one code touch was a pure
    frontend category map).

Everything above ships with hermetic tests; nothing is merged without ruff +
`pytest` green.

> **Roadmap status**: every planned milestone above is now ✅ shipped. With no
> remaining planned features, the next engineering phase is **production
> readiness / release hardening** (see below) rather than a new feature.

## Next phase — release hardening (proposed)

All feature work is shipped; the roadmap's remaining value is preparing v0.1.0
for real use rather than adding surface area. Candidate hardening items, each
independently verifiable and testable, in priority order:

- ✅ **Deployment & startup** — a container image (or `run.sh`/service) with
  documented env handling, bind behaviour, and tidy shutdown; a healthcheck path
  that exercises the DB + LLM probe.
- ✅ **Secrets & auth hardening** — session tokens are random/opaque and stored
  only as a SHA-256 hash in a server-side store; logout revokes server-side;
  rotating `BERU_API_KEY` invalidates all sessions; auth (login) rate limiting
  with spoof-resistant per-IP buckets; TLS/proxy + session-cookie notes
  documented for non-localhost use.
- ✅ **Operational observability** — `GET /api/v1/metrics` exposes process-local
  request rate/histogram, error-envelope counts, tool outcomes from the
  reliability ledger, active sessions, and proactive scheduler/monitor run
  rates; documented log-format contract (see
  [`docs/observability.md`](docs/observability.md)).
- ✅ **Backup & restore** — SQLite online-backup snapshot (safe while the app is
  running, WAL-aware), automatic integrity + row-count verification, and a
  documented restore procedure with pre-restore copies; covers conversations,
  proactive store/history, and the reliability ledger tables in one file (see
  [`docs/backup.md`](docs/backup.md)).
- ✅ **Release checklist** — `scripts/release.py` pins dependencies
  (`requirements.lock` / `requirements-dev.lock` from the installed closure),
  bumps versioning from the single source in `backend/__init__.py` (mirrored
  into `pyproject.toml`, read by settings), and `python -m scripts.release check`
  is the pre-release smoke test: version consistency, single Alembic head,
  migration round-trip, ruff, `node --check`, full pytest, and lockfile sanity
  (see [`docs/release.md`](docs/release.md)).

---

## Stage 5 — Beyond one user 🔜

The release-hardening phase made v0.1.0 shippable. The next engineering phase
turns BERU from a single-owner source tree into a system more than one person
can actually run: accounts, a real database, an extension platform, and host
isolation for tools. Each item below is independently verifiable with hermetic
tests and a `python -m scripts.release check` gate; they should be built in
dependency order 5.1 → 5.2, 5.3 → 5.4 (5.1 and 5.4 are independent of each
other).

- 🔜 **5.1 Accounts & multi-user isolation** — today auth is one shared API key
  with a volatile in-memory session store (`backend/api/security.py`) and no
  user table. Deliverables: a `users` migration; a durable, DB-backed session
  store that survives restarts; first-run bootstrap of an owner account; per-user
  ownership and scoping of conversations/facts/projects (user A cannot see
  user B's data); per-user rate limits and login. Gate: isolation + persistence
  tests green.
- 🔜 **5.2 Real database support (PostgreSQL)** — Postgres is nominal today:
  no `asyncpg`/`psycopg` in requirements, backup is SQLite-file-only
  (`backend/services/backup.py`), and the instance lock skips non-SQLite
  (`backend/database/lock.py`). Deliverables: add the async drivers; make backup
  and the instance lock driver-agnostic; run the full suite on SQLite and
  Postgres in CI; compose file + docs. Gate: full suite green on Postgres.
- 🔜 **5.3 Extension platform (packaging + plugins + real voice)** —
  `pyproject.toml` has no `[build-system]` (so `pip install .` fails) and every
  provider requires editing a hardcoded factory. Deliverables: add
  `[build-system]` + setuptools so `python -m build` / `pip install .` work and
  the existing `bump` drives versioning; entry-point based discovery for
  tools/agents/providers with the current registry as fallback; ship whisper STT
  + edge-tts TTS as first-party optional-extra plugins. Gate: install a sample
  tool from a wheel in a clean test env.
- 🔜 **5.4 Host hardening (tool sandbox)** — `CommandExecutor` is an allowlist +
  pattern-blocklist over a full-privilege child process. Deliverables:
  least-privilege service account for subprocesses; per-tool credential scoping
  (tools get scoped keys, not the global API key); an optional container
  executor backend implementing the same interface. Gate: guardrail tests prove
  destructive commands are blocked in every backend.

None of these change product behaviour; each is testable and keeps the existing
architecture.
