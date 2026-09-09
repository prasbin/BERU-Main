# Observability

BERU exposes two complementary observability surfaces for a single-process
deployment:

1. **`GET /api/v1/metrics`** — a JSON snapshot of process-local request, error,
   reliability-ledger, session, and proactive run counters.
2. **Structured logs** — text or JSON with a per-request correlation id, plus a
   machine-readable error-type header on every error envelope.

Both are deliberately lightweight and dependency-free; there is no metrics
scraper or log forwarder bundled. Values are **process-local**: they reset on
restart, and each worker process reports its own numbers.

## `/api/v1/metrics`

Requires the API key (same auth as every other API route). Sample response:

```json
{
  "app": "BERU",
  "version": "0.15.0",
  "environment": "development",
  "llm_provider": "mock",
  "requests": {
    "uptime_s": 1234.5,
    "total_requests": 42,
    "requests_per_1m": 3.0,
    "by_status": { "200": 40, "404": 1, "429": 1 },
    "by_error_type": { "not_found": 1, "rate_limit_exceeded": 1 }
  },
  "active_sessions": 1,
  "reliability": {
    "uptime_s": 1234.5,
    "total_tool_calls": 12,
    "successes": 10,
    "failures": 1,
    "confirmations_pending": 1,
    "approvals": 3,
    "denials": 0,
    "top_tools": [["desktop.screenshot", 5]],
    "persisted": false
  },
  "proactive": {
    "scheduler_running": false,
    "monitor_running": false,
    "tasks": [{ "name": "daily-review", "status": "pending", "run_count": 0, "last_run": null }],
    "triggers": [{ "name": "file-change", "fire_count": 5, "last_fired": "2026-09-08T..." }]
  }
}
```

Field reference:

| Field | Meaning |
|---|---|
| `requests.total_requests` | Requests completed since process start. |
| `requests.requests_per_1m` | Requests in the trailing 60-second window (rolling average). |
| `requests.by_status` | Histogram keyed by status code (keys stringified for JSON). |
| `requests.by_error_type` | Error envelopes counted by `error.type`. |
| `active_sessions` | Live server-side session tokens in the in-memory store. |
| `reliability` | Tool activity summary from the reliability ledger (see `backend/services/activity_ledger.py`). |
| `proactive` | Scheduler task run counts and monitor trigger fire counts (`scheduler_running`/`monitor_running` reflect whether the background loops are live). |

`/metrics` never starts background loops and never performs I/O: it reads
in-memory counters and the durability serialisation of the ledger only. If a
sub-component (e.g. the proactive runtime) is not importable, its section
degrades to empty/false rather than failing the request.

### Error-type header

Every API error envelope carries a response header:

```
X-Beru-Error-Type: not_found
```

`/metrics` tallies these headers — this is why envelope counting works without
parsing response bodies. The header is stamped by the exception handlers in
`backend/core/errors.py`.

## Log-format contract

Logging is centralised in `backend/core/logging.py` — one formatter, one
handler, and a `request_id` correlated across every log line in a request.

### Selecting a format

- `BERU_LOG_FORMAT=text` (default) — human-readable single-line records.
- `BERU_LOG_FORMAT=json` — one JSON object per line (Structured Logging; easier
  to ship to a log pipeline).
- Level: `BERU_LOG_LEVEL` (default `info`).

### Fields (JSON format)

| Field | Type | Meaning |
|---|---|---|
| `time` | string | `YYYY-MM-DDTHH:MM:SS` with offset (UTC). |
| `level` | string | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. |
| `logger` | string | Logger name, e.g. `backend.chat`. |
| `request_id` | string | Correlation id for the current request (`-` outside a request). Echoed to clients as the `X-Request-ID` response header. |
| `message` | string | Human-readable message. |
| `exception` | string | (optional) Formatted traceback for records with exception info. |

Example line:

```json
{"time":"2026-09-08T12:00:00+0000","level":"INFO","logger":"backend.api.routers.health","request_id":"a1b2c3d4e5f6","message":"Readiness probe ok: database"}
```

### Contract rules

- Every log record is **one line** — parseable without multi-line handling
  (exception/tracebacks are embedded in the `exception` field).
- The `request_id` field is always present and matches the `X-Request-ID`
  response header of the request that produced the record.
- **Sensitive values are never logged**: API keys, session tokens, cookie
  values, or full LLM API keys must not appear in messages. Scrub before
  logging.
- Noise from HTTP client libraries (`httpx`, `httpcore`) is suppressed to
  `WARNING` by default.

### Why not tracing/metrics exporters

BERU is a single-user, single-process application for a personal machine. A
full Prometheus/OpenTelemetry stack is disproportionate to the surface. The
`/metrics` JSON endpoint + structured logs cover the operational questions
("is it up?", "did anything error?", "how often do tools run?") with zero
extra services.