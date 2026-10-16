from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid5

from app.booking import creation_prompts as prompts
from app.booking.machine import transition
from app.booking.models import (
    BookingCreatedResult,
    BookingRequest,
    BookingState,
    BookingStatus,
    CreationState,
    NextAction,
    NextActionType,
    PresentedFare,
    RequestStatus,
)
from app.booking.quoting import ensure_bookable
from app.errors import AppError
from app.providers.models import BookingPayload, Confirmation, MaxBudget, ProviderBooking
from app.providers.protocols import (
    BookingProvider,
    BookingProviderError,
    BookingRejected,
    LogisticsProvider,
)

if TYPE_CHECKING:
    from app.schemas import Session


def _idempotency_key(session: Session, generation: int) -> str:
    return str(uuid5(session.id, f"booking:{generation}"))


def _payload(booking: BookingState, quote_id: str) -> BookingPayload:
    details, state = booking.details, booking.quote
    assert state and state.pickup and state.destination and state.quote and state.acceptance
    assert state.pickup.resolved and state.destination.resolved
    assert state.pickup.resolved.has_coordinates and state.destination.resolved.has_coordinates
    return BookingPayload(
        pickup=state.pickup.resolved,
        destination=state.destination.resolved,
        package_description=details.package_description.value or "",
        recipient_name=details.recipient_name.value or "",
        recipient_phone=details.recipient_phone.value or "",
        sender_phone=details.sender_phone.value or "",
        quote_id=quote_id,
        base_fare=state.quote.base_fare,
        currency=state.quote.currency,
        max_budget=MaxBudget(amount=details.budget_amount or "", currency=details.budget_currency),
        confirmation=Confirmation(
            confirmed_at=state.acceptance.accepted_at,
            quote_id=quote_id,
            fingerprint=state.acceptance.fingerprint,
        ),
    )


def _set_status(booking: BookingState, status: BookingStatus, action: NextAction) -> None:
    transition(booking, status)
    booking.next_actions = [action]


def _mark_created(booking: BookingState, request: BookingRequest, found: ProviderBooking) -> str:
    request.status = RequestStatus.created
    request.reference = found.reference
    request.provider_status = found.status
    booking.result = BookingCreatedResult(
        reference=found.reference, provider_status=found.status, payload=request.payload
    )
    _set_status(booking, BookingStatus.booking_created, NextAction(type=NextActionType.none))
    return prompts.created(request)


def _pending(booking: BookingState) -> str:
    _set_status(booking, BookingStatus.booking_pending, NextAction(type=NextActionType.none))
    return prompts.UNCONFIRMED


def _lookup(
    booking: BookingState, request: BookingRequest, provider: BookingProvider
) -> tuple[bool, str | None]:
    """(reachable, reply): reply is set when the provider already holds the booking."""
    try:
        found = provider.find_booking(request.idempotency_key)
    except BookingProviderError:
        return False, None
    return True, _mark_created(booking, request, found) if found else None


def _send(session: Session, request: BookingRequest, provider: BookingProvider) -> str:
    booking = session.booking
    creation = booking.creation
    assert creation is not None
    request.attempts += 1
    try:
        found = provider.create_booking(request.payload, request.idempotency_key)
    except BookingRejected as exc:
        reason = str(exc) or "request refused"
        creation.last_error = reason
        creation.request = None
        creation.generation += 1
        if booking.quote:
            booking.quote.acceptance = None
        _set_status(
            booking,
            BookingStatus.awaiting_confirmation,
            NextAction(type=NextActionType.confirm, field="proceed"),
        )
        return prompts.rejected(reason)
    except BookingProviderError:
        _, reply = _lookup(booking, request, provider)
        return reply or _pending(booking)
    return _mark_created(booking, request, found)


def submit(
    session: Session,
    provider: BookingProvider,
    logistics: LogisticsProvider,
    now: datetime,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    """Create the booking. Call only when quoting reports `ready_to_book`."""
    booking = session.booking
    try:
        quote = ensure_bookable(session, logistics, now)
    except AppError as exc:
        if exc.code != "quote_changed":
            raise
        state = booking.quote
        assert state and state.quote
        state.presented_fare = PresentedFare(
            base_fare=state.quote.base_fare, currency=state.quote.currency
        )
        details = exc.details or {}
        return prompts.fare_changed(details["previous"], details["current"])

    if booking.creation is None:
        booking.creation = CreationState()
    creation = booking.creation
    request = BookingRequest(
        idempotency_key=_idempotency_key(session, creation.generation),
        payload=_payload(booking, quote.quote_id),
    )
    creation.request = request
    _set_status(booking, BookingStatus.booking_pending, NextAction(type=NextActionType.none))
    if checkpoint:
        checkpoint()
    return _send(session, request, provider)


def on_message(
    session: Session,
    provider: BookingProvider,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    """Handle a turn while a request is pending or already created."""
    booking = session.booking
    creation = booking.creation
    assert creation is not None and creation.request is not None
    request = creation.request
    if request.status == RequestStatus.created:
        return prompts.already_created(request)
    reachable, reply = _lookup(booking, request, provider)
    if reply is not None:
        return reply
    if not reachable:
        return _pending(booking)  # cannot tell whether it exists; do not resend blindly
    return _send(session, request, provider)
