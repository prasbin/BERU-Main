# Engineering Decisions

A lightweight decision log (ADR-style) capturing the significant choices behind
the v0.1.0 foundation and *why* they were made. Each entry is small on purpose;
the goal is that a future contributor (or a future session) understands the
rationale without re-deriving it.

Format: **Decision · Context · Rationale · Consequences / revisit-when.**

---

## 1. Async SQLAlchemy 2.0 + async endpoints

**Decision.** Use SQLAlchemy 2.0 in fully async mode (`create_async_engine`,
`async_sessionmaker`, `AsyncSession`) with async FastAPI route handlers.

**Context.** BERU makes network-bound LLM calls per request and will grow more
I/O (tools, web, devices).

**Rationale.** An async stack keeps the event loop free during LLM/network waits,
scaling far better than sync-per-request. 2.0's typed `Mapped[...]` models are
clearer and future-proof.

**Consequences.** Everything DB-touching is `async`; tests use
`pytest-asyncio`. Revisit only if a required dependency is sync-only.

---

## 2. Provider-agnostic LLM layer with a mock default

**Decision.** Define a narrow `LLMProvider` interface speaking only
`LLMMessage`/`LLMResponse`. Ship a `MockProvider` as the **default**.

**Context.** The project mandates that LLM providers be replaceable, and the app
must be runnable and testable with zero setup.

**Rationale.** No caller ever imports a vendor SDK, so swapping providers is a
config change. A deterministic offline mock means the server runs and the whole
test suite passes with no API key and no network.

**Consequences.** New providers implement one method and register in the
registry. The mock's output is intentionally not "intelligent" — it echoes — so
tests stay deterministic.

---

## 3. One OpenAI-compatible adapter instead of per-vendor clients

**Decision.** Implement a single `OpenAICompatibleProvider` (raw `httpx` to
`/chat/completions`) rather than separate OpenAI/Groq/Ollama integrations.

**Context.** The user selected an OpenAI-compatible provider, and most hosted and
local runtimes expose that same wire format.

**Rationale.** One tested adapter covers OpenAI, Groq, OpenRouter, Ollama, LM
Studio, and more — just different `LLM_BASE_URL`/`LLM_MODEL`. Using `httpx`
directly avoids a heavy SDK dependency for a simple POST.

**Consequences.** Providers with materially different APIs (e.g. Anthropic's
native format) still need their own class — cheap, thanks to the interface.

---

## 4. Engine/agents stay database-agnostic; services own persistence

**Decision.** The intelligence engine and agents perform pure reasoning and know
nothing about the database. `ChatService`/`ConversationService` own persistence
and memory assembly.

**Context.** Reasoning and storage change for different reasons and at different
rates.

**Rationale.** Keeping reasoning free of DB concerns makes agents unit-testable
in isolation and lets storage evolve (schema, migrations, vector stores) without
touching agent logic — a clean seam matching the layered architecture.

**Consequences.** The service composes memory → engine → persistence and commits
once per turn (atomic turns). Memory strategies swap behind `BaseMemory`.

---

## 5. Separate Pydantic schemas from ORM models

**Decision.** `schemas/` (Pydantic, wire contract) is distinct from `models/`
(SQLAlchemy, storage), with explicit mapping between them.

**Rationale.** The public API shape and the database schema should evolve
independently; leaking ORM objects to the wire couples them and risks exposing
internal fields.

**Consequences.** A little mapping boilerplate, paid back in flexibility and
clarity. Response models use `from_attributes=True` for easy conversion.

---

## 6. `create_all` first, Alembic once the schema matters — **now Alembic** (see #13)

**Decision.** Bootstrap the schema at startup with `Base.metadata.create_all`
during the greenfield phase; adopt Alembic migrations once schema evolution
over real data becomes a concern.

**Context.** Greenfield project, one developer, a schema still in flux.

**Rationale.** `create_all` is zero-overhead and perfect for early iteration.
Introducing migrations before the schema stabilised would have slowed
experimentation with little benefit.

**Consequences.** This was the right call for v0.1.0 and has now been
**superseded**: with the data model settled and streaming shipped, BERU adopts
**Alembic** so schema changes are versioned and reversible. See decision **#13**
for the migration design.

---

## 7. String (UUID) primary keys

**Decision.** Use 36-char UUID strings as primary keys, generated in the app.

**Rationale.** UUIDs are safe to expose in URLs, don't leak row counts, avoid
autoincrement coordination, and stay stable if storage is ever sharded or
synced across devices (a stated long-term goal: laptop ↔ Android).

**Consequences.** Slightly larger keys than integers — negligible at this scale.

---

## 8. Stdlib logging (no loguru) + request correlation id

**Decision.** Use Python's standard `logging` with a custom JSON formatter and a
`ContextVar`-based `request_id`, instead of adding loguru.

**Context.** Project instructions list "Loguru **or** structured logging".

**Rationale.** Structured logging is the requirement; the stdlib meets it with
zero extra dependencies and integrates cleanly with Uvicorn. Fewer dependencies
is a project value.

**Consequences.** A little formatter code we own. Every log line can carry the
request id for tracing a request end to end.

---

## 9. Consistent error envelope via a `BeruError` hierarchy

**Decision.** All application errors derive from `BeruError` (carrying an HTTP
status and a stable `error_type`); exception handlers render one shape:
`{"error": {"type", "message", "detail?"}}`.

**Rationale.** A predictable, machine-readable error contract makes clients (and
the future UI) far simpler, and centralises how errors map to HTTP.

**Consequences.** New error kinds subclass `BeruError`; validation errors are
normalised into the same envelope.

---

## 10. Permissioned tools, not auto-executed yet — **now tool-calling (see Stage 3)**

**Decision.** Tools declare `permissions` and `parameters` and are discoverable
via the API; this decision originally kept agents from calling them
autonomously.

**Context.** Security is a first-class requirement; unrestricted tool/system
access is explicitly disallowed.

**Rationale.** Establish the permission-bearing interface first; wire up
execution only alongside enforcement and confirmation for sensitive actions.
Building the loop before the guardrails would invite unsafe behaviour.

**Consequences.** **This is now superseded by Stage 3** (see `roadmap.md`):
agents execute tools during a turn and reason over real results. The original
guardrail survives as the confirmation gate — tools flagged
`requires_confirmation` (command execution, app launching, notifications) always
return a `confirmation_required` result and are never auto-confirmed.

---

## 11. `requirements*.txt` as the dependency source of truth

**Decision.** Pin runtime deps in `requirements.txt` and dev deps in
`requirements-dev.txt` (which includes the former); `pyproject.toml` is used for
tooling config (pytest, ruff) and metadata.

**Rationale.** Explicit, reproducible installs that are obvious to any Python
developer, while keeping test/lint configuration centralised.

**Consequences.** Two dependency files to keep in sync (runtime vs dev), which is
a deliberate, conventional split.

---

## 12. Streaming via SSE, layered chunk types, persist-after-stream

**Decision.** Add streaming as a parallel path: `stream_chat()` on the provider
(yielding `LLMStreamChunk`), `BaseAgent.stream()` (yielding `AgentStreamChunk`),
`ChatService.stream_process()` (yielding `ChatStreamEvent`), and a
`POST /chat/stream` **SSE** endpoint. The buffered `/chat` path is untouched.

**Context.** Token-by-token replies are high user-value, but must not compromise
the atomic-turn guarantee or the provider-agnostic interface.

**Rationale.** SSE (not WebSockets) fits one-way server→client token flow, works
over plain HTTP, and needs no new dependency. The ABC's `stream_chat` **defaults
to a single `chat()` call**, so providers without native streaming still work.
Distinct chunk types per layer keep each layer speaking its own vocabulary
(provider → agent → chat/API) rather than leaking a vendor shape upward. The
service **never holds a database session across the LLM stream**: a short-lived
session resolves/creates the conversation and persists the user message before
the first token, the tokens stream with no session open, and a second short-lived
session persists the assistant message after the stream ends. A mid-stream
failure or client disconnect emits an `error` event; the already-committed user
message is retained and no partial assistant text is written.

**Consequences.** The endpoint pulls the first (`start`) event eagerly so an
unknown conversation/agent still returns a clean 404 before the stream begins.
Because no session crosses the streaming window, a long conversation turn can no
longer pin an SQLite writer or hold a connection open — the `get_session`
dependency is used only by buffered/chat paths. `stream_process` consumes a
swappable session factory (`set_stream_session_factory`), which tests redirect
to a throwaway database. Usage is only available mid-stream when the server
honours `stream_options.include_usage` (OpenAI-style); others simply report
none. Revisit if a provider needs a non-SSE transport or bidirectional
streaming.

---

## 15. Append-only tables order by monotonic `seq`, never random ids

**Decision.** Every append-only audit/history table orders "newest first" by a
**monotonic insert-order `seq` counter**, and all engine-facing
newest-`n`/latest-value reads (task history, fire history, `trigger_last_value`,
prune `keep_last`) use it as the tie-breaker. The reliability ledger
(`activity_records`, `audit_records`) already shipped `seq`; this pass extended
the same design to `task_runs` and `trigger_fires` via migration
`b5c6d7e8f9a1`.

**Context.** `task_runs`/`trigger_fires` previously broke same-timestamp ties by
`created_at` (integer-second in SQLite) then row `id` — a random UUID4. Under
load, two rows appended within the same microsecond were ordered
nondeterministically, which made `list_*` "newest first" assertions and
`trigger_last_value` (used by `history.trigger_last_value` monitor triggers)
return the wrong row **intermittently** (reproduced ~1/5 file runs).

**Rationale.** `created_at`/`run_at`/`fired_at` are all produced by the
application clock and can legitimately collide at microsecond precision; a
random UUID `id` has no ordering semantics. `seq` is assigned in the INSERT
statement itself (`MAX(seq)+1` as a scalar subquery), so it is atomic under
concurrent writers and requires no application-level counter. `seq >= 0` check
constraints mirror the reliability-ledger tables; legacy rows are backfilled
from SQLite rowid (insertion order) in the migration.

**Consequences.** Milliseconds-level insertion order is preserved across
restarts and `datetime` ties. Export documents now carry `seq` so restore keeps
deterministic order. The `datetime`-only orderings in "oldest first" export
queries also gained the `seq` tie-breaker (ascending). Revisit if history tables
outgrow a 64-bit `seq` (practically never for an append-only log).

---

## 13. Alembic migrations with a sync engine, auto-applied at startup

**Decision.** Replace `Base.metadata.create_all` with **Alembic**. Migrations
run through a **synchronous** engine derived from the app's async
`DATABASE_URL`, live in `migrations/` with `env.py` targeting `Base.metadata`,
and are **applied automatically at startup** (in a worker thread). A single
helper (`backend/database/migrations.py`) is the one source of truth used by
both the CLI and the app; it imports `alembic` lazily.

**Context.** The data model has settled and real conversation data now
accumulates in `beru.db`, so schema changes must be versioned and reversible
rather than silently diffed by `create_all` (which never alters existing tables).

**Rationale.** A **sync** engine for migrations avoids nesting `asyncio.run`
inside the running event loop (the failure mode of Alembic's async template) and
is simpler and more robust for short, serial DDL. **Auto-applying at startup**
preserves BERU's zero-config "just runs" experience — the previous behaviour of
`create_all` — while switching the mechanism to versioned migrations; the manual
`alembic upgrade head` remains available for controlled deploys. Because a
developer already has a `beru.db` from earlier stages, `run_migrations` **adopts
an un-versioned database by stamping it to head** instead of failing on
"table already exists". `render_as_batch=True` keeps future SQLite `ALTER`s
working.

**Consequences.** `alembic` becomes a runtime dependency. The URL is read from
settings (never hardcoded), converted via `to_sync_url`. Tests build their
schema directly from metadata for speed/hermeticity and don't run the lifespan,
so they neither require Alembic nor exercise startup migration — a dedicated
`tests/test_migrations.py` (guarded by `importorskip`) proves the migration
produces exactly the ORM schema and round-trips. **Revisit when** a non-SQLite
backend or a separate migrate-then-serve deploy step is wanted (move the
auto-apply behind a flag).

---

## 14. Real capability engines with availability probes, never faked output

**Decision.** Replaced the browser/desktop stubs with real engines — Playwright
(headless Chromium) for browsing, native platform APIs for desktop — where each
engine reports availability through a live probe (`available`/`limited`/
`unavailable`) and every tool/API path degrades gracefully when its backend is
absent instead of returning fabricated results.

**Context.** Earlier milestones deliberately shipped "unavailable" placeholders
so the permission model, tool interface, and API contracts could be built and
tested before a heavyweight dependency was installed. Real device/browser
behaviour is the whole point of those tools.

**Rationale.** Real results let agents reason over truthful output (actual page
text, actual screenshots) — the base requirement for reliable tool-calling.
The probe-degrade model keeps the app runnable and the hermetic test suite
offline: if Playwright/Chromium (or a platform backend) is missing, the engine
reports `unavailable` and the app and tests still work; tests substitute fake
engines where deterministic behaviour matters, while a separate live E2E check
exercises real Chromium in CI-capable environments.

**Consequences.** `playwright` is now a runtime dependency and Chromium must be
installed once (`playwright install chromium`) for browser features. Engine,
tools, API, and frontend all share one availability source of truth. The
browser (28) and desktop (21) tools are wired through the confirmation gate for
sensitive actions. Revisit when a hosting environment cannot run a browser —
run the browser engine as an opt-in sidecar behind the same probe.
