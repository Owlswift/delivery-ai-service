import itertools

import pytest
from fastapi.testclient import TestClient

from app.booking.machine import (
    ALLOWED,
    STATE_OF,
    IllegalTransition,
    can_transition,
    is_terminal,
    transition,
)
from app.booking.models import BookingState, BookingStatus, WorkflowState
from app.events.handler import handle_event
from app.events.models import ProviderEvent
from app.events.recovery import recover_pending
from app.main import create_app
from app.persistence.serialize import run_exclusive
from app.persistence.session_store import ConcurrencyConflict, InMemorySessionStore
from app.persistence.sqlite_store import SqliteSessionStore
from tests.test_booking_request import confirmed
from tests.test_offers import POLICY, Desk
from tests.test_quoting import T0

S = WorkflowState


def closure(d: Desk, kind: str, event_id: str, reason: str | None = None):
    payload = {"reason": reason} if reason else {}
    kind = {"booking_cancelled": "booking.cancelled", "request_expired": "booking.expired"}[kind]
    ev = ProviderEvent(
        event_id=event_id, booking_reference=d.ref, type=kind, occurred_at=T0, payload=payload
    )
    return handle_event(d.session, ev, d.op, POLICY, d.clock)


def statuses(d: Desk) -> list[str]:
    return [t.to_status for t in d.b.workflow.history]


# ---- table -----------------------------------------------------------------
def test_every_status_maps_to_a_logical_state() -> None:
    assert set(STATE_OF) == set(BookingStatus)
    assert set(STATE_OF.values()) == set(WorkflowState)


def test_table_edges_and_terminal_rules() -> None:
    for a, b in itertools.product(BookingStatus, repeat=2):
        if a == b:
            continue
        allowed = can_transition(a, b)
        if is_terminal(a):
            assert not allowed, (a, b)
        elif STATE_OF[a] == STATE_OF[b]:
            assert allowed
        elif a == BookingStatus.failed:
            assert allowed == (STATE_OF[b] == S.AWAITING_FARE)
        else:
            assert allowed == (STATE_OF[b] in ALLOWED[STATE_OF[a]]), (a, b)
    assert not can_transition(BookingStatus.courier_assigned, BookingStatus.booking_created)
    assert not can_transition(BookingStatus.cancelled, BookingStatus.collecting)
    assert can_transition(BookingStatus.failed, BookingStatus.details_complete)
    assert not can_transition(BookingStatus.failed, BookingStatus.booking_created)
    assert not can_transition(BookingStatus.collecting, BookingStatus.courier_assigned)


def test_illegal_transition_is_refused_and_not_recorded() -> None:
    b = BookingState(status=BookingStatus.courier_assigned)
    with pytest.raises(IllegalTransition):
        transition(b, BookingStatus.booking_created)
    assert b.status == BookingStatus.courier_assigned and b.workflow.history == []


def test_illegal_transition_maps_to_http_409() -> None:
    app = create_app()

    @app.get("/boom")
    def boom():
        raise IllegalTransition("courier_assigned", "collecting")

    r = TestClient(app).get("/boom")
    assert r.status_code == 409 and r.json()["error"]["code"] == "illegal_transition"


# ---- audit -----------------------------------------------------------------
def test_history_records_every_change_in_order_without_flapping() -> None:
    d = Desk()
    d.offer("o1", price="6000", eta=4)
    assert statuses(d) == [
        "collecting",
        "details_complete",
        "awaiting_confirmation",
        "ready_to_book",
        "booking_pending",
        "booking_created",
        "assigning_courier",
        "courier_assigned",
    ]
    history = d.b.workflow.history
    assert [t.seq for t in history] == list(range(len(history)))
    assert history[0].trigger == "session_created" and history[0].from_status is None
    assert all(t.trigger == "user_message" for t in history[1:6])
    assert history[-1].trigger == "event:offer.created" and history[-1].event_id
    assert history[-1].to_state == S.COURIER_ASSIGNED and d.b.workflow.state == S.COURIER_ASSIGNED


def test_unchanged_turns_add_no_history() -> None:
    d = Desk()
    n = len(d.b.workflow.history)
    d.msg("any update?")
    d.msg("still waiting?")
    assert len(d.b.workflow.history) == n


def test_history_survives_sqlite_round_trip(tmp_path) -> None:
    d = Desk()
    d.offer("o1", eta=4)
    store = SqliteSessionStore(str(tmp_path / "w.db"))
    store.create(d.session)
    loaded = SqliteSessionStore(str(tmp_path / "w.db")).get(d.session.id, "acme")
    assert [t.to_status for t in loaded.booking.workflow.history] == statuses(d)


# ---- terminal states -------------------------------------------------------
def test_assigned_booking_cannot_regress() -> None:
    d = Desk()
    d.offer("o1", eta=4)
    n = len(d.b.workflow.history)
    assert d.offer("o2", eta=1).outcome == "ignored"
    assert closure(d, "booking_cancelled", "c1").outcome == "ignored"
    assert closure(d, "request_expired", "x1").outcome == "ignored"
    d.msg("cancel")
    d.msg("raise my budget to 90000")
    assert d.b.status == BookingStatus.courier_assigned
    assert len(d.b.workflow.history) == n and len(d.op.assign_calls) == 1


def test_cancelled_booking_never_reactivates() -> None:
    d = Desk()
    closure(d, "booking_cancelled", "c1", "customer called")
    assert d.b.status == BookingStatus.cancelled
    assert d.offer("late", eta=1).outcome == "ignored"
    assert "cancelled" in d.msg("yes, try again")
    assert "cancelled" in d.msg("actually pickup: 1 Allen Avenue, Ikeja")
    assert d.b.status == BookingStatus.cancelled and d.op.assign_calls == []


# ---- user cancellation -----------------------------------------------------
def test_user_cancel_before_creation_creates_nothing() -> None:
    f = confirmed()
    reply = f.say("cancel")
    assert "cancelled" in reply and f.b.status == BookingStatus.cancelled
    f.say("yes")
    assert f.bp.create_calls == [] and f.b.status == BookingStatus.cancelled


def test_user_cancel_after_creation_is_honest_and_changes_nothing() -> None:
    d = Desk()
    reply = d.msg("cancel please")
    assert "isn't supported" in reply and d.b.status == BookingStatus.booking_created


# ---- provider closure events ----------------------------------------------
def test_provider_cancel_and_expiry() -> None:
    d = Desk()
    d.offer("o1", price="9000", eta=2)  # awaiting approval
    ack = closure(d, "booking_cancelled", "c1", "provider closed")
    assert ack.outcome == "cancelled" and "provider closed" in ack.message
    assert d.b.status == BookingStatus.cancelled and d.b.offers.pending_approval is None
    assert closure(d, "booking_cancelled", "c1").outcome == "duplicate"

    e = Desk()
    ack = closure(e, "request_expired", "x1")
    assert ack.outcome == "failed" and "try again" in ack.message
    assert e.b.status == BookingStatus.failed
    assert closure(e, "request_expired", "x2").outcome == "ignored"


def test_failed_booking_can_retry_with_new_confirmation_and_new_key() -> None:
    d = Desk()
    first_key = d.b.creation.request.idempotency_key
    closure(d, "request_expired", "x1")
    assert "try again" in d.msg("hello")
    reply = d.msg("try again")
    assert d.b.status == BookingStatus.awaiting_confirmation and "confirm" in reply
    assert len(d.bp.bookings) == 1  # nothing re-sent before the user confirms
    d.msg("yes")
    assert d.b.status == BookingStatus.booking_created
    assert d.b.creation.generation == 1 and d.b.creation.request.idempotency_key != first_key
    assert len(d.bp.bookings) == 2 and d.b.offers is None
    assert (
        d.offer("fresh", price="6000", eta=3, reference=d.b.creation.request.reference).outcome
        == "stored"
    )


def test_offer_to_failed_booking_is_ignored() -> None:
    d = Desk()
    closure(d, "request_expired", "x1")
    assert d.offer("late", eta=1).outcome == "ignored"


# ---- events: duplicates and unknown references -----------------------------
def test_duplicate_event_returns_original_outcome_and_does_nothing() -> None:
    d = Desk()
    first = d.offer("o1", price="9000", eta=3, event_id="same")
    again = d.offer("o1", price="9000", eta=3, event_id="same")
    assert again.outcome == "duplicate" and again.original == first.outcome
    assert again.message is None


def test_event_id_memory_is_bounded() -> None:
    d = Desk()
    for i in range(520):
        d.offer("o1", price="9000", eta=3, event_id=f"id{i}")
    assert len(d.b.workflow.seen_event_ids) == 500


# ---- concurrency -----------------------------------------------------------
def test_stale_save_is_rejected() -> None:
    store = InMemorySessionStore()
    d = Desk()
    store.create(d.session)
    a = store.get(d.session.id, "acme")
    b = store.get(d.session.id, "acme")
    store.save(a)
    with pytest.raises(ConcurrencyConflict):
        store.save(b)
    assert b.version == 0  # not advanced by the failed save


def test_competing_decisions_yield_one_assignment() -> None:
    store = InMemorySessionStore()
    d = Desk()
    store.create(d.session)
    sid = d.session.id

    def ev(offer_id, event_id):
        return ProviderEvent(
            event_id=event_id,
            booking_reference=d.ref,
            type="offer.created",
            occurred_at=T0,
            payload=dict(
                offer_id=offer_id,
                courier=dict(courier_id=f"c-{offer_id}"),
                price="6000",
                currency="NGN",
                pickup_eta_min=3,
            ),
        )

    def apply(event):
        def fn(session):
            return handle_event(
                session, event, d.op, POLICY, d.clock, checkpoint=lambda: store.save(session)
            )

        return fn

    raced = {"done": False}

    def worker_b(session):
        if not raced["done"]:
            raced["done"] = True
            run_exclusive(store, sid, "acme", apply(ev("o1", "ea")))  # A commits first
        return apply(ev("o2", "eb"))(session)

    ack = run_exclusive(store, sid, "acme", worker_b)
    final = store.get(sid, "acme")
    assert ack.outcome == "ignored"  # B re-ran on A's result: booking already assigned
    assert final.booking.offers.assignment.offer_id == "o1"
    assert [c[0] for c in d.op.assign_calls] == ["o1"]  # B never reached the provider


def test_offer_during_inflight_assignment_does_not_select_another() -> None:
    d = Desk(["timeout_after"])
    d.op.find_fails = True
    d.offer("o1", eta=5)
    assert d.b.status == BookingStatus.assigning_courier
    d.offer("o2", eta=1)  # better, but a decision is already in flight
    assert [c[0] for c in d.op.assign_calls] == ["o1"]
    assert d.b.status == BookingStatus.assigning_courier
    d.op.find_fails = False
    d.msg("status?")
    assert d.b.status == BookingStatus.courier_assigned
    assert d.b.offers.assignment.offer_id == "o1"


def test_run_exclusive_gives_up_after_repeated_conflicts() -> None:
    store = InMemorySessionStore()
    d = Desk()
    store.create(d.session)

    def always_loses(session):
        fresh = store.get(session.id, "acme")
        store.save(fresh)  # someone else commits every time
        return 1

    from app.errors import AppError

    with pytest.raises(AppError) as err:
        run_exclusive(store, d.session.id, "acme", always_loses, retries=3)
    assert err.value.status_code == 409


# ---- recovery --------------------------------------------------------------
def test_recovery_resolves_pending_booking_and_tells_user() -> None:
    from tests.test_booking_request import Flow
    from tests.test_quoting import blob

    f = Flow(["timeout_after"])
    f.bp.find_fails = True
    f.say(blob())
    f.say("yes")
    assert f.b.status == BookingStatus.booking_pending
    store = InMemorySessionStore()
    store.create(f.session)

    f.bp.find_fails = True  # provider still down: nothing changes, nothing sent
    out = recover_pending(store, f.bp, None, POLICY, f.clock)
    assert [(r.before, r.after) for r in out] == [("booking_pending", "booking_pending")]
    assert len(f.bp.create_calls) == 1

    f.bp.find_fails = False
    out = recover_pending(store, f.bp, None, POLICY, f.clock)
    assert out[0].after == "booking_created"
    saved = store.get(f.session.id, "acme")
    assert saved.messages[-1].role == "assistant" and "BK-" in saved.messages[-1].content
    assert len(f.bp.create_calls) == 1 and len(f.bp.bookings) == 1
    assert saved.booking.workflow.history[-1].trigger == "recovery"


def test_recovery_resolves_in_flight_assignment() -> None:
    d = Desk(["timeout_after"])
    d.op.find_fails = True
    d.offer("o1", eta=5)
    store = InMemorySessionStore()
    store.create(d.session)
    d.op.find_fails = False
    out = recover_pending(store, d.bp, d.op, POLICY, d.clock)
    assert out[0].before == "assigning_courier" and out[0].after == "courier_assigned"
    assert "assigned" in store.get(d.session.id, "acme").messages[-1].content
    assert len(d.op.assign_calls) == 1


def test_recovery_runs_at_startup(tmp_path, monkeypatch) -> None:
    from app.api.v1 import sessions as sessions_module
    from app.config import get_settings
    from tests.test_booking_request import Flow
    from tests.test_quoting import blob

    path = str(tmp_path / "boot.db")
    f = Flow(["timeout_before"])
    f.say(blob())
    f.say("yes")
    assert f.b.status == BookingStatus.booking_pending
    SqliteSessionStore(path).create(f.session)

    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("RECOVER_ON_STARTUP", "true")
    monkeypatch.setenv("PROVIDER_MODE", "direct")
    get_settings.cache_clear()
    monkeypatch.setattr(sessions_module, "_store", None)
    with TestClient(create_app()):
        pass
    recovered = SqliteSessionStore(path).get(f.session.id, "acme")
    assert recovered.booking.status == BookingStatus.booking_created
    get_settings.cache_clear()


# ---- API -------------------------------------------------------------------
def test_api_history_and_events(client) -> None:
    h = {"X-API-Key": "dev-key"}
    from tests.test_quoting import blob

    sid = client.post("/v1/sessions", json={"sender_phone": "+2348030000000"}, headers=h).json()[
        "id"
    ]
    url = f"/v1/sessions/{sid}/messages"
    client.post(url, json={"content": blob()}, headers=h)
    ref = client.post(url, json={"content": "yes"}, headers=h).json()["session"]["booking"][
        "result"
    ]["reference"]
    from tests.signing import signed

    ev = {
        "event_id": "c1",
        "booking_reference": ref,
        "type": "booking.cancelled",
        "occurred_at": "2026-10-16T12:00:00Z",
        "payload": {"reason": "ops"},
    }
    assert signed(client, "POST", "/v1/events", ev).json()["result"] == "cancelled"
    assert signed(client, "POST", "/v1/events", ev).json()["result"] == "duplicate"
    got = client.get(f"/v1/sessions/{sid}", headers=h).json()
    history = got["booking"]["workflow"]["history"]
    assert (
        history[-1]["to_status"] == "cancelled"
        and history[-1]["trigger"] == "event:booking.cancelled"
    )
    assert got["booking"]["workflow"]["state"] == "FAILED_OR_CANCELLED"
    assert got["version"] >= 1
    unknown = {**ev, "event_id": "c2", "type": "bogus"}
    assert signed(client, "POST", "/v1/events", unknown).status_code == 422
