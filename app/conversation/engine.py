from __future__ import annotations

from typing import TYPE_CHECKING

from app.booking.machine import init, transition
from app.booking.models import (
    USER_DETAIL_FIELDS,
    BookingState,
    BookingStatus,
    BudgetAmount,
    CollectedField,
    DeliveryDetails,
    DetailsCompleteResult,
    FieldStatus,
    NextAction,
    NextActionType,
)
from app.conversation.extract import Extractor
from app.conversation.prompts import COMPLETE, prompt_for_field
from app.conversation.validate import gap_fields, validate_details

if TYPE_CHECKING:
    from app.schemas import Session


def refresh(booking: BookingState) -> str:
    """Re-validate, recompute gaps/status/next action; return the assistant text."""
    details = booking.details
    validate_details(details)
    booking.missing_fields, booking.ambiguous_fields = gap_fields(details)

    for name in USER_DETAIL_FIELDS:
        field: CollectedField = getattr(details, name)
        if field.status != FieldStatus.valid:
            prompt = prompt_for_field(name, field.status)
            transition(booking, BookingStatus.collecting)
            booking.result = None
            booking.next_actions = [
                NextAction(type=NextActionType.ask_user, field=name, prompt=prompt)
            ]
            return prompt

    if booking.status == BookingStatus.collecting:
        transition(booking, BookingStatus.details_complete)  # later stages own their statuses
    booking.next_actions = [NextAction(type=NextActionType.none)]
    booking.result = DetailsCompleteResult(
        pickup_address=details.pickup_address.value or "",
        destination_address=details.destination_address.value or "",
        package_description=details.package_description.value or "",
        recipient_name=details.recipient_name.value or "",
        recipient_phone=details.recipient_phone.value or "",
        sender_max_budget=BudgetAmount(
            amount=details.budget_amount or "", currency=details.budget_currency
        ),
        sender_phone=details.sender_phone.value or "",
    )
    return COMPLETE


def start_collection(sender_phone: str) -> BookingState:
    details = DeliveryDetails()
    details.sender_phone.value = sender_phone
    booking = BookingState(details=details)
    init(booking)
    refresh(booking)
    return booking


def apply_user_turn(session: Session, text: str, extractor: Extractor) -> str:
    """Extract, merge (new values overwrite), validate; mutate the session booking."""
    booking = session.booking
    extraction = extractor.extract(text, session)
    for name in USER_DETAIL_FIELDS:
        value = getattr(extraction, name)
        if value:
            getattr(booking.details, name).value = value
    return refresh(booking)
