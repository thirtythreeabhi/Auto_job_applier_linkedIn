# AI trace storage and integration interface

`modules.ai.trace_store` provides local, read/write trace storage without
depending on the model or LinkedIn automation code. SQLite uses WAL mode, a
10 second busy timeout, one connection per operation, and indexed run/job/time
and filter columns. By default the database is `.ai_traces.sqlite3` in the
repository root. Set `AI_TRACE_DB_PATH` to override it (also useful for tests).
The database and the existing `.ai_prompt_log.jsonl` are runtime data and must
not be committed.

`modules.ai.prompt_log.record_prompt(...)` continues to write the compatible
JSONL fields `timestamp`, `category`, `provider`, `model`, `prompt`, `response`,
and `error`. It also creates/updates a SQLite trace, adds an `llm_request`
event, and accepts optional `trace_id`, `run_id`, `job_id`, `candidate_context`,
`parsed_output`, `latency_ms`, `retry_info`, `decision`, and
`application_outcome` keyword arguments. Logging failures do not interrupt the
automation. Existing callers can continue using the original positional
arguments.

## Python interface

- `create_trace(**fields) -> trace_id`: starts a trace (`status="running"`).
- `append_event(trace_id, event_type, name, payload) -> event_id`: appends an
  ordered event. Use `llm_tool_call` only when the model actually invoked a
  tool; application operations are `workflow` events. Other supported types
  are `llm_request`, `retry`, `error`, `decision`, and `application_outcome`.
- `complete_trace(trace_id, **result_fields) -> detail`: records raw response,
  parsed output, latency, error/status, retry information, decision, or
  application outcome. An error defaults status to `error`; otherwise it is
  `completed`.
- `correlate_trace(trace_id, run_id=..., job_id=..., decision=...,
  application_outcome=...) -> detail` attaches classification and application
  results to one trace.
- `correlate_job(job_id, run_id=..., decision=...,
  application_outcome=...) -> affected_count` associates a job-level decision
  or outcome with matching traces (optionally scoped to a run).
- `list_traces(filters=..., limit=50, offset=0) -> {items,total,limit,offset}`
  returns summaries. Filters: `query`, `category`, `model`, `provider`,
  `status`, `decision`, `run_id`, `job_id`, `since`, and `until` (ISO 8601).
  Page size is clamped to 1–500.
- `get_trace(trace_id) -> detail | None` returns complete fields and ordered
  events.
- `import_legacy_prompt_log(path=None)` imports valid legacy JSONL entries once
  per source line and skips malformed lines.

## HTTP integration contract

The application integration branch can expose the following read-only routes.
No HTTP route is added by this storage change.

### `GET /api/ai-traces`

Accept optional query parameters `q`, `decision`, `model`, `category`,
`status`, `provider`, `run_id`, `job_id`, `since`, `until`, `limit`, and
`offset`. These map to the corresponding `list_traces` filters (`q` maps to
`query`). Return HTTP 200 with:

```json
{
  "items": [{
    "trace_id": "uuid",
    "run_id": "run identifier or null",
    "job_id": "LinkedIn job identifier or null",
    "timestamp": "2026-11-05T12:00:00+00:00",
    "category": "application answer",
    "provider": "openai",
    "model": "model name",
    "status": "completed",
    "latency_ms": 128,
    "error": "",
    "decision": "REVIEW",
    "application_outcome": null
  }],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

Summaries are ordered by timestamp descending, then trace ID descending.
The route adds `has_more` for pagination and `job_title`/`company` for job
grouping. It also exposes UI aliases `final_decision` (same as `decision`) and
`retry_count` (derived from the stored attempt number). The control panel's
`from`/`to` query names are accepted as aliases for `since`/`until`; its
`Other / none` decision filter returns traces with no classifier decision.

### `GET /api/ai-traces/<trace_id>`

Return HTTP 200 with the full `get_trace(trace_id)` object, including
`candidate_context`, `prompt`, `raw_response`, `parsed_output`, `retry_info`,
and `events` in ascending `sequence` order. Each event has `event_id`,
`sequence`, `timestamp`, `event_type`, `name`, and `payload`. Return HTTP 404
when the trace is missing. All stored fields are redacted before persistence;
render them as plain text in the UI.

## Tables

- `ai_traces`: one model execution with trace/run/job IDs, timestamp, category,
  provider/model, prompt/context, raw and parsed output, latency, status/error,
  retry information, classifier decision, application outcome, and update
  timestamp.
- `ai_trace_events`: append-only event rows with per-trace sequence numbers,
  type, name, timestamp, and JSON payload.
- `ai_trace_imports`: source path and content hash to make legacy imports
  idempotent.

Prompt, response, context, event payload, error, and correlation values pass
through credential/session-secret redaction before being stored. The UI and
integrating API should continue treating all stored strings as untrusted data.
