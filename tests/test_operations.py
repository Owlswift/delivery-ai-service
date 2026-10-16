import time
from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.conversation.engine import start_collection
from app.persistence.sqlite_store import SqliteSessionStore
from app.providers.fake_logistics import FakeLogisticsProvider
from app.providers.models import ResolvedAddress
from app.providers.operations.book import InvalidResult, OperationBook, OpsConfig
from app.providers.operations.deferred import DeferredProvider
from app.providers.operations.models import OperationResultRequest, OperationStatus
from app.providers.operations.pending import OperationPending
from app.providers.protocols import (
    BookingRejected,
    OfferTimeout,
    OfferUnavailable,
    ProviderError,
)
from tests.executor import Executor
from tests.signing import signed
from tests.test_quoting import blob

H = {"X-API-Key": "dev-key"}
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
CFG = OpsConfig(
    max_attempts=3, backoff_base_s=2, backoff_cap_s=60, lease_s=60, exhausted_cooldown_s=300
)


# ---- unit: the deferred provider over recorded operations -------------------------
def deferred(booking=None, now=NOW, cfg=CFG):
    booking = booking or start_collection("+2348030000000")
    return DeferredProvider(booking, now, cfg), booking


def finish(provider, op_id, outcome, attempt=None, **kw):
    op = provider.book.booking.ops[op_id]
    req = OperationResultRequest(attempt=attempt or op.attempts + 1, outcome=outcome, **kw)
    return provider.book.apply(op, req)


def pending_id(provider, call):
    with pytest.raises(OperationPending) as err:
        call()
    return err.value.operation_id


def good_resolution(text):
    return FakeLogisticsProvider().resolve_address(text).model_dump(mode="json")


def test_read_operation_queues_then_replays_once() -> None:
    p, booking = deferred()
    op_id = pending_id(p, lambda: p.resolve_address("12 King Street, Ikeja"))
    assert (
        pending_id(p, lambda: p.resolve_address("12 King Street, Ikeja")) == op_id
    )  # no duplicate
    finish(p, op_id, "succeeded", result=good_resolution("12 King Street, Ikeja"))
    assert p.resolve_address("12 King Street, Ikeja").status == "resolved"
    pending_id(p, lambda: p.resolve_address("12 King Street, Ikeja"))  # consumed: asks again
    # the finished first operation was pruned when its successor was queued
    assert [o.seq for o in booking.ops.values()] == [2]


def test_read_operation_failure_is_an_error_then_asks_again() -> None:
    p, _ = deferred()
    op_id = pending_id(p, lambda: p.resolve_address("x y z"))
    finish(p, op_id, "rejected", error={"code": "bad", "message": "nope"})
    with pytest.raises(ProviderError):
        p.resolve_address("x y z")
    pending_id(p, lambda: p.resolve_address("x y z"))


def test_booking_creation_is_idempotent_and_memoized() -> None:
    from tests.test_booking_request import confirmed

    f = confirmed()
    f.say("yes")
    real = next(iter(f.bp.payloads.values()))
    p, _ = deferred()
    op_id = pending_id(p, lambda: p.create_booking(real, "key-1"))
    assert pending_id(p, lambda: p.create_booking(real, "key-1")) == op_id
    assert p.find_booking("key-1") is None
    finish(p, op_id, "succeeded", result={"reference": "BK-9", "status": "awaiting_offers"})
    assert p.create_booking(real, "key-1").reference == "BK-9"
    assert p.create_booking(real, "key-1").reference == "BK-9"  # repeatable
    assert p.find_booking("key-1").reference == "BK-9"
    assert len(p.book.booking.ops) == 1


def test_rejection_is_definite_and_repeatable() -> None:
    from tests.test_booking_request import confirmed

    f = confirmed()
    f.say("yes")
    real = next(iter(f.bp.payloads.values()))
    p, _ = deferred()
    op_id = pending_id(p, lambda: p.create_booking(real, "k"))
    finish(p, op_id, "rejected", error={"code": "area", "message": "not serviceable"})
    for _ in range(2):
        with pytest.raises(BookingRejected, match="not serviceable"):
            p.create_booking(real, "k")


def test_unavailable_and_unknown_back_off_then_exhaust_then_restart_after_cooldown() -> None:
    p, booking = deferred()
    op_id = pending_id(p, lambda: p.assign_offer("BK-1", "o1", "akey"))
    op = booking.ops[op_id]

    finish(p, op_id, "unavailable")
    assert op.status == OperationStatus.pending and op.attempts == 1
    assert op.available_at == NOW + timedelta(seconds=2) and not op.reconcile
    finish(p, op_id, "unknown")
    assert op.available_at == NOW + timedelta(seconds=4) and op.reconcile  # exponential, reconcile
    finish(p, op_id, "unknown")
    assert op.status == OperationStatus.exhausted and op.attempts == 3

    with pytest.raises(OfferTimeout):  # exhausted: reported as an unknown outcome
        p.assign_offer("BK-1", "o1", "akey")
    later, _ = deferred(booking, now=NOW + timedelta(seconds=301))
    assert pending_id(later, lambda: later.assign_offer("BK-1", "o1", "akey")) == op_id
    assert op.status == OperationStatus.pending and op.attempts == 0 and op.reconcile
    assert op.idempotency_key == "akey"  # same key: resending is the reconciliation


def test_assignment_rejection_maps_to_unavailable_offer() -> None:
    p, _ = deferred()
    op_id = pending_id(p, lambda: p.assign_offer("BK-1", "o1", "k"))
    finish(p, op_id, "rejected", error={"code": "taken", "message": "gone"})
    with pytest.raises(OfferUnavailable):
        p.assign_offer("BK-1", "o1", "k")
    assert p.find_assignment("BK-1") is None


def test_stale_attempt_number_is_ignored() -> None:
    p, booking = deferred()
    op_id = pending_id(p, lambda: p.assign_offer("BK-1", "o1", "k"))
    finish(p, op_id, "unavailable")
    assert finish(p, op_id, "unavailable", attempt=1) == "duplicate"  # repeat of attempt 1
    assert booking.ops[op_id].attempts == 1


def test_results_are_checked_against_the_schema_and_the_arguments() -> None:
    p, booking = deferred()
    pickup = ResolvedAddress(place_id="a", formatted_address="A", latitude=1, longitude=1)
    dest = ResolvedAddress(place_id="b", formatted_address="B", latitude=2, longitude=2)
    est = pending_id(p, lambda: p.estimate_fare(pickup, dest))
    quote = dict(
        quote_id="q",
        base_fare="5000",
        currency="NGN",
        expires_at=NOW.isoformat(),
        pickup_place_id="a",
        destination_place_id="b",
    )
    with pytest.raises(InvalidResult):
        finish(p, est, "succeeded", result={**quote, "destination_place_id": "other"})
    with pytest.raises(InvalidResult):
        finish(p, est, "succeeded")  # missing result
    with pytest.raises(InvalidResult):
        finish(p, est, "succeeded", result={"quote_id": "q"})  # schema
    assert booking.ops[est].status == OperationStatus.pending
    finish(p, est, "succeeded", result=quote)
    assert p.estimate_fare(pickup, dest).base_fare == "5000"

    asg = pending_id(p, lambda: p.assign_offer("BK-1", "o1", "k"))
    with pytest.raises(InvalidResult):
        finish(p, asg, "succeeded", result={"offer_id": "o2", "courier_id": "c"})


def test_lease_hides_an_operation_until_it_expires() -> None:
    p, booking = deferred()
    op_id = pending_id(p, lambda: p.resolve_address("a b c"))
    op = booking.ops[op_id]
    book = OperationBook(booking, NOW, CFG)
    assert book.is_due(op)
    env = book.lease(op, "acme", "sess")
    assert env.attempt == 1 and env.lease_expires_at == NOW + timedelta(seconds=60)
    assert not book.is_due(op)
    assert OperationBook(booking, NOW + timedelta(seconds=61), CFG).is_due(op)  # redelivery


# ---- API: the whole conversation in deferred mode, driven by a simulated executor ----
def new_session(api):
    return api.post("/v1/sessions", json={"sender_phone": "+2348030000000"}, headers=H).json()["id"]


def say(api, sid, text, headers=H):
    return api.post(f"/v1/sessions/{sid}/messages", json={"content": text}, headers=headers)


def booking_of(api, sid):
    return api.get(f"/v1/sessions/{sid}", headers=H).json()["booking"]


def assistant_texts(api, sid):
    return [
        m["content"]
        for m in api.get(f"/v1/sessions/{sid}", headers=H).json()["messages"]
        if m["role"] == "assistant"
    ]


def test_full_conversation_runs_through_operations(api) -> None:
    ex = Executor(api)
    sid = new_session(api)

    reply = say(api, sid, blob()).json()
    assert "checking" in reply["message"]["content"]
    assert reply["session"]["booking"]["next_actions"][0]["type"] == "wait"
    first = ex.lease()
    assert [e["type"] for e in first] == ["resolve_address"]  # one at a time
    ex.post(first[0], ex.execute(first[0]))
    ex.run()
    assert booking_of(api, sid)["status"] == "awaiting_confirmation"
    assert "Base fare: 5000 NGN" in assistant_texts(api, sid)[-1]
    assert [e["type"] for e in ex.executed] == [
        "resolve_address",
        "estimate_fare",
    ]  # after the first, leased by hand

    say(api, sid, "yes")
    ex.run()
    booking = booking_of(api, sid)
    assert booking["status"] == "booking_created"
    create = next(e for e in ex.executed if e["type"] == "create_booking")
    confirmation = create["args"]["payload"]["confirmation"]
    assert confirmation["quote_id"] and confirmation["fingerprint"] and confirmation["confirmed_at"]
    assert create["idempotency_key"] and create["executor"] == "provider_server"
    assert "created" in assistant_texts(api, sid)[-1]
    ref = booking["result"]["reference"]

    event = {
        "event_id": "e1",
        "booking_reference": ref,
        "type": "offer.created",
        "occurred_at": "2026-10-16T12:00:00Z",
        "payload": {
            "offer_id": "o1",
            "courier": {"courier_id": "c1", "name": "Ife"},
            "price": "6000",
            "currency": "NGN",
            "pickup_eta_min": 7,
        },
    }
    assert signed(api, "POST", "/v1/events", event).json()["result"] == "stored"
    assign = ex.lease()
    assert [e["type"] for e in assign] == ["assign_offer"] and assign[0]["idempotency_key"]
    assert booking_of(api, sid)["status"] == "assigning_courier"
    ex.post(assign[0], ex.execute(assign[0]))
    final = booking_of(api, sid)
    assert final["status"] == "courier_assigned"
    assert "A courier has been assigned" in assistant_texts(api, sid)[-1]
    assert [t["to_status"] for t in final["workflow"]["history"]][-1] == "courier_assigned"


def test_messages_while_pending_do_not_duplicate_operations(api) -> None:
    sid = new_session(api)
    say(api, sid, blob())
    say(api, sid, "hello?")
    ex = Executor(api)
    assert len(ex.lease()) == 1
    ops = [o for o in booking_of(api, sid)["ops"].values()]
    assert len(ops) == 1 and ops[0]["status"] == "pending"


def test_users_confirmation_survives_a_requote(api) -> None:
    ex = Executor(
        api, logistics=FakeLogisticsProvider(ttl=timedelta(seconds=-5))
    )  # already expired
    sid = new_session(api)
    say(api, sid, blob())
    ex.run()
    assert booking_of(api, sid)["status"] == "awaiting_confirmation"
    reply = say(api, sid, "yes").json()  # quote is stale: needs a fresh estimate first
    assert "checking" in reply["message"]["content"]
    assert booking_of(api, sid)["suspended_turn"]["text"] == "yes"
    ex.logistics.ttl = timedelta(minutes=10)
    ex.run()
    booking = booking_of(api, sid)
    assert booking["status"] == "booking_created"  # "yes" applied to the unchanged fare
    assert booking["suspended_turn"] is None
    assert [e["type"] for e in ex.executed].count("create_booking") == 1


def test_changed_fare_after_requote_asks_again_and_books_nothing(api) -> None:
    ex = Executor(api, logistics=FakeLogisticsProvider(ttl=timedelta(seconds=-5)))
    sid = new_session(api)
    say(api, sid, blob())
    ex.run()
    say(api, sid, "yes")
    ex.logistics.ttl = timedelta(minutes=10)
    ex.logistics.fare = "6500"
    ex.run()
    assert booking_of(api, sid)["status"] == "awaiting_confirmation"
    assert "changed from 5000 NGN to 6500 NGN" in assistant_texts(api, sid)[-1]
    assert not any(e["type"] == "create_booking" for e in ex.executed)


def test_provider_rejection_of_creation_is_explained_without_claiming_a_booking(api) -> None:
    ex = Executor(api)
    ex.bookings.script = ["reject"]
    sid = new_session(api)
    say(api, sid, blob())
    ex.run()
    say(api, sid, "yes")
    ex.run()
    booking = booking_of(api, sid)
    assert booking["status"] == "awaiting_confirmation"
    assert "No booking was created" in assistant_texts(api, sid)[-1]
    assert ex.bookings.bookings == {}


def test_unknown_creation_outcome_is_retried_with_the_same_key_and_reconcile(api) -> None:
    ex = Executor(api)
    ex.bookings.script = ["timeout_after"]  # booking exists at the provider, response lost
    sid = new_session(api)
    say(api, sid, blob())
    ex.run()
    say(api, sid, "yes")
    ex.run()
    creates = [e for e in ex.executed if e["type"] == "create_booking"]
    assert [c["attempt"] for c in creates] == [1, 2] and [c["reconcile"] for c in creates] == [
        False,
        True,
    ]
    assert creates[0]["idempotency_key"] == creates[1]["idempotency_key"]
    assert booking_of(api, sid)["status"] == "booking_created"
    assert len(ex.bookings.bookings) == 1


def test_exhausted_creation_is_never_reported_as_booked(api, monkeypatch) -> None:
    monkeypatch.setenv("OPERATION_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    ex = Executor(api)
    ex.bookings.script = ["timeout_before", "timeout_before"]
    sid = new_session(api)
    say(api, sid, blob())
    ex.run()
    say(api, sid, "yes")
    ex.run()
    booking = booking_of(api, sid)
    assert booking["status"] == "booking_pending"
    assert "can't say it's booked" in assistant_texts(api, sid)[-1]
    assert ex.bookings.bookings == {}


def test_result_after_cancellation_is_recorded_but_changes_nothing(api) -> None:
    sid = new_session(api)
    say(api, sid, blob())
    say(api, sid, "cancel")
    assert booking_of(api, sid)["status"] == "cancelled"
    ex = Executor(api)
    n = len(assistant_texts(api, sid))
    ex.run()
    assert booking_of(api, sid)["status"] == "cancelled"
    assert len(assistant_texts(api, sid)) == n


def test_duplicate_conflicting_and_invalid_result_posts(api) -> None:
    sid = new_session(api)
    say(api, sid, blob())
    ex = Executor(api)
    env = ex.lease()[0]
    outcome = ex.execute(env)
    first = ex.post(env, outcome).json()
    assert first["duplicate"] is False
    again = ex.post(env, outcome)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    conflict = ex.post(env, {"outcome": "rejected", "error": {"code": "x", "message": "y"}})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "operation_closed"

    nxt = ex.lease()[0]
    bad = ex.post(nxt, {"outcome": "succeeded", "result": {"status": "resolved"}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_result"
    assert (
        signed(
            api, "POST", "/v1/operations/nope/result", {"attempt": 1, "outcome": "unknown"}
        ).status_code
        == 404
    )


def test_lease_exclusivity_and_tenant_scoping(api, monkeypatch) -> None:
    sid = new_session(api)
    say(api, sid, blob())
    assert len(Executor(api).lease()) == 1
    assert Executor(api).lease() == []  # leased: hidden from a second executor
    assert Executor(api, provider="beta").lease() == []  # another provider sees nothing
    env_id = next(iter(booking_of(api, sid)["ops"]))
    other = signed(
        api,
        "POST",
        f"/v1/operations/{env_id}/result",
        {"attempt": 1, "outcome": "unknown"},
        provider="beta",
    )
    assert other.status_code == 404


# ---- security ---------------------------------------------------------------------
def test_signature_checks(api) -> None:
    body = {"limit": 1}
    assert signed(api, "POST", "/v1/operations/lease", body).status_code == 200
    assert signed(api, "POST", "/v1/operations/lease", body, tamper=True).status_code == 401
    assert (
        signed(api, "POST", "/v1/operations/lease", body, ts=time.time() - 3600).status_code == 401
    )
    assert signed(api, "POST", "/v1/operations/lease", body, secret="wrong").status_code == 401
    assert signed(api, "POST", "/v1/operations/lease", body, provider="ghost").status_code == 401
    assert (
        api.post("/v1/operations/lease", json=body, headers=H).status_code == 401
    )  # API key is not enough
    assert api.post("/v1/events", json={}, headers=H).status_code == 401


def test_client_token_is_scoped_to_one_session_and_to_messages_and_reads(api) -> None:
    sid, other = new_session(api), new_session(api)
    minted = api.post(f"/v1/sessions/{sid}/client-token", headers=H)
    token = minted.json()["token"]
    bearer = {"Authorization": f"Bearer {token}"}
    assert say(api, sid, "hello", headers=bearer).status_code == 200
    assert api.get(f"/v1/sessions/{sid}", headers=bearer).status_code == 200
    assert api.get(f"/v1/sessions/{other}", headers=bearer).status_code == 401
    assert say(api, other, "hello", headers=bearer).status_code == 401
    assert (
        api.post(
            "/v1/sessions", json={"sender_phone": "+2348030000000"}, headers=bearer
        ).status_code
        == 401
    )
    assert api.post(f"/v1/sessions/{sid}/client-token", headers=bearer).status_code == 401
    assert api.post("/v1/operations/lease", json={}, headers=bearer).status_code == 401
    assert api.post("/v1/events", json={}, headers=bearer).status_code == 401
    forged = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    assert (
        api.get(f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {forged}"}).status_code
        == 401
    )
    assert (
        api.post(f"/v1/sessions/{sid}/client-token", headers={"X-API-Key": "other-key"}).status_code
        == 404
    )


def test_expired_or_unconfigured_client_tokens_are_refused(api, monkeypatch) -> None:
    sid = new_session(api)
    monkeypatch.setenv("CLIENT_TOKEN_TTL_S", "-1")
    get_settings.cache_clear()
    token = api.post(f"/v1/sessions/{sid}/client-token", headers=H).json()["token"]
    assert (
        api.get(f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )
    monkeypatch.setenv("CLIENT_TOKEN_SECRET", "")
    get_settings.cache_clear()
    assert api.post(f"/v1/sessions/{sid}/client-token", headers=H).status_code == 501
    assert (
        api.get(f"/v1/sessions/{sid}", headers={"Authorization": "Bearer v1.e30.AAAA"}).status_code
        == 401
    )


# ---- persistence -------------------------------------------------------------------
def test_pending_operations_survive_a_restart(tmp_path) -> None:
    from uuid import uuid4

    from app.schemas import Channel, Session, SessionStatus

    path = str(tmp_path / "ops.db")
    booking = start_collection("+2348030000000")
    p = DeferredProvider(booking, NOW, CFG)
    with pytest.raises(OperationPending) as err:
        p.resolve_address("12 King Street, Ikeja")
    op_id = err.value.operation_id
    session = Session(
        id=uuid4(),
        company_id="acme",
        status=SessionStatus.active,
        locale="en-US",
        channel=Channel.text,
        booking=booking,
        created_at=NOW,
        updated_at=NOW,
    )
    SqliteSessionStore(path).create(session)

    reopened = SqliteSessionStore(path)
    assert reopened.find_pending_operation_sessions("acme", NOW, 10) == [session.id]
    assert reopened.find_pending_operation_sessions("beta", NOW, 10) == []
    assert reopened.find_operation_session(op_id, "acme") == session.id
    assert reopened.find_operation_session(op_id, "beta") is None

    loaded = reopened.get(session.id, "acme")
    OperationBook(loaded.booking, NOW, CFG).lease(
        loaded.booking.ops[op_id], "acme", str(session.id)
    )
    reopened.save(loaded)
    assert reopened.find_pending_operation_sessions("acme", NOW, 10) == []  # leased
    assert reopened.find_pending_operation_sessions("acme", NOW + timedelta(seconds=61), 10) == [
        session.id
    ]


def test_recovery_restarts_an_exhausted_operation_after_the_cooldown(api, monkeypatch) -> None:
    from app.events.recovery import recover_pending
    from app.providers.selection import Providers, providers_for

    monkeypatch.setenv("OPERATION_MAX_ATTEMPTS", "1")
    get_settings.cache_clear()
    ex = Executor(api)
    ex.bookings.script = ["timeout_before"]
    sid = new_session(api)
    say(api, sid, blob())
    ex.run()
    say(api, sid, "yes")
    ex.run()
    assert booking_of(api, sid)["status"] == "booking_pending"
    assert ex.lease() == []  # exhausted: nothing for the executor

    def factory(session, now):
        active = providers_for(session.booking, now, Providers(None, None, None))
        return active.booking, active.offers

    soon = recover_pending(api.store, None, None, None, provider_factory=factory)
    assert [(r.before, r.after) for r in soon] == [("booking_pending", "booking_pending")]
    assert ex.lease() == []  # still cooling down: no blind retry

    later = datetime.now(UTC) + timedelta(seconds=301)
    recover_pending(api.store, None, None, None, now=later, provider_factory=factory)
    op = next(o for o in booking_of(api, sid)["ops"].values() if o["type"] == "create_booking")
    assert op["status"] == "pending" and op["attempts"] == 0 and op["reconcile"] is True
    assert op["idempotency_key"]  # re-queued under the same key
