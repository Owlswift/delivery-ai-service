from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.booking import creation as creation
from app.booking.machine import trigger
from app.booking.models import BookingStatus
from app.offers import engine as offers
from app.offers.ranking import RankingPolicy
from app.persistence.serialize import run_exclusive
from app.persistence.session_store import SessionStore
from app.providers.operations.pending import OperationPending
from app.providers.protocols import BookingProvider, OfferProvider
from app.schemas import Message, MessageRole, Session

IN_FLIGHT = [BookingStatus.booking_pending.value, BookingStatus.assigning_courier.value]


@dataclass
class RecoveryResult:
    session_id: UUID
    before: str
    after: str


def recover_pending(
    store: SessionStore,
    booking_provider: BookingProvider,
    offer_provider: OfferProvider,
    ranking: RankingPolicy,
    now: datetime | None = None,
    provider_factory: Callable[[Session, datetime], tuple[BookingProvider, OfferProvider]]
    | None = None,
) -> list[RecoveryResult]:
    """Reconcile bookings left mid-flight (restart, provider outage). Idempotent:
    still-unknown outcomes stay in their in-flight state for the next attempt."""
    now = now or datetime.now(UTC)
    results: list[RecoveryResult] = []
    for pending in store.list_by_booking_status(IN_FLIGHT):

        def reconcile(session: Session) -> RecoveryResult:
            booking = session.booking
            before = booking.status.value
            message = None
            bp, op = (
                provider_factory(session, now)
                if provider_factory
                else (booking_provider, offer_provider)
            )
            with trigger("recovery"):
                try:
                    if booking.status == BookingStatus.booking_pending:
                        message = creation.on_message(session, bp)
                    elif booking.status == BookingStatus.assigning_courier:
                        message = offers.evaluate(session, op, ranking, now)
                except OperationPending:
                    message = None  # re-queued for the executor; resumes on its result
            after = booking.status.value
            if after != before and message:
                session.messages.append(
                    Message(
                        id=uuid4(),
                        role=MessageRole.assistant,
                        content=message,
                        channel=session.channel,
                        created_at=now,
                    )
                )
                session.updated_at = now
            return RecoveryResult(session.id, before, after)

        results.append(run_exclusive(store, pending.id, pending.company_id, reconcile))
    return results
