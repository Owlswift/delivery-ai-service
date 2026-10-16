# Provider integration contract v1: gap analysis

**Status:** draft; blocked on three requirements decisions (section 3)  
**Date:** 2026-10-16

The contract defines what every logistics provider must support. Our service already implements the provider side as ports (Python protocols) in `app/quoting/provider.py`, `app/booking_request/provider.py` and `app/offers/provider.py`, with fakes for tests. This document maps the contract onto those ports and lists what must change before the contract can be frozen. No code has been changed for it.

## 1. Operation coverage

| Contract operation | Current port | Status |
| --- | --- | --- |
| Resolve addresses | `LogisticsProvider.resolve_address` (per address; `resolved` / `ambiguous` / `not_found`) | Covered. Address shape undecided (decision 2). Contract says one call with both texts; we call once per address. |
| Estimate fare | `estimate_fare` (quote id, base fare, currency, expiry) | Covered, but we also use `route {distance_km, duration_min}` to present the route (Task 2). Not in the contract; add it or drop the route from the summary. |
| Create booking | `BookingProvider.create_booking(payload, key)` | Partly. Payload has quote id, budget, key. **Missing:** the recorded user confirmation (contract: "explicit user confirmation recorded by the orchestration workflow"). |
| Receive courier offers | `POST /v1/events`, type `offer` | Partly. See section 2. |
| Request assignment | `OfferProvider.assign_offer(ref, offer_id, key)` | Partly. We return `{offer_id, courier_id}`; the contract wants an assignment result **and status**. |
| Retrieve booking status | none | **Missing.** Our reconciliation uses `find_booking(idempotency_key)` and `find_assignment(reference)` instead. |

## 2. Gaps against the contract

1. **Reconciling an unknown create outcome.** If `create_booking` times out we never learn the booking reference, so "retrieve booking status by reference" cannot find it. The contract must provide one of: (a) create is idempotent and returns the existing booking for a repeated key (then a same-key resend is the reconciliation, and "do not blindly retry" is satisfied because the retry is provably safe); or (b) lookup by idempotency key / client reference. Today we do both: `find_booking(key)` first, then resend. The contract should state which one it guarantees. Recommendation: require (a) and optionally offer (b).
2. **Four response classes.** Contract: success, validation failure, unavailable service, unknown outcome. Our ports have only `Rejected` (definite refusal) and a generic error/timeout that we treat as unknown. Needed: a distinct `Unavailable` (not processed; safe to retry with backoff, no reconciliation) separate from `UnknownOutcome` (must reconcile). Also a structured validation failure (field-level) instead of a message string, so the user can be told what to correct.
3. **Provider identifier and correlation ID** on every outbound request: not modelled. We have tenant (`company_id`) from the API key but no provider id and no correlation id; neither is logged or propagated.
4. **Offer event shape.** Contract has event types `created | updated | withdrawn | expired` and a mandatory event timestamp. Ours has envelope types `offer | booking_cancelled | request_expired` and offer `status` `open | withdrawn | expired`, with `occurred_at` optional. Mapping: `created`/`updated` -> `open` offer; `withdrawn`/`expired` -> those statuses. Needed: accept the contract's types, make the timestamp required, and use it for stale detection (today only `updated_at` on the offer does that). Closure events (`booking_cancelled`, `request_expired`) are our addition and need a place in the contract.
5. **Authentication.** Inbound events use a static API key. Contract requires signed requests or equivalent strong service authentication, authorised per provider and booking. Needed: request signature verification (timestamp + HMAC or mTLS), replay window, per-provider secrets in server-side storage.
6. **Reliability behaviour not yet implemented because no real adapter exists:** bounded retries with backoff for `Unavailable`, per-provider timeouts and rate limits in configuration, secret loading. All belong in the adapter, not the workflow.
7. **Audit and logging.** The workflow history records transitions without payloads (good). There is no application logging yet; when added it must carry correlation ids and avoid phone numbers and addresses.
8. **Money.** We carry amount-as-string plus currency everywhere and reject currency mismatches; no NGN assumption in the provider ports (NGN only appears as test data and a default in the fake). Conforms.
9. **Opaque references.** `place_id`, `quote_id`, booking `reference`, `offer_id` are treated as opaque strings. Conforms.
10. **Data minimisation.** The booking payload sends recipient name/phone and sender phone, which the provider needs to deliver. Nothing else personal is sent. Conforms; keep it that way.

## 3. Decisions needed before freezing v1

1. **Offer selection data and "likes".** Task 4 settled what we use: pickup ETA (preferred) or pickup distance, within budget; likes and reliability are not scored. The contract should therefore state: `pickup_eta_min` and `pickup_distance_km` (units fixed, either may be absent), and `courier` details plus any reliability or likes data as **opaque pass-through** that we store and display but never interpret. If likes should ever affect ranking, its definition (count or rate, window, scope, missing-data rule) must be written into the contract first.
2. **Address representation. SETTLED:** coordinates are required; when the provider omits them, the service geocodes the provider's formatted address with Google Maps (see `2026-10-16-coordinates-geocoding.md`). Original proposal for the resolved location: `{place_id (opaque), formatted_address, latitude, longitude, components?}` with coordinates in decimal degrees (WGS84), and `latitude`/`longitude` required for resolved results so distance and ETA claims can be audited. Ambiguous results return a bounded list of such locations. Our current model has only `place_id` and `formatted_address`.
3. **Integration mechanism. PARTLY SETTLED:** the provider installs an SDK in its own codebase that formats provider responses for our API and calls our API at each step. Still open: who initiates provider operations (see the question in the conversation). Original recommendation: define v1 as our port and ship (a) a generic HTTP client adapter for providers that implement the JSON contract directly and (b) a connector pattern, one small server-side adapter per provider, for providers that expose different APIs. In both cases credentials stay server-side and the workflow code never changes. The decision to confirm: who builds and operates connectors, and whether providers may push events to us directly or only via their connector.

## 4. Proposed work once decisions are made

In order, each independently testable:

1. Freeze the JSON schemas for the six operations and the event envelope (this document plus the three decisions).
2. Ports: add `Unavailable` / `UnknownOutcome` / structured validation failure; add confirmation record to the create payload; add status to the assignment result; add `get_booking_status(reference)`; add coordinates to resolved addresses; carry provider id and correlation id.
3. Events: accept contract event types and required timestamp; keep closure events; signature verification and replay protection.
4. Adapter: generic HTTP adapter with timeouts, bounded backoff, rate limiting and config; logging with correlation ids and redaction.
5. Contract tests that run the same suite against the fake and any adapter.
