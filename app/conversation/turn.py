"""One user turn: collection, then address/fare quoting, then booking creation."""

import re
from collections.abc import Callable
from datetime import UTC, datetime

from app.booking import creation as creation
from app.booking import prompts as wf_prompts
from app.booking.machine import transition, trigger
from app.booking.models import BookingState, BookingStatus, NextAction, NextActionType
from app.booking.quoting import advance, detail_values, select_candidate
from app.conversation.engine import apply_user_turn, refresh
from app.conversation.extract import Extractor
from app.offers import engine as offers
from app.offers.ranking import NearestPickupPolicy, RankingPolicy
from app.providers.geocoder import Geocoder
from app.providers.operations.models import SuspendedTurn
from app.providers.operations.pending import OperationPending
from app.providers.protocols import BookingProvider, LogisticsProvider, OfferProvider
from app.schemas import Session


def handle_user_turn(
    session: Session,
    text: str,
    extractor: Extractor,
    provider: LogisticsProvider,
    now: datetime | None = None,
    booking_provider: BookingProvider | None = None,
    checkpoint: Callable[[], None] | None = None,
    offer_provider: OfferProvider | None = None,
    ranking: RankingPolicy | None = None,
    geocoder: Geocoder | None = None,
) -> str:
    """`booking_provider=None` runs the conversation up to `ready_to_book` only.
    `checkpoint` persists the session; creation calls it before contacting the provider."""
    now = now or datetime.now(UTC)
    with trigger("user_message"):
        return _turn(
            session,
            text,
            extractor,
            provider,
            now,
            booking_provider,
            checkpoint,
            offer_provider,
            ranking,
            geocoder,
        )


CANCEL = re.compile(r"^\s*(please\s+)?(cancel|abort|never\s*mind|forget\s+it)\b", re.IGNORECASE)
RETRY = re.compile(r"^\s*(yes|yep|yeah|ok|okay|sure|retry|try again|please do)\b", re.IGNORECASE)
PRE_CREATION = {
    BookingStatus.collecting,
    BookingStatus.details_complete,
    BookingStatus.address_clarification,
    BookingStatus.budget_revision,
    BookingStatus.ready_for_quote,
    BookingStatus.awaiting_confirmation,
    BookingStatus.ready_to_book,
}


def _suspend(
    booking: BookingState,
    text: str,
    details_changed: bool,
    prev_status: BookingStatus,
    now: datetime,
) -> str:
    """A provider operation is pending: remember the turn so the result can replay it."""
    booking.suspended_turn = SuspendedTurn(
        text=text, details_changed=details_changed, prev_status=prev_status.value, at=now
    )
    booking.next_actions = [NextAction(type=NextActionType.wait)]
    return wf_prompts.CHECKING


def _retry_after_failure(
    session: Session, now: datetime, provider: LogisticsProvider, geocoder: Geocoder | None
) -> str:
    """failed -> AWAITING_FARE: re-resolve, re-quote, ask the user to confirm again."""
    booking = session.booking
    transition(booking, BookingStatus.details_complete, reason="user retry")
    if booking.creation:
        booking.creation.request = None
        booking.creation.generation += 1  # new idempotency key for the new request
    booking.offers = None
    booking.quote = None
    refresh(booking)
    return advance(
        session,
        "",
        details_changed=True,
        prev_status=BookingStatus.details_complete,
        provider=provider,
        now=now,
        geocoder=geocoder,
    )


def _turn(
    session: Session,
    text: str,
    extractor: Extractor,
    provider: LogisticsProvider,
    now: datetime,
    booking_provider: BookingProvider | None,
    checkpoint: Callable[[], None] | None,
    offer_provider: OfferProvider | None,
    ranking: RankingPolicy | None,
    geocoder: Geocoder | None,
) -> str:
    booking = session.booking

    if booking.status == BookingStatus.cancelled:
        return wf_prompts.CANCELLED
    if booking.status == BookingStatus.failed:
        if RETRY.search(text):
            try:
                return _retry_after_failure(session, now, provider, geocoder)
            except OperationPending:
                return _suspend(booking, text, True, BookingStatus.details_complete, now)
        return wf_prompts.failed(
            booking.workflow.history[-1].reason if booking.workflow.history else None
        )
    if CANCEL.search(text):
        if booking.status in PRE_CREATION:
            transition(booking, BookingStatus.cancelled, reason="user cancelled", now=now)
            booking.next_actions = [NextAction(type=NextActionType.none)]
            booking.result = None
            return wf_prompts.USER_CANCELLED
        if booking.status == BookingStatus.booking_pending:
            return wf_prompts.PENDING_NOT_CANCELLABLE
        return wf_prompts.not_cancellable(
            booking.creation.request.reference
            if booking.creation and booking.creation.request
            else None
        )

    if offer_provider is not None and booking.status in (
        BookingStatus.booking_created,
        BookingStatus.offers_awaiting_approval,
        BookingStatus.assigning_courier,
        BookingStatus.courier_assigned,
    ):
        try:
            return offers.on_user_message(
                session, text, offer_provider, ranking or NearestPickupPolicy(), now, checkpoint
            )
        except OperationPending:
            return wf_prompts.CHECKING

    if booking_provider is not None and booking.status in (
        BookingStatus.booking_pending,
        BookingStatus.booking_created,
    ):
        try:
            return creation.on_message(session, booking_provider, checkpoint)
        except OperationPending:
            return wf_prompts.CHECKING

    prev_status = booking.status
    before = detail_values(booking)
    changed = False
    try:
        if select_candidate(booking, text):
            reply = advance(
                session,
                text,
                details_changed=False,
                prev_status=prev_status,
                provider=provider,
                now=now,
                geocoder=geocoder,
            )
        else:
            reply = apply_user_turn(session, text, extractor)
            if booking.status == BookingStatus.collecting:
                return reply
            changed = detail_values(booking) != before
            reply = advance(
                session,
                text,
                details_changed=changed,
                prev_status=prev_status,
                provider=provider,
                now=now,
                geocoder=geocoder,
            )
    except OperationPending:
        return _suspend(booking, text, changed, prev_status, now)
    booking.suspended_turn = None  # the quoting step is done; submission is next
    if booking_provider is not None and booking.status == BookingStatus.ready_to_book:
        try:
            reply = creation.submit(session, booking_provider, provider, now, checkpoint)
        except OperationPending:
            booking.next_actions = [NextAction(type=NextActionType.wait)]
            return wf_prompts.CHECKING
    return reply
