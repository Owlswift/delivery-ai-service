# SDK-as-executor architecture and provider contract v1

**Status:** implemented  
**Date:** 2026-10-16

Our service stops calling providers. It decides which provider operation is needed next and publishes it as a structured **operation**. A trusted server-side component in the provider's infrastructure (shipped as part of the provider SDK) pulls operations, executes them against the provider's own APIs, and posts the formatted result back. The workflow then resumes. The frontend SDK is a separate piece and never executes provider operations.

Supersedes the outbound-port design for production. The existing `LogisticsProvider` / `BookingProvider` / `OfferProvider` protocols stay as the internal seam: a `DeferredProvider` implements them by publishing operations and replaying with recorded results, and the old fakes remain as the `direct` provider mode for local runs and tests.

## Locked decisions

- **Three parties, three credentials.**
  1. *Frontend SDK*: sends user turns, renders replies, shows progress. Holds only a short-lived, single-session **client token**. Cannot read or complete operations, cannot create sessions, cannot send events.
  2. *Provider server component*: holds the provider secret. Creates sessions, mints client tokens, pulls operations, posts results, delivers offer and closure events. Every request is **signed** (HMAC-SHA256 over timestamp, method, path+query and body hash, five-minute window).
  3. *Our service*: owns the workflow, validates every operation and result, never holds provider credentials.
- **Operations are a closed set.** Only the five types below are ever published, each with a frozen argument schema. The SDK executes only operation types it knows, validates arguments against the published schema, and rejects anything else. Our service validates every result against the frozen result schema and cross-checks it against the arguments (for example, a fare quote must be for the requested place ids).
- **Sensitive operations are server-only.** `create_booking` and `assign_offer` are issued only to, and results accepted only from, the signed provider-server principal. The v1 contract has a single executor kind, `provider_server`; `resolve_address` and `estimate_fare` are also server-executed in v1 because a client-supplied fare or location cannot be trusted. A client-executable class can be added later without breaking the schema (the `executor` field is reserved).
- **Booking creation carries the recorded confirmation.** `create_booking` args carry `payload.confirmation = {confirmed_at, quote_id, fingerprint}` taken from the workflow's acceptance record. The operation exists only after the workflow reaches `ready_to_book`.
- **State stays in our service; operations are the only side channel.** Operations live in the session document (atomic with workflow state, CAS-protected, restart-safe). A derived index table lets the queue be queried across sessions.
- **Replay, not callbacks.** When an engine needs a provider call, `DeferredProvider` records an operation and raises `OperationPending`; the turn replies with a short "checking" message and persists. When the result arrives, the same engine code is re-run; the provider call now returns the recorded result. Engines were already idempotent and keyed (resolutions by input text, quotes by place ids, bookings and assignments by idempotency key), so replay is safe.
- **Idempotent create and assign are a contract requirement.** A repeated `create_booking` / `assign_offer` with the same idempotency key must return the original outcome. That makes "resend with the same key" the reconciliation for unknown outcomes. The envelope's `reconcile: true` tells the executor the previous attempt's outcome was unknown, so it may look the booking up first (`get_booking_status`) if its API allows.
- **Geocoding stays in our service** (server-side Google key); the provider need not return coordinates.

## Operations

| Type | Arguments | Result |
| --- | --- | --- |
| `resolve_address` | `{text}` | `AddressResolution` (`resolved` / `ambiguous` / `not_found`, locations) |
| `estimate_fare` | `{pickup, destination}` resolved locations | `FareQuote` (quote id, base fare, currency, expiry, optional route) |
| `create_booking` | `{payload}` (includes the recorded `confirmation`) + idempotency key | `ProviderBooking` `{reference, status}` |
| `assign_offer` | `{booking_reference, offer_id}` + idempotency key | `AssignmentInfo` `{offer_id, courier_id, status}` |
| `get_booking_status` | `{booking_reference}` | `BookingStatusResult` (schema frozen; not yet used by the workflow) |

Envelope handed to the executor: `operation_id`, `type`, `executor`, `provider_id`, `session_id` (opaque), `correlation_id`, `idempotency_key?`, `attempt`, `reconcile`, `timeout_s`, `created_at`, `lease_expires_at`, `args`.

Result posted back: `{attempt, outcome, result?, error?}` with `outcome` one of:

- `succeeded`: `result` required and schema-validated.
- `rejected`: definite refusal or validation failure; `error {code, message, fields?}`. Never retried.
- `unavailable`: provider could not process it at all; safe to retry with backoff.
- `unknown`: may have been processed; retried with the same key, `reconcile: true`.

Retry policy (configuration): `OPERATION_MAX_ATTEMPTS` (default 5), exponential backoff from `OPERATION_BACKOFF_BASE_S` (2) capped at `OPERATION_BACKOFF_CAP_S` (60), `OPERATION_LEASE_S` (60), `OPERATION_TIMEOUT_S` (30). When attempts are exhausted the operation becomes `exhausted`: the workflow treats it as an unknown outcome (user told it is unconfirmed, never "booked"), and a later trigger may restart it after `OPERATION_EXHAUSTED_COOLDOWN_S` (300).

## Endpoints (all provider-server, signed)

- `POST /v1/operations/lease` `{limit}`: leases up to `limit` due operations for this provider and returns envelopes. A lease hides an operation until `lease_expires_at`; an executor that crashes causes redelivery (at-least-once), so executors must honour idempotency keys.
- `POST /v1/operations/{operation_id}/result`: records the outcome and resumes the workflow in the same atomic update. A repeated post for the same `attempt` or an already-closed operation with the same outcome returns `duplicate`; a conflicting outcome is `409 operation_closed`; a malformed or inconsistent result is `422`.
- `POST /v1/events`: offer and closure events (contract below).
- `POST /v1/sessions/{id}/client-token`: (API key, server-side only) mints a client token for the frontend SDK.

Frontend endpoints (client token or API key, scoped to one session): `POST /v1/sessions/{id}/messages`, `GET /v1/sessions/{id}`. The message response shows `next_actions[0].type = "wait"` while operations are pending; new assistant messages appear on the session as results arrive.

Provider id comes from the signature headers (`X-Provider-Id`); a correlation id may be supplied (`X-Correlation-Id`) and is echoed. Operation envelopes carry their own correlation id for tracing across both sides. Timestamps on operations support measuring round-trip latency.

## Event contract changes

Event `type` is one of `offer.created`, `offer.updated`, `offer.withdrawn`, `offer.expired`, `booking.cancelled`, `booking.expired`. `event_id`, `booking_reference` and `occurred_at` are required. Offer status is derived from the type (created and updated mean open). `occurred_at` is the offer's version stamp: an older event for the same offer is ignored. Duplicate event ids are acknowledged with the original outcome. Offer payload: `offer_id`, `courier {courier_id, name?, phone?, details}`, `price`, `currency`, optional `expires_at`, `pickup_eta_min`, `pickup_distance_km`. Likes, reliability and any other courier data are opaque pass-through in `courier.details`.

Other schema changes: `FareQuote.route` becomes optional; the create-booking payload gains `confirmation`; `AssignmentInfo` gains `status`; assignment results must be for the requested `offer_id`.

## Resuming

Posting a result resumes the workflow by booking status:

- address/fare/confirmation statuses: replay the suspended quoting turn (its text, change flag and previous status are saved on the booking so a user's "yes" is not lost), then submit if the booking is now `ready_to_book`;
- `booking_pending`: reconcile creation;
- `booking_created`, `offers_awaiting_approval`, `assigning_courier`: re-evaluate offers;
- terminal, failed or cancelled: record only.

Messages produced by a resume are appended to the session as assistant messages.

## Contract freeze

The wire models (operation envelope, args, results, result request and ack, error body, event envelope and response) are exported to `docs/contract/v1/schemas.json` by `python -m scripts.export_contract`. A test fails if the models drift from the committed file, so any change to the contract is a deliberate regenerate-and-review step.

## Failure handling

| Case | Behavior |
| --- | --- |
| Executor crashes after leasing | Lease expires; redelivered; idempotency keys protect create and assign |
| Result posted twice | Second is `duplicate`; no second resume |
| Result arrives after cancel, failure or assignment | Recorded; workflow not touched |
| Result contradicts arguments (wrong place ids, wrong offer) | `422`; operation stays pending |
| `unavailable` repeatedly | Backoff, then `exhausted`; user told it is unconfirmed or retried |
| `unknown` for create/assign | Resend with same key and `reconcile: true`; never reported as success |
| User message while operations pending | Collection still runs; the turn reports "checking"; replay uses the latest suspended turn |
| Two executors lease concurrently | Only one gets each due operation (CAS plus lease) |
| Bad signature, stale timestamp, unknown provider | `401`; nothing parsed beyond headers |
| Client token for another session or expired | `401`/`404`; no operation endpoints reachable with it |

## Out of scope here

The frontend SDK and the provider-server component themselves (they live in separate repositories and consume the frozen schemas); long-polling or webhooks for lower latency; wiring `get_booking_status` into the workflow; replay protection beyond timestamp window plus idempotent operations.

## Layout

```
app/contract/        wire models exported for the freeze (re-exports, no logic)
app/operations/
  pending.py         OperationPending (leaf)
  models.py          Operation, args/results, envelope, result request/ack
  book.py            OperationBook (enqueue, latest, backoff, lease, apply result)
  deferred.py        DeferredProvider (implements the three provider protocols)
  resume.py          resume() by booking status
app/auth.py          + signature verification, client tokens
app/api/v1/operations.py   lease and result endpoints
app/api/providers.py       choose deferred or direct providers per request
scripts/export_contract.py
docs/contract/v1/schemas.json
```

Store: `operations` index table (derived on every save), `find_pending_operation_sessions`, `find_operation_session`.

## Tests

Contract freeze; deferred provider replay for each operation (pending, success, rejected, unavailable backoff, unknown with reconcile, exhausted and cooldown restart); full conversation through the API in deferred mode with a simulated executor (quote, confirm, create, offers, assign); suspended turn keeps the user's confirmation; lease exclusivity and expiry; duplicate and conflicting result posts; result schema and argument cross-checks; confirmation record in create args; signature verification (valid, tampered body, stale, unknown provider); client token scoping (messages and read only, wrong session, expired); events with new types and required timestamp, stale offers ignored; restart with pending operations; direct mode unchanged.
