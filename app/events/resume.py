from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.booking import creation as creation
from app.booking.models import BookingStatus
from app.booking.quoting import advance
from app.offers import engine as offers
from app.offers.ranking import RankingPolicy
from app.providers.geocoder import Geocoder
from app.providers.operations.deferred import DeferredProvider
from app.providers.operations.pending import OperationPending

if TYPE_CHECKING:
    from app.schemas import Session

QUOTE_PHASE = frozenset(
    {
        BookingStatus.details_complete,
        BookingStatus.address_clarification,
        BookingStatus.budget_revision,
        BookingStatus.awaiting_confirmation,
        BookingStatus.ready_to_book,
    }
)
REPLAY_WITHOUT_TURN = frozenset(
    {
        BookingStatus.details_complete,
        BookingStatus.address_clarification,
        BookingStatus.budget_revision,
    }
)
OFFER_PHASE = frozenset(
    {
        BookingStatus.booking_created,
        BookingStatus.offers_awaiting_approval,
        BookingStatus.assigning_courier,
    }
)


def resume(
    session: Session,
    deferred: DeferredProvider,
    geocoder: Geocoder | None,
    ranking: RankingPolicy,
    now: datetime,
) -> str | None:
    """Continue the workflow after an operation result arrived. Returns a message for
    the user when the continuation produced one. Terminal, failed and cancelled
    bookings are left untouched."""
    booking = session.booking
    status = booking.status
    try:
        if status in QUOTE_PHASE:
            turn = booking.suspended_turn
            if turn is None and status not in REPLAY_WITHOUT_TURN:
                return None  # nothing was waiting on this result
            reply = advance(
                session,
                turn.text if turn else "",
                details_changed=turn.details_changed if turn else False,
                prev_status=BookingStatus(turn.prev_status) if turn else status,
                provider=deferred,
                now=now,
                geocoder=geocoder,
            )
            booking.suspended_turn = None  # the quoting step is done; submission is next
            if booking.status == BookingStatus.ready_to_book:
                reply = creation.submit(session, deferred, deferred, now)
            return reply
        if status == BookingStatus.booking_pending:
            return creation.on_message(session, deferred)
        if status in OFFER_PHASE:
            return offers.evaluate(session, deferred, ranking, now)
    except OperationPending:
        return None
    return None
