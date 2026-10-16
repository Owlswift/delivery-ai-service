from datetime import UTC, datetime
from uuid import uuid4

from app.booking.models import BookingStatus, CollectedField, DeliveryDetails, FieldStatus
from app.conversation.engine import apply_user_turn, start_collection
from app.conversation.extract import RuleBasedExtractor
from app.conversation.validate import validate_details
from app.persistence.sqlite_store import SqliteSessionStore
from app.schemas import Channel, Session, SessionStatus

EX = RuleBasedExtractor()
FULL = (
    "Pickup: 12 King Street, Ikeja\nDestination: 5 Marina Road, Lagos Island\n"
    "Package: documents\nRecipient: Ada Obi\nRecipient phone: 08031234567\n"
    "Budget: 8000 NGN"
)


def make_session() -> Session:
    now = datetime.now(UTC)
    return Session(
        id=uuid4(),
        company_id="acme",
        status=SessionStatus.active,
        locale="en-US",
        channel=Channel.text,
        booking=start_collection("+2348030000000"),
        created_at=now,
        updated_at=now,
    )


def turn(session: Session, text: str) -> str:
    return apply_user_turn(session, text, EX)


# extractor
def test_labeled_blob() -> None:
    e = EX.extract(FULL, make_session())
    assert e.pickup_address == "12 King Street, Ikeja"
    assert e.recipient_name == "Ada Obi"
    assert e.recipient_phone == "08031234567"


def test_from_to_sentence() -> None:
    e = EX.extract(
        "Pick up from 12 King Street, Ikeja and deliver to 5 Marina Road, Lagos Island",
        make_session(),
    )
    assert e.pickup_address == "12 King Street, Ikeja"
    assert e.destination_address == "5 Marina Road, Lagos Island"


def test_last_question_fallback() -> None:
    s = make_session()
    assert s.booking.next_actions[0].field == "pickup_address"
    turn(s, "12 King Street, Ikeja")
    assert s.booking.details.pickup_address.status == FieldStatus.valid


def test_sender_phone_never_taken_from_chat() -> None:
    s = make_session()
    turn(s, "my number is 08099999999")
    assert s.booking.details.sender_phone.value == "+2348030000000"
    assert s.booking.details.recipient_phone.status == FieldStatus.missing


# validator
def test_address_validation() -> None:
    def status(v: str) -> FieldStatus:
        d = DeliveryDetails(pickup_address=CollectedField(value=v))
        validate_details(d)
        return d.pickup_address.status

    assert status("12 King Street, Ikeja") == FieldStatus.valid
    for vague in ("the mall", "airport", "my house", "there"):
        assert status(vague) == FieldStatus.ambiguous


def test_phone_and_budget_validation() -> None:
    d = DeliveryDetails(
        recipient_phone=CollectedField(value="abc12345"),
        sender_max_budget=CollectedField(value="0"),
    )
    validate_details(d)
    assert d.recipient_phone.status == FieldStatus.invalid
    assert d.sender_max_budget.status == FieldStatus.invalid
    d.recipient_phone.value = "+234 803 123 4567"
    d.sender_max_budget.value = "₦8,000"
    validate_details(d)
    assert d.recipient_phone.status == FieldStatus.valid


# engine
def test_complete_in_one_turn() -> None:
    s = make_session()
    turn(s, FULL)
    assert s.booking.status == BookingStatus.details_complete
    assert s.booking.result is not None


def test_progressive_fill_asks_one_question_in_order() -> None:
    s = make_session()
    turn(s, "12 King Street, Ikeja")
    assert s.booking.next_actions[0].field == "destination_address"
    turn(s, "5 Marina Road, Lagos Island")
    assert s.booking.next_actions[0].field == "package_description"
    assert s.booking.status == BookingStatus.collecting


def test_correction_overwrites() -> None:
    s = make_session()
    turn(s, FULL)
    turn(s, "Actually pickup: 9 Allen Avenue, Ikeja")
    assert s.booking.details.pickup_address.value == "9 Allen Avenue, Ikeja"
    assert s.booking.status == BookingStatus.details_complete


def test_no_invention() -> None:
    s = make_session()
    turn(s, "hello there")
    d = s.booking.details
    assert d.destination_address.value is None
    assert d.package_description.value is None
    assert d.sender_max_budget.value is None


# store
def test_sqlite_survives_restart(tmp_path) -> None:
    path = str(tmp_path / "s.db")
    s = make_session()
    turn(s, FULL)
    SqliteSessionStore(path).create(s)
    loaded = SqliteSessionStore(path).get(s.id, "acme")
    assert loaded is not None
    assert loaded.booking.status == BookingStatus.details_complete
    assert SqliteSessionStore(path).get(s.id, "other") is None
