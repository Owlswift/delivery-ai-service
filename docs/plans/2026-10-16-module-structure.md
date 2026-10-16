# Module structure

**Status:** implemented  
**Date:** 2026-10-16

Reorganises the service into the nine modules from Task 7 and fills the foundation gaps (migrations, structured logging, metrics, tracing, audit events). Behaviour does not change; the existing test suite is the safety net, plus a new architecture test that enforces the dependency rules below.

## Target layout

| Module | Contents | Comes from |
| --- | --- | --- |
| `api` | HTTP routers, request validation, dependency wiring | `app/api/` |
| `conversation` | messages, detail extraction, validation, follow-up prompts, the per-turn entry point `handle_user_turn` | `collection/*`, `conversation.py`, `services/booking.py` |
| `booking` | the booking document (`BookingState` and its parts), state machine and audit history, address/fare quoting, booking creation, workflow prompts | `collection/models`, `workflow/machine|models|prompts`, `quoting/engine|prompts`, `booking_request/engine|prompts` |
| `providers` | provider interfaces and errors, contract value objects, geocoder, fakes, operation queue and deferred provider, provider selection | `quoting/provider|models|fake*`, `booking_request/provider|fake|models`, `offers/provider|fake`, `operations/*`, `api/providers.py` |
| `offers` | offer models, deterministic ranking, selection and assignment engine | `offers/*` minus provider/fake |
| `events` | provider-event envelope, deduplication and dispatch, resuming after operation results, recovery sweep | `workflow/events|recovery`, `operations/resume` |
| `persistence` | session stores, locking, compare-and-swap helper, versioned migrations | `services/*`, `workflow/serialize` |
| `security` | API keys, signed provider requests, client tokens | `auth.py` |
| `observability` | structured JSON logging, correlation context, metrics registry, audit events | new |

Also kept at the top level: `config.py`, `errors.py` (now a leaf: the error body models move into it), `schemas.py` (session and message API models), `main.py`, and `contract/` (the frozen wire schemas, which only re-export models).

## Dependency rules (enforced by `tests/test_architecture.py`)

1. **Data layer.** Every `models.py` is a data module. Data modules may import other data modules, `config`, `errors`, pydantic and the standard library, and nothing else. This is what lets the booking document compose parts owned by other modules without circular imports.
2. **Logic layer, allowed imports between modules** (data modules excluded):
   - `observability`: none
   - `security`: observability
   - `providers`: observability
   - `booking`: providers, observability
   - `offers`: booking, providers, observability
   - `events`: booking, offers, providers, persistence, observability
   - `conversation`: booking, offers, providers, observability
   - `persistence`: observability
   - `api`: everything
3. No logic cycles. Package `__init__` files stay light (no re-exports of engines), so importing a data module never drags in logic.
4. **The language model boundary.** Any future model integration lives in `conversation` and may only return an extraction or a proposed next action as data. Operational side effects stay in `booking`, `offers` and `providers`, reached only through the deterministic workflow and the state machine. The architecture test enforces that `conversation` extractors cannot import `providers`' execution paths (`deferred`, `operations.book`).

## Foundation work included

- **Migrations.** A versioned SQLite migration runner (`schema_migrations` table, ordered steps, each safe to re-run). It replaces the inline `CREATE TABLE`/`ALTER TABLE`, upgrades databases created before migrations existed, and is runnable on its own with `python -m app.persistence.migrations`.
- **Structured logging.** JSON log lines with timestamp, level, event name, correlation id, provider id, session id. A redaction layer removes values for keys such as phone, address, recipient, key, secret, signature, token and authorization, and masks phone-like strings in messages. Request and response bodies are never logged.
- **Correlation and tracing.** Middleware accepts or creates `X-Correlation-Id`, stores it in a context variable, returns it on the response, and operations carry their own correlation id. A `span` helper times named steps and records them as metrics and log events. Full OpenTelemetry export is a follow-up; the correlation id is the trace id.
- **Metrics.** A small in-process registry (counters and histograms with labels), rendered in Prometheus text format at `GET /metrics`, protected by `METRICS_TOKEN` (disabled when unset). Instrumented: HTTP requests and latency, state transitions, operation lifecycle and queue latency, event outcomes, authentication failures, provider errors.
- **Audit events.** State transitions, operation lease/completion, event handling and authentication failures emit audit log events containing identifiers and outcomes only.
- **Operation pruning.** Superseded completed operations for the same key are dropped when a newer one is queued, so a session record does not grow without bound.
- **Tooling.** ruff configuration and, if it can be installed, a clean run.

## Notes from implementation

- Redaction lives in `observability` (not `security`): logging needs it and `observability` may not depend on anything. Configuration secrets are `SecretStr`, so settings output never shows them.
- `resume` and `recovery` moved to `events` rather than `booking`: they re-enter offer logic, and `booking` may not depend on `offers`. The per-turn entry point `handle_user_turn` lives in `conversation` for the same reason.
- `parse_budget` moved to `booking/money.py` because both detail validation (`conversation`) and budget negotiation (`offers`) need it.
- A migration step now starts its own transaction: Python's `sqlite3` does not open one for DDL, so a failing step would otherwise not roll back (found by a test).
- Route labels use the matched path with parameter values replaced by their names (`/v1/sessions/{session_id}`), and `unmatched` for anything else, so metric cardinality stays bounded.

## Not changing

Behaviour, API paths, wire schemas (the frozen contract must still match), and workflow semantics. The hold window for offers, provider-side cancellation and an LLM extractor remain follow-ups.

## Order of work

1. Move files and rewrite imports, splitting the five mixed `models.py`/`provider.py` modules by ownership. Run the suite after each group.
2. Add the architecture test and fix any violation by moving code, not by loosening the rules.
3. Persistence migrations, then security/observability modules and their instrumentation.
4. Operation pruning, tooling, README.
5. Full run, contract check, commit.
