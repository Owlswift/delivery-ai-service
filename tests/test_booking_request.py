from datetime import timedelta

from app.booking.models import BookingCreatedResult, BookingStatus
from app.booking.quoting import advance
from app.conversation.turn import handle_user_turn
from app.persistence.sqlite_store import SqliteSessionStore
from app.providers.fake_booking import FakeBookingProvider
from tests.test_quoting import EX, Convo, blob


class Flow(Convo):
    def __init__(self, script=None, store=None):
        super().__init__()
        self.bp = FakeBookingProvider(script)
        self.store = store

    def say(self, text: str) -> str:
        return handle_user_turn(
            self.session,
            text,
            EX,
            self.provider,
            self.clock,
            booking_provider=self.bp,
            checkpoint=(lambda: self.store.save(self.session)) if self.store else None,
        )


def confirmed(script=None, **kw) -> Flow:
    f = Flow(script, **kw)
    f.say(blob())
    return f


def test_nothing_created_before_confirmation() -> None:
    f = confirmed()
    assert f.b.status == BookingStatus.awaiting_confirmation
    assert f.bp.create_calls == []
    f.say("no")
    f.say("what about tracking?")
    assert f.bp.create_calls == [] and f.b.creation is None


def test_summary_has_all_details_then_yes_creates() -> None:
    f = Flow()
    summary = f.say(blob())
    for part in (
        "12 King Street",
        "5 Marina Road",
        "documents",
        "Ada Obi",
        "08031234567",
        "5000 NGN",
        "8000 NGN",
    ):
        assert part in summary
    reply = f.say("yes")
    assert f.b.status == BookingStatus.booking_created
    assert "BK-00001" in reply and "awaiting courier offers" in reply
    assert isinstance(f.b.result, BookingCreatedResult)
    assert f.b.result.payload.quote_id == f.b.quote.quote.quote_id
    assert len(f.bp.bookings) == 1


def test_duplicate_confirmation_returns_existing() -> None:
    f = confirmed()
    first = f.say("yes")
    second = f.say("yes")
    assert "BK-00001" in second and len(f.bp.create_calls) == 1
    assert first.split(" Status")[0] in second


def test_correction_before_confirm_refreshes_quote_and_creates_once() -> None:
    f = confirmed()
    f.provider.fare = "6000"
    f.say("Actually destination: 9 Allen Avenue, Ikeja")
    assert f.bp.create_calls == []
    f.say("yes")
    assert f.b.result.payload.base_fare == "6000"
    assert "9 Allen Avenue" in f.b.result.payload.destination.formatted_address
    assert len(f.bp.bookings) == 1


def test_rejection_claims_nothing_and_retry_uses_new_key() -> None:
    f = confirmed(["reject"])
    reply = f.say("yes")
    assert "No booking was created" in reply and "not serviceable" in reply
    assert f.b.status == BookingStatus.awaiting_confirmation
    assert f.b.quote.acceptance is None and f.bp.bookings == {}
    f.say("yes")
    assert f.b.status == BookingStatus.booking_created
    assert len(set(f.bp.create_calls)) == 2 and len(f.bp.bookings) == 1


def test_timeout_before_processing_retries_same_key_once_created() -> None:
    f = confirmed(["timeout_before"])
    reply = f.say("yes")
    assert f.b.status == BookingStatus.booking_pending
    assert "can't say it's booked" in reply and "BK-" not in reply
    f.say("any update?")
    assert f.b.status == BookingStatus.booking_created
    assert len(f.bp.bookings) == 1 and len(set(f.bp.create_calls)) == 1


def test_timeout_after_processing_reconciles_without_second_create() -> None:
    f = confirmed(["timeout_after"])
    reply = f.say("yes")
    assert f.b.status == BookingStatus.booking_created  # found by key right away
    assert "BK-00001" in reply
    assert len(f.bp.create_calls) == 1 and len(f.bp.bookings) == 1


def test_lookup_failure_stays_pending_and_does_not_resend() -> None:
    f = confirmed(["timeout_after"])
    f.bp.find_fails = True
    f.say("yes")
    assert f.b.status == BookingStatus.booking_pending
    f.say("status?")
    assert len(f.bp.create_calls) == 1
    f.bp.find_fails = False
    f.say("status?")
    assert f.b.status == BookingStatus.booking_created
    assert len(f.bp.create_calls) == 1 and len(f.bp.bookings) == 1


def test_restart_mid_call_recovers_from_sqlite(tmp_path) -> None:
    store = SqliteSessionStore(str(tmp_path / "s.db"))
    f = confirmed(["crash_after"], store=store)
    store.create(f.session)
    try:
        f.say("yes")
    except RuntimeError:
        pass
    reloaded = SqliteSessionStore(str(tmp_path / "s.db")).get(f.session.id, "acme")
    assert reloaded.booking.status == BookingStatus.booking_pending
    assert reloaded.booking.creation.request.payload.quote_id
    f.session = reloaded
    f.say("hello?")
    assert f.b.status == BookingStatus.booking_created
    assert len(f.bp.create_calls) == 1 and len(f.bp.bookings) == 1


def test_fare_change_before_submit_asks_again_and_books_nothing() -> None:
    f = confirmed()
    advance(
        f.session,
        "yes",
        details_changed=False,
        prev_status=BookingStatus.awaiting_confirmation,
        provider=f.provider,
        now=f.clock,
    )
    assert f.b.status == BookingStatus.ready_to_book
    f.clock += timedelta(minutes=30)
    f.provider.fare = "7000"
    from app.booking.creation import submit

    reply = submit(f.session, f.bp, f.provider, f.clock)
    assert "changed from 5000 NGN to 7000 NGN" in reply
    assert f.bp.create_calls == []
    assert f.b.status == BookingStatus.awaiting_confirmation
    f.say("yes")
    assert f.b.result.payload.base_fare == "7000"


def test_message_after_creation_reports_stored_status() -> None:
    f = confirmed()
    f.say("yes")
    f.bp.status = "something_else"
    reply = f.say("Actually pickup: 2 Allen Avenue, Ikeja")
    assert "BK-00001" in reply and "awaiting courier offers" in reply
    assert f.b.details.pickup_address.value == "12 King Street, Ikeja"
