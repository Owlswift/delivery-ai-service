# Session and event state machine

**Status:** implemented  
**Date:** 2026-10-16

Makes the booking workflow explicit and safe under retries, duplicate and out-of-order events, concurrent decisions, restarts and provider outages. Tasks 1-4 each changed `booking.status` directly; this task puts every change behind one machine that enforces legal transitions, records an audit trail, and owns event intake.

## Locked decisions

- **One module, `app/workflow/`**, owns: the logical states, the transition table, the audit history, the provider-event envelope and dispatcher, exclusive per-booking execution, and restart recovery. Other modules ask it to change state; they no longer assign `booking.status` themselves.
- **Two levels, one source of truth.** `BookingStatus` stays the fine-grained, API-visible status (clients already read it). The nine logical states are *derived* from it by a fixed mapping, never stored separately as input. Legality is checked on the logical states; the audit records both.
- **COURIER_ASSIGNED and cancelled are terminal** for this service. FAILED_OR_CANCELLED is split by fine status: `failed` is recoverable, `cancelled` is terminal. Nothing leaves a terminal state, ever; attempts raise `IllegalTransition` (HTTP 409) and are not applied.
- **Provider is authoritative.** Closure events from the provider (`booking_cancelled`, `request_expired`) move us to `cancelled`/`failed`; they can never regress `courier_assigned`.
- **Side effects are idempotent, state changes are compare-and-swap.** Every outbound call already carries a deterministic idempotency key (booking creation, assignment). Sessions get a `version`; a save succeeds only if the stored version is unchanged. A competing writer gets a conflict, reloads, and sees the other decision.
- **Persist before sending** (already true for creation and assignment) stays the rule, and now also gives the CAS guard: two workers cannot both record `assigning_courier` for the same booking.
- Process-local per-session locks stay as a fast path to reduce conflicts; correctness does not depend on them.

## States

| Logical state | Fine statuses (`BookingStatus`) |
| --- | --- |
| COLLECTING_DETAILS | `collecting` |
| AWAITING_FARE | `details_complete`, `address_clarification`, `budget_revision` |
| AWAITING_USER_CONFIRMATION | `awaiting_confirmation` |
| CREATING_BOOKING | `ready_to_book`, `booking_pending` |
| AWAITING_OFFERS | `booking_created` |
| AWAITING_BUDGET_APPROVAL | `offers_awaiting_approval` |
| ASSIGNING_COURIER | `assigning_courier` (new) |
| COURIER_ASSIGNED | `courier_assigned` (terminal) |
| FAILED_OR_CANCELLED | `failed` (new, recoverable), `cancelled` (terminal) |

## Transition table (logical)

A change between fine statuses inside the same logical state is always allowed. Between logical states only these are:

| From | To |
| --- | --- |
| COLLECTING_DETAILS | AWAITING_FARE, FAILED_OR_CANCELLED |
| AWAITING_FARE | COLLECTING_DETAILS, AWAITING_USER_CONFIRMATION, FAILED_OR_CANCELLED |
| AWAITING_USER_CONFIRMATION | COLLECTING_DETAILS, AWAITING_FARE, CREATING_BOOKING, FAILED_OR_CANCELLED |
| CREATING_BOOKING | AWAITING_OFFERS, AWAITING_USER_CONFIRMATION, FAILED_OR_CANCELLED |
| AWAITING_OFFERS | AWAITING_BUDGET_APPROVAL, ASSIGNING_COURIER, FAILED_OR_CANCELLED |
| AWAITING_BUDGET_APPROVAL | AWAITING_OFFERS, ASSIGNING_COURIER, FAILED_OR_CANCELLED |
| ASSIGNING_COURIER | COURIER_ASSIGNED, AWAITING_OFFERS, AWAITING_BUDGET_APPROVAL, FAILED_OR_CANCELLED |
| COURIER_ASSIGNED | none (terminal) |
| FAILED_OR_CANCELLED | only from `failed`: AWAITING_FARE (user retries; re-resolve and re-quote, user confirms again, new idempotency key). From `cancelled`: none |

Behavior the table encodes: a corrected address returns AWAITING_USER_CONFIRMATION to AWAITING_FARE / COLLECTING_DETAILS; a rejected booking returns CREATING_BOOKING to AWAITING_USER_CONFIRMATION; a rejected assignment returns ASSIGNING_COURIER to AWAITING_OFFERS (or AWAITING_BUDGET_APPROVAL when only above-budget offers remain).

`ASSIGNING_COURIER` is new: the status is set (and persisted) before the assignment request is sent, and stays set while the outcome is unknown. While it is set, no other offer may be selected; later events only reconcile the in-flight assignment. This is what prevents competing decisions.

## Audit

`booking.workflow` (new, always present):

- `state`: current logical state
- `history`: append-only list of `{seq, at, from_status, to_status, from_state, to_state, trigger, reason, event_id}`; one entry per actual status change, plus a first entry at session creation. `trigger` is `user_message`, `event:<type>`, `recovery`, or `system`; it is carried implicitly (context variable) so call sites stay simple.
- `seen_event_ids`: `{event_id: original outcome}`, bounded (latest 500)

History is part of the persisted session document and returned by `GET /v1/sessions/{id}`.

## Provider events

Envelope, all fields required except `occurred_at`:

```json
{ "event_id": "...", "booking_reference": "BK-00001", "type": "offer", "occurred_at": "...", "payload": { } }
```

Types: `offer` (payload = offer fields; replaces the old `/v1/offers/events`), `booking_cancelled`, `request_expired`. Endpoint: `POST /v1/events` (API-key, tenant-scoped; unknown reference -> 404, malformed -> 422).

Handling order:

1. Validate the envelope and the booking reference (reject, do not record).
2. **Duplicate event id** -> acknowledge `duplicate` with the original outcome; no processing.
3. **State guard** (out-of-order / late):
   - `offer`: accepted only in AWAITING_OFFERS, AWAITING_BUDGET_APPROVAL, ASSIGNING_COURIER; in a terminal or failed state -> `ignored`.
   - `booking_cancelled` / `request_expired`: ignored if COURIER_ASSIGNED or already terminal; otherwise -> `cancelled` / `failed`, pending approval cleared, user told once.
4. Dispatch to the owning module; record the event id with its outcome in the same save.
5. Return an acknowledgement `{result, notified}`.

A status-regressing event can never apply because every state change goes through the table.

## Concurrency and restarts

- `run_exclusive(store, session, fn)`: takes the in-process lock, loads, runs `fn`, saves with CAS; on a conflict reloads and runs again (bounded retries, then 409). Used by the message route and the event route. Re-running is safe because outbound calls are idempotent and the second run sees the first run's state.
- **One assignment per booking:** CAS on the `assigning_courier` checkpoint plus the deterministic assignment key. Providers must also accept at most one assignment per booking (stated in the provider contract).
- **Recovery:** all state lives in the session document. A recovery sweep (`recover_pending`) reconciles sessions left in `booking_pending` (look up by key, resend same key if absent) and `assigning_courier` (look up assignment) and informs the user only if the outcome became known. Runs at startup (setting `recover_on_startup`, default on) and is callable on demand. Messages and events also reconcile lazily, as before.
- **Provider outage:** unknown outcomes stay in their in-flight state (`booking_pending`, `assigning_courier`), never regress, and are retried by the next message, event or sweep.

## User actions

- **Cancel** (message starts with cancel / never mind / forget it): before a provider booking exists -> `cancelled`, nothing was created, nothing to undo. After creation, the reply says the request is still active and cancelling isn't supported here yet (honest; provider-side cancel is a follow-up). While `booking_pending` (outcome unknown) the same applies.
- **Message in `cancelled`:** told it was cancelled; nothing is processed.
- **Message in `failed`:** told why; "yes / try again" -> back to AWAITING_FARE, then a fresh quote and a new confirmation; booking uses a new idempotency key.
- **Response after expiry:** unchanged and re-tested: approvals revalidate the offer's expiry and price against `now`.

## Acceptance criteria -> where enforced

- No double assignment by competing decisions: `assigning_courier` checkpointed under CAS; in-flight assignment blocks other selection; deterministic keys; provider one-assignment contract.
- Duplicate events cannot repeat actions: central event-id dedup plus the offers module's content-level dedup.
- Terminal states cannot revert: transition table; `IllegalTransition`; events and messages short-circuit on terminal states.
- Every transition persisted and auditable: `workflow.history` saved with the session.
- Recovery after restart/outage: persisted in-flight states, `recover_pending`, lazy reconciliation.

## Notes from implementation

- Collection used to re-mark `details_complete` on every turn, which would have written false regressions into the audit trail (`awaiting_confirmation -> details_complete -> ready_to_book`). It now only moves a booking forward from `collecting`; later stages own their own statuses.
- `IllegalTransition` is a plain exception mapped to HTTP 409 in `main.py` (importing `AppError` into the machine created an import cycle).
- Retrying a `failed` booking resets the offers state and quote, bumps the idempotency generation, re-resolves and re-quotes, and requires a fresh confirmation before a new provider booking is created.

## Layout

```
app/workflow/
  models.py     WorkflowState, Transition, WorkflowRecord (no collection imports)
  machine.py    status->state mapping, table, transition(), trigger context, IllegalTransition, init
  events.py     ProviderEvent envelope, handle_event(), EventAck
  serialize.py  run_exclusive() (lock + compare-and-swap retry)
  recovery.py   recover_pending()
app/api/v1/events.py   POST /v1/events (replaces offers.py)
```

Store changes: `Session.version`; `save` is compare-and-swap (`ConcurrencyConflict`); `list_by_booking_status`. Existing modules replace direct `booking.status = ...` with `machine.transition(...)`. `OfferState.seen_event_ids` and `OfferEvent.event_id` are removed (dedup now lives in the machine).

## Tests

Mapping covers every `BookingStatus`; each table edge allowed and a sample of forbidden ones raises; terminal states reject every transition; history records every change with trigger; duplicate event acknowledged with original outcome and no second action; late offer after assignment/cancel ignored; cancellation/expiry events vs terminal; out-of-order closure; concurrent writers (stale session save conflicts, retry sees the other decision, one assignment); competing offer arrival while assignment in flight does not select another; user cancel before and after creation; failed -> retry -> new key; restart recovery sweep for `booking_pending` and `assigning_courier` incl. provider still down; API: `/v1/events` acks, 404/422, history in GET.

## Follow-ups

- Provider-side cancellation after creation (needs a provider `cancel` operation with reconciliation)
- Cross-process lock/queue if the single-process fast path is not enough
- Real provider adapters; webhook signature verification
