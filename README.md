# Delivery AI Service

Logistics-agnostic service for AI-assisted delivery booking conversations.

The service **owns conversation and booking state**. A provider's server creates sessions and mints short-lived client tokens; the frontend SDK sends only new user turns. The service never calls a provider directly: it publishes **operations** (resolve address, estimate fare, create booking, assign courier) that the provider's trusted server-side component pulls, executes against the provider's own APIs, and answers with a signed result. Voice enters as transcribed text on the message endpoint.

## Modules

| Module | Responsibility |
| --- | --- |
| `app/api` | HTTP endpoints, request validation, dependency wiring |
| `app/conversation` | messages, detail extraction and validation, follow-up questions, the per-turn entry point |
| `app/booking` | the booking document, state machine and audit history, address/fare quoting, booking creation |
| `app/providers` | provider interfaces, contract value objects, operation queue and deferred provider, geocoding, fakes |
| `app/offers` | offer validation, storage, deterministic ranking, selection and assignment |
| `app/events` | provider event envelope, deduplication, resuming after results, recovery |
| `app/persistence` | session stores, locking, optimistic concurrency, versioned migrations |
| `app/security` | API keys, signed provider requests, client tokens |
| `app/observability` | structured logs, correlation ids, metrics, audit events |

Dependency rules (data models may import each other; logic modules only downward) are enforced by `tests/test_architecture.py`. A language model, if added, lives in `conversation` and may only return extracted data or a proposed action; side effects happen only through the deterministic workflow.

## Principals and endpoints

| Who | Credential | Endpoints |
| --- | --- | --- |
| Provider server | `X-API-Key` | `POST /v1/sessions`, `POST /v1/sessions/{id}/client-token` |
| Frontend SDK | `Authorization: Bearer <client token>` (one session) | `POST /v1/sessions/{id}/messages`, `GET /v1/sessions/{id}` |
| Provider's executor | signed request (`X-Provider-Id`, `X-Timestamp`, `X-Signature`) | `POST /v1/operations/lease`, `POST /v1/operations/{id}/result`, `POST /v1/events` |
| Operations | `Authorization: Bearer <METRICS_TOKEN>` | `GET /metrics` (off unless configured) |

Also `GET /health` (open), `/docs` and `/openapi.json`.

The wire contract is frozen in [docs/contract/v1/schemas.json](docs/contract/v1/schemas.json) and checked by a test. After an intentional change run `python -m scripts.export_contract` and review the diff.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

The database is migrated automatically on start; to do it by hand: `python -m app.persistence.migrations [path]`.

## Configuration (environment variables)

| Variable | Purpose | Default |
| --- | --- | --- |
| `API_KEYS` | `key:company,...` for provider servers | `dev-key:acme` (change it) |
| `PROVIDER_SECRETS` | `provider_id:secret,...` for signed executor calls | none |
| `CLIENT_TOKEN_SECRET`, `CLIENT_TOKEN_TTL_S` | frontend token signing | none (tokens disabled), 3600 |
| `PROVIDER_MODE` | `deferred` (operations) or `direct` (in-process fakes) | `deferred` |
| `DATABASE_PATH` | SQLite file | `delivery_ai.db` |
| `GOOGLE_MAPS_API_KEY` | geocoding when a provider omits coordinates | none (reported as unavailable) |
| `OPERATION_MAX_ATTEMPTS`, `_BACKOFF_BASE_S`, `_BACKOFF_CAP_S`, `_LEASE_S`, `_TIMEOUT_S`, `_EXHAUSTED_COOLDOWN_S` | operation retry policy | 5, 2, 60, 60, 30, 300 |
| `RECOVER_ON_STARTUP` | reconcile bookings left mid-flight | `true` |
| `LOG_LEVEL`, `LOG_JSON` | logging | `INFO`, `true` |
| `METRICS_TOKEN` | enables `/metrics` | none |

Secrets are never printed in settings output or logs. Logs are JSON, carry a correlation id (`X-Correlation-Id` is accepted and echoed), and never contain message text, addresses, phone numbers, tokens or request bodies.

## Develop

```bash
pytest                 # full suite
ruff check . && ruff format .
```

Design documents, in the order they were written, are in [docs/plans/](docs/plans/) (see its README for the module names they used at the time).
