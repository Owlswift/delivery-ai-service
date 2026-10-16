from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.booking.models import BookingStatus, ReadyToBookResult
from app.booking.quoting import ensure_bookable
from app.conversation.engine import start_collection
from app.conversation.extract import RuleBasedExtractor
from app.conversation.turn import handle_user_turn
from app.errors import AppError
from app.providers.fake_logistics import FakeLogisticsProvider
from app.providers.protocols import ProviderTimeout
from app.schemas import Channel, Session, SessionStatus

EX = RuleBasedExtractor()
T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def blob(pickup="12 King Street, Ikeja", dest="5 Marina Road, Lagos Island", budget="8000 NGN"):
    return (
        f"Pickup: {pickup}\nDestination: {dest}\nPackage: documents\n"
        f"Recipient: Ada Obi\nRecipient phone: 08031234567\nBudget: {budget}"
    )


class Convo:
    def __init__(self, provider=None):
        self.clock = T0
        self.provider = provider or FakeLogisticsProvider(now=lambda: self.clock)
        self.session = Session(
            id=uuid4(),
            company_id="acme",
            status=SessionStatus.active,
            locale="en-US",
            channel=Channel.text,
            booking=start_collection("+2348030000000"),
            created_at=T0,
            updated_at=T0,
        )

    def say(self, text: str) -> str:
        return handle_user_turn(self.session, text, EX, self.provider, self.clock)

    @property
    def b(self):
        return self.session.booking


def at_confirmation(**kw) -> Convo:
    c = Convo()
    c.say(blob(**kw))
    assert c.b.status == BookingStatus.awaiting_confirmation
    return c


def test_resolved_and_quoted_from_provider_only() -> None:
    c = Convo()
    reply = c.say(blob())
    assert c.b.status == BookingStatus.awaiting_confirmation
    assert c.b.quote.quote.base_fare == "5000"
    assert "Base fare: 5000 NGN" in reply and "35 min" in reply
    assert "confirm" in reply and "Package: documents" in reply and "Ada Obi" in reply
    assert c.b.next_actions[0].type == "confirm"


def test_no_quote_before_both_resolved() -> None:
    c = Convo()
    reply = c.say(blob(dest="9 Unresolvable Road, Lagos"))
    assert c.b.status == BookingStatus.address_clarification
    assert c.b.quote.quote is None
    assert c.provider.estimate_calls == 0
    assert "destination" in reply


def test_not_found_then_corrected() -> None:
    c = Convo()
    c.say(blob(pickup="1 Unresolvable Lane, Ikeja"))
    assert c.b.next_actions[0].field == "pickup_address"
    c.say("3 Allen Avenue, Ikeja")
    assert c.b.status == BookingStatus.awaiting_confirmation
    assert c.b.quote.pickup.resolved.formatted_address == "3 Allen Avenue, Ikeja"


def test_ambiguous_pick_by_number() -> None:
    c = Convo()
    reply = c.say(blob(pickup="7 Ambiguous Close, Ikeja"))
    assert c.b.status == BookingStatus.address_clarification
    assert "1." in reply and "2." in reply
    c.say("2")
    assert c.b.status == BookingStatus.awaiting_confirmation
    assert "(South)" in c.b.quote.pickup.resolved.formatted_address


def test_timeout_changes_nothing_and_retries() -> None:
    c = Convo()
    real = c.provider.resolve_address
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) == 1:
            raise ProviderTimeout(text)
        return real(text)

    c.provider.resolve_address = flaky
    reply = c.say(blob())
    assert "try again" in reply
    assert c.b.quote.pickup is None and c.b.quote.quote is None
    c.say("ok")
    assert c.b.status == BookingStatus.awaiting_confirmation


def test_estimate_failure_keeps_addresses_and_retries() -> None:
    c = Convo()
    c.provider.fail_estimate = True
    reply = c.say(blob())
    assert "try again" in reply and c.b.quote.quote is None
    assert c.b.quote.pickup.resolved is not None
    c.provider.fail_estimate = False
    c.say("hello")
    assert c.b.status == BookingStatus.awaiting_confirmation


def test_budget_below_fare_asks_revision_then_proceeds() -> None:
    c = Convo()
    reply = c.say(blob(budget="3000 NGN"))
    assert c.b.status == BookingStatus.budget_revision
    assert "5000 NGN" in reply and c.b.next_actions[0].field == "sender_max_budget"
    c.say("6000")
    assert c.b.status == BookingStatus.awaiting_confirmation


def test_budget_equal_to_fare_is_ok_and_wrong_currency_is_not() -> None:
    assert at_confirmation(budget="5000 NGN").b.status == BookingStatus.awaiting_confirmation
    c = Convo()
    c.say(blob(budget="9000 USD"))
    assert c.b.status == BookingStatus.budget_revision


def test_explicit_yes_required() -> None:
    c = at_confirmation()
    c.say("what vehicle is it?")
    assert c.b.status == BookingStatus.awaiting_confirmation
    c.say("no")
    assert c.b.status == BookingStatus.awaiting_confirmation
    assert c.b.quote.acceptance is None
    c.say("yes")
    assert c.b.status == BookingStatus.ready_to_book
    assert isinstance(c.b.result, ReadyToBookResult)
    assert c.b.result.quote.base_fare == "5000"


def test_changing_address_discards_quote_and_consent() -> None:
    c = at_confirmation()
    c.say("yes")
    first_quote = c.b.quote.quote.quote_id
    c.say("Actually destination: 9 Allen Avenue, Ikeja")
    assert c.b.status == BookingStatus.awaiting_confirmation
    assert c.b.quote.quote.quote_id != first_quote
    assert c.b.quote.quote.destination_place_id.endswith("9allenavenueikeja")
    assert c.b.quote.acceptance is None
    c.say("yes")
    assert c.b.status == BookingStatus.ready_to_book


def test_changing_address_to_unresolvable_clears_usable_quote() -> None:
    c = at_confirmation()
    c.say("Actually pickup: 2 Unresolvable Way, Ikeja")
    assert c.b.status == BookingStatus.address_clarification
    c.say("yes")  # a yes must not book anything
    assert c.b.status != BookingStatus.ready_to_book


def test_quote_expiry_same_fare_still_accepts() -> None:
    c = at_confirmation()
    c.clock += timedelta(minutes=30)
    c.say("yes")
    assert c.b.status == BookingStatus.ready_to_book
    assert c.provider.estimate_calls == 2


def test_quote_expiry_changed_fare_represents() -> None:
    c = at_confirmation()
    c.clock += timedelta(minutes=30)
    c.provider.fare = "6500"
    reply = c.say("yes")
    assert c.b.status == BookingStatus.awaiting_confirmation
    assert "changed from 5000 NGN to 6500 NGN" in reply
    c.say("yes")
    assert c.b.status == BookingStatus.ready_to_book
    assert c.b.result.quote.base_fare == "6500"


def test_fare_increase_above_budget_asks_revision() -> None:
    c = at_confirmation(budget="5500 NGN")
    c.clock += timedelta(minutes=30)
    c.provider.fare = "6500"
    c.say("yes")
    assert c.b.status == BookingStatus.budget_revision


def test_ensure_bookable_requires_confirmation() -> None:
    c = at_confirmation()
    with pytest.raises(AppError) as err:
        ensure_bookable(c.session, c.provider, c.clock)
    assert err.value.code == "not_confirmed"


def test_ensure_bookable_ok_then_expired_same_fare_then_changed_fare() -> None:
    c = at_confirmation()
    c.say("yes")
    assert ensure_bookable(c.session, c.provider, c.clock).base_fare == "5000"
    c.clock += timedelta(minutes=30)
    assert ensure_bookable(c.session, c.provider, c.clock).base_fare == "5000"
    c.clock += timedelta(minutes=30)
    c.provider.fare = "7000"
    with pytest.raises(AppError) as err:
        ensure_bookable(c.session, c.provider, c.clock)
    assert err.value.code == "quote_changed"
    assert c.b.quote.acceptance is None
    assert c.b.status == BookingStatus.awaiting_confirmation
    with pytest.raises(AppError) as err:
        ensure_bookable(c.session, c.provider, c.clock)
    assert err.value.code == "not_confirmed"


def test_ensure_bookable_rejects_detail_change_after_consent() -> None:
    c = at_confirmation()
    c.say("yes")
    c.b.details.package_description.value = "something else"
    with pytest.raises(AppError) as err:
        ensure_bookable(c.session, c.provider, c.clock)
    assert err.value.code == "not_confirmed"


def test_api_flow_to_ready(client) -> None:
    h = {"X-API-Key": "dev-key"}
    sid = client.post("/v1/sessions", json={"sender_phone": "+2348030000000"}, headers=h).json()[
        "id"
    ]
    url = f"/v1/sessions/{sid}/messages"
    first = client.post(url, json={"content": blob()}, headers=h).json()
    assert first["session"]["booking"]["status"] == "awaiting_confirmation"
    done = client.post(url, json={"content": "yes"}, headers=h).json()
    booking = done["session"]["booking"]
    assert booking["status"] == "booking_created"
    assert booking["result"]["type"] == "booking_created"
    assert booking["result"]["reference"] == "BK-00001"
    assert (
        client.get(f"/v1/sessions/{sid}", headers=h).json()["booking"]["status"]
        == "booking_created"
    )
