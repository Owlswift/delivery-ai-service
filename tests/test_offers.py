from datetime import timedelta

import pytest

from app.booking.models import BookingStatus
from app.conversation.turn import handle_user_turn
from app.events.handler import handle_event
from app.events.models import EventError, ProviderEvent
from app.offers.models import Courier, Offer
from app.offers.ranking import NearestPickupPolicy
from app.providers.fake_offers import FakeOfferProvider
from tests.test_booking_request import Flow
from tests.test_quoting import EX, T0, blob

POLICY = NearestPickupPolicy()


class Desk(Flow):
    """A booked delivery (budget 8000 NGN) waiting for offers."""

    def __init__(self, script=None, store=None):
        super().__init__(store=store)
        self.op = FakeOfferProvider(script)
        self.say(blob())
        self.say("yes")
        assert self.b.status == BookingStatus.booking_created
        self.ref = self.b.creation.request.reference
        self.n = 0

    def offer(self, offer_id, price="6000", eta=None, dist=None, status="open", **kw):
        self.n += 1
        event_id = kw.pop("event_id", f"e{self.n}")
        reference = kw.pop("reference", self.ref)
        occurred_at = kw.pop("updated_at", T0 + timedelta(seconds=self.n))
        payload = dict(
            offer_id=offer_id,
            courier=dict(
                courier_id=f"c-{offer_id}", name=f"Courier {offer_id}", details={"likes": 42}
            ),
            price=price,
            currency="NGN",
            pickup_eta_min=eta,
            pickup_distance_km=dist,
            **kw,
        )
        kind = {
            "open": "offer.created",
            "withdrawn": "offer.withdrawn",
            "expired": "offer.expired",
        }.get(status, status)
        event = ProviderEvent(
            event_id=event_id,
            booking_reference=reference,
            type=kind,
            occurred_at=occurred_at,
            payload=payload,
        )
        return handle_event(self.session, event, self.op, POLICY, self.clock)

    def msg(self, text):
        return handle_user_turn(
            self.session,
            text,
            EX,
            self.provider,
            self.clock,
            booking_provider=self.bp,
            offer_provider=self.op,
        )


def test_policy_is_deterministic_and_orders_by_eta_then_price_then_time_then_id() -> None:
    def o(i, price="100", eta=None, dist=None, at=0):
        return Offer(
            offer_id=i,
            courier=Courier(courier_id=i),
            price=price,
            currency="NGN",
            status="open",
            pickup_eta_min=eta,
            pickup_distance_km=dist,
            received_at=T0 + timedelta(seconds=at),
        )

    def ranked(offers):
        return [x.offer_id for x in POLICY.rank(offers).ordered]

    assert ranked([o("a", eta=20), o("b", eta=5), o("c", eta=10)]) == ["b", "c", "a"]
    # ETA missing on one -> distance is the single metric, missing distance last
    assert ranked([o("a", eta=5, dist=9), o("b", dist=2), o("c", eta=1)]) == ["b", "a", "c"]
    # exact ties: price, then received_at, then id
    assert ranked(
        [
            o("b", eta=5, price="200"),
            o("a", eta=5, price="100", at=9),
            o("c", eta=5, price="100", at=1),
        ]
    ) == ["c", "a", "b"]
    assert ranked([o("z", eta=5), o("y", eta=5)]) == ["y", "z"]
    # no tolerance: 5 vs 6 minutes decides even though the 6-minute offer is cheaper
    assert ranked([o("a", eta=6, price="1"), o("b", eta=5, price="999")]) == ["b", "a"]
    items = [o("a", eta=3), o("b", eta=3, price="50")]
    assert ranked(items) == ranked(list(reversed(items)))


def test_best_within_budget_is_assigned_and_provider_confirms_first() -> None:
    d = Desk()
    d.op.script = ["timeout_before"]
    r = d.offer("o1", eta=10)
    assert r.message is None and d.b.status == BookingStatus.assigning_courier  # unconfirmed
    assert d.b.offers.assignment.status == "requested"
    d.op.find_fails = False
    reply = d.msg("any news?")
    assert d.b.status == BookingStatus.courier_assigned
    assert "assigned" in reply and len(d.op.assignments) == 1


def test_assignment_message_only_after_confirmation_and_records_decision() -> None:
    d = Desk()
    r = d.offer("o1", price="7000", eta=12)
    assert d.b.status == BookingStatus.courier_assigned
    assert "Courier o1" in r.message and "12 min" in r.message
    dec = d.b.offers.decision
    assert dec.policy_version == "nearest-pickup-v1" and dec.chosen_offer_id == "o1"
    assert d.op.assign_calls[0][0] == "o1"


def test_duplicate_events_never_assign_twice() -> None:
    d = Desk()
    d.offer("o1", eta=10, event_id="same")
    again = d.offer("o1", eta=10, event_id="same")
    assert again.outcome == "duplicate"
    d.offer("o2", eta=1)  # arrives after assignment
    assert len(d.op.assign_calls) == 1 and d.b.offers.assignment.offer_id == "o1"


def test_identical_redelivery_without_event_id_is_noop() -> None:
    d = Desk(script=["timeout_before"])
    d.offer("o1", price="9000", eta=3, updated_at=T0)
    # retransmission under a new event id: same offer version (same occurred_at), same content
    assert d.offer("o1", price="9000", eta=3, updated_at=T0).outcome == "duplicate"


def test_invalid_events_are_rejected_and_not_stored() -> None:
    d = Desk()
    for kwargs in (
        dict(reference="OTHER"),
        dict(price="0"),
        dict(price="abc"),
        dict(price="-5"),
    ):
        with pytest.raises(EventError):
            d.offer("bad", **kwargs)
    with pytest.raises(EventError):
        d.offer("")
    assert d.b.offers is None or d.b.offers.offers == {}


def test_expired_and_withdrawn_offers_cannot_be_selected() -> None:
    d = Desk()
    d.offer("gone", price="9000", status="withdrawn")  # above budget anyway
    d.offer("old", price="5000", eta=1, expires_at=T0 - timedelta(minutes=1))
    d.offer("exp", price="5000", eta=1, status="expired")
    assert d.b.status == BookingStatus.booking_created and d.op.assign_calls == []


def test_offer_expiring_before_evaluation_is_skipped() -> None:
    d = Desk()
    d.offer("short", price="9000", eta=1, expires_at=T0 + timedelta(minutes=1))
    assert d.b.status == BookingStatus.offers_awaiting_approval
    d.clock += timedelta(minutes=5)
    d.offer("noise", price="9500", status="withdrawn")
    d.msg("yes")  # approval for an offer that expired meanwhile
    assert d.op.assign_calls == []
    assert d.b.status == BookingStatus.booking_created


def test_above_budget_is_never_assigned_without_approval() -> None:
    d = Desk()
    r = d.offer("o1", price="9000", eta=2)
    assert d.b.status == BookingStatus.offers_awaiting_approval
    assert d.op.assign_calls == []
    assert "9000 NGN" in r.message and "8000 NGN" in r.message
    d.offer("o2", price="8500", eta=1)  # new, lower, still above: no repeat ask
    assert d.op.assign_calls == []


def test_asks_about_lowest_above_budget_offer_only_once() -> None:
    d = Desk()
    first = d.offer("o1", price="9500", eta=2)
    second = d.offer("o2", price="9100", eta=9)
    assert first.message and second.message is None
    assert d.b.offers.pending_approval.offer_id == "o1"


def test_user_approval_assigns_that_offer_and_keeps_budget() -> None:
    d = Desk()
    d.offer("o1", price="9000", eta=2)
    reply = d.msg("yes")
    assert d.b.status == BookingStatus.courier_assigned and "assigned" in reply
    assert d.b.offers.assignment.approved_above_budget
    assert d.b.offers.budget.amount == "8000"


def test_decline_keeps_request_active_and_does_not_reask_same_offer() -> None:
    d = Desk()
    d.offer("o1", price="9000", eta=2)
    d.msg("no")
    assert d.b.status == BookingStatus.booking_created
    assert d.offer("o1", price="9000", eta=2, event_id="x").message is None
    assert d.op.assign_calls == []
    # a changed price is a different offer to the user and is asked about again
    assert d.offer("o1", price="8800", eta=2).message is not None


def test_raising_budget_reevaluates_all_valid_offers_and_picks_best() -> None:
    d = Desk()
    d.offer("far", price="9000", eta=30)
    d.offer("near", price="9500", eta=4)
    d.offer("dead", price="8500", eta=1, status="withdrawn")
    reply = d.msg("raise my budget to 9,600 NGN")
    assert d.b.status == BookingStatus.courier_assigned
    assert d.b.offers.assignment.offer_id == "near"
    assert d.b.offers.budget.amount == "9600" and len(d.b.offers.budget.history) == 1
    assert d.b.result.payload.max_budget.amount == "8000"  # booking payload untouched
    assert "assigned" in reply


def test_budget_raise_not_enough_waits_with_confirmation() -> None:
    d = Desk()
    d.offer("o1", price="9500", eta=2)
    d.msg("9000")
    assert d.b.offers.budget.amount == "9000"
    assert d.b.status == BookingStatus.offers_awaiting_approval  # still asks about o1


def test_budget_currency_mismatch_is_refused() -> None:
    d = Desk()
    d.offer("o1", price="9500", eta=2)
    reply = d.msg("9500 USD")
    assert "NGN" in reply and d.b.offers.budget.amount == "8000"


def test_provider_rejection_falls_through_to_next_best() -> None:
    d = Desk(script=["reject"])
    d.offer("far", eta=30)  # assigned attempt rejected
    assert d.b.status == BookingStatus.booking_created
    r = d.offer("mid", eta=15)
    assert d.b.offers.assignment.offer_id == "mid" and "Courier mid" in r.message
    assert d.b.offers.offers["far"].status == "unavailable"


def test_rejection_with_no_alternative_tells_user() -> None:
    d = Desk(script=["reject"])
    r = d.offer("only", eta=5)
    assert "couldn't assign" in r.message
    assert d.b.status == BookingStatus.booking_created


def test_unavailable_offer_is_not_revived_by_redelivery() -> None:
    d = Desk(script=["reject"])
    d.offer("x", eta=5)
    d.offer("x", eta=5, event_id="again", updated_at=T0 + timedelta(seconds=5))
    assert len(d.op.assign_calls) == 1


def test_stale_event_is_ignored() -> None:
    d = Desk()
    d.offer("o1", price="9000", eta=9, updated_at=T0 + timedelta(minutes=2))
    r = d.offer("o1", price="5000", eta=1, updated_at=T0 + timedelta(minutes=1))
    assert r.outcome == "stale" and d.op.assign_calls == []


def test_withdrawal_of_asked_offer_informs_user_once() -> None:
    d = Desk()
    d.offer("o1", price="9000", eta=2)
    r = d.offer("o1", price="9000", eta=2, status="withdrawn")
    assert "no longer available" in r.message
    assert d.b.status == BookingStatus.booking_created
    assert d.offer("o9", price="9900", status="withdrawn").message is None


def test_assignment_timeout_after_processing_reconciles_without_second_assign() -> None:
    d = Desk(script=["timeout_after"])
    r = d.offer("o1", eta=3)
    assert d.b.status == BookingStatus.courier_assigned and "assigned" in r.message
    assert len(d.op.assign_calls) == 1


def test_lookup_failure_never_claims_assignment_and_does_not_resend() -> None:
    d = Desk(script=["timeout_after"])
    d.op.find_fails = True
    r = d.offer("o1", eta=3)
    assert r.message is None and d.b.status == BookingStatus.assigning_courier
    reply = d.msg("hello?")
    assert "can't say a courier is assigned" in reply and len(d.op.assign_calls) == 1
    d.op.find_fails = False
    d.msg("hello?")
    assert d.b.status == BookingStatus.courier_assigned and len(d.op.assign_calls) == 1


def test_messages_after_assignment_report_it_and_never_reassign() -> None:
    d = Desk()
    d.offer("o1", eta=3)
    reply = d.msg("yes")
    assert "assigned" in reply and len(d.op.assign_calls) == 1


def test_waiting_message_before_any_offer() -> None:
    d = Desk()
    assert "awaiting courier offers" in d.msg("any update?")
    assert d.b.status == BookingStatus.booking_created


def test_restart_recovers_pending_assignment_from_sqlite(tmp_path) -> None:
    from app.persistence.sqlite_store import SqliteSessionStore

    path = str(tmp_path / "s.db")
    store = SqliteSessionStore(path)
    d = Desk()
    d.op.script = ["timeout_after"]
    d.op.find_fails = True
    store.create(d.session)
    ev = ProviderEvent(
        event_id="e",
        booking_reference=d.ref,
        type="offer.created",
        occurred_at=T0,
        payload=dict(
            offer_id="o1",
            courier=dict(courier_id="c1"),
            price="6000",
            currency="NGN",
            pickup_eta_min=3,
        ),
    )
    handle_event(d.session, ev, d.op, POLICY, d.clock, checkpoint=lambda: store.save(d.session))
    reloaded = SqliteSessionStore(path).get(d.session.id, "acme")
    assert reloaded.booking.offers.assignment.status == "requested"
    d.session = reloaded
    d.op.find_fails = False
    d.msg("status?")
    assert d.b.status == BookingStatus.courier_assigned and len(d.op.assign_calls) == 1
    assert SqliteSessionStore(path).get_by_booking_reference(d.ref, "acme") is not None
    assert SqliteSessionStore(path).get_by_booking_reference(d.ref, "other") is None


def test_api_event_flow_notifies_once(client) -> None:
    h = {"X-API-Key": "dev-key"}
    sid = client.post("/v1/sessions", json={"sender_phone": "+2348030000000"}, headers=h).json()[
        "id"
    ]
    url = f"/v1/sessions/{sid}/messages"
    client.post(url, json={"content": blob()}, headers=h)
    ref = client.post(url, json={"content": "yes"}, headers=h).json()["session"]["booking"][
        "result"
    ]["reference"]

    from tests.signing import signed

    def event(**kw):
        payload = {
            "offer_id": "o1",
            "courier": {"courier_id": "c1", "name": "Ife"},
            "price": "9000",
            "currency": "NGN",
            "pickup_eta_min": 5,
        }
        payload.update(kw.pop("payload", {}))
        base = {
            "event_id": "e1",
            "booking_reference": ref,
            "type": "offer.created",
            "occurred_at": "2026-10-16T12:00:00Z",
            "payload": payload,
        }
        return {**base, **kw}

    def post(body, **kw):
        return signed(client, "POST", "/v1/events", body, **kw)

    r = post(event())
    assert r.status_code == 200 and r.json()["result"] == "stored" and r.json()["notified"]
    assert post(event()).json() == {
        "result": "duplicate",
        "notified": False,
        "original_result": "stored",
    }
    assert post(event(booking_reference="NOPE")).status_code == 404
    assert post(event(event_id="e2", payload={"price": "0"})).status_code == 422
    assert post({**event(), "event_id": ""}).status_code == 422
    missing = event()
    del missing["occurred_at"]
    assert post(missing).status_code == 422
    assert post(event(), provider="beta").status_code == 404  # another provider's tenant
    reply = client.post(url, json={"content": "yes"}, headers=h).json()
    assert reply["session"]["booking"]["status"] == "courier_assigned"
    msgs = client.get(f"/v1/sessions/{sid}", headers=h).json()["messages"]
    assert sum("lowest offer" in m["content"] for m in msgs) == 1
