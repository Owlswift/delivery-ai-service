from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.booking import prompts
from app.booking.machine import is_terminal, transition, trigger
from app.booking.models import BookingStatus, NextAction, NextActionType
from app.events.models import EventAck, EventError, ProviderEvent
from app.observability.audit import audit
from app.observability.metrics import EVENTS
from app.offers.engine import OfferEventError, ingest
from app.offers.models import OfferEvent, OfferEventPayload
from app.offers.ranking import RankingPolicy
from app.providers.protocols import OfferProvider

if TYPE_CHECKING:
    from app.schemas import Session

MAX_SEEN_EVENTS = 500
OFFER_EVENT_STATUS = {
    "offer.created": "open",
    "offer.updated": "open",
    "offer.withdrawn": "withdrawn",
    "offer.expired": "expired",
}
OFFER_STATUSES = frozenset(
    {
        BookingStatus.booking_created,
        BookingStatus.offers_awaiting_approval,
        BookingStatus.assigning_courier,
    }
)


def _remember(session: Session, event_id: str, outcome: str) -> None:
    seen = session.booking.workflow.seen_event_ids
    seen[event_id] = outcome
    while len(seen) > MAX_SEEN_EVENTS:
        seen.pop(next(iter(seen)))


def handle_event(
    session: Session,
    event: ProviderEvent,
    offer_provider: OfferProvider,
    ranking: RankingPolicy,
    now: datetime,
    checkpoint: Callable[[], None] | None = None,
) -> EventAck:
    """Validate, deduplicate, guard against the current state, dispatch."""
    booking = session.booking
    request = booking.creation.request if booking.creation else None
    if request is None or request.reference != event.booking_reference:
        raise EventError("unknown_booking", "Booking reference does not match")

    seen = booking.workflow.seen_event_ids
    if event.event_id in seen:
        EVENTS.inc(type=event.type, result="duplicate")
        return EventAck("duplicate", original=seen[event.event_id])

    with trigger(f"event:{event.type}", event.event_id):
        try:
            ack = _dispatch(session, event, offer_provider, ranking, now, checkpoint)
        except OfferEventError as exc:
            raise EventError(exc.code, str(exc)) from exc
    _remember(session, event.event_id, ack.outcome)
    EVENTS.inc(type=event.type, result=ack.outcome)
    audit("event.handled", event_id=event.event_id, type=event.type, outcome=ack.outcome)
    return ack


def _dispatch(
    session: Session,
    event: ProviderEvent,
    offer_provider: OfferProvider,
    ranking: RankingPolicy,
    now: datetime,
    checkpoint: Callable[[], None] | None,
) -> EventAck:
    booking = session.booking
    if event.type.startswith("offer."):
        if booking.status not in OFFER_STATUSES:
            return EventAck("ignored")  # late/out-of-order: not eligible for selection
        try:
            payload = OfferEventPayload.model_validate(event.payload)
            offer = OfferEvent.model_validate(
                {
                    **payload.model_dump(),
                    "booking_reference": event.booking_reference,
                    "status": OFFER_EVENT_STATUS[event.type],
                    "updated_at": event.occurred_at,  # version stamp: older events lose
                }
            )
        except ValidationError as exc:
            raise EventError("invalid_offer", "malformed offer payload") from exc
        result = ingest(session, offer, offer_provider, ranking, now, checkpoint)
        return EventAck(result.outcome, result.message)

    if is_terminal(booking.status):
        return EventAck("ignored")  # never regress a completed or cancelled booking
    if event.type == "booking.expired" and booking.status == BookingStatus.failed:
        return EventAck("ignored")

    reason = event.payload.get("reason")
    cancelled = event.type == "booking.cancelled"
    transition(
        booking,
        BookingStatus.cancelled if cancelled else BookingStatus.failed,
        reason=reason if isinstance(reason, str) else event.type,
        now=now,
    )
    booking.next_actions = [NextAction(type=NextActionType.none)]
    if booking.offers:
        booking.offers.pending_approval = None
    text = reason if isinstance(reason, str) else None
    message = prompts.provider_cancelled(text) if cancelled else prompts.request_expired(text)
    return EventAck("cancelled" if cancelled else "failed", message)
