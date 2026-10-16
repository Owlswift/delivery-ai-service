from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from uuid import uuid5

from app.booking.machine import transition
from app.booking.models import BookingState, BookingStatus, NextAction, NextActionType
from app.booking.money import parse_budget
from app.offers import prompts
from app.offers.models import (
    AssignmentState,
    AssignmentStatus,
    Budget,
    BudgetChange,
    Decision,
    Offer,
    OfferEvent,
    OfferState,
    OfferStatus,
    PendingApproval,
)
from app.offers.ranking import RankingPolicy
from app.providers.models import AssignmentInfo
from app.providers.operations.pending import OperationPending
from app.providers.protocols import OfferProvider, OfferProviderError, OfferUnavailable

if TYPE_CHECKING:
    from app.schemas import Session

AFFIRMATIVE = re.compile(
    r"^\s*(yes|yep|yeah|y|ok|okay|sure|accept|approve|go ahead|confirm)\b", re.IGNORECASE
)
NEGATIVE = re.compile(r"\b(no|nope|decline|don'?t|do not|not now|stop)\b", re.IGNORECASE)
BUDGET_WORDS = re.compile(r"\b(budget|raise|increase|up to|pay)\b", re.IGNORECASE)


class OfferEventError(Exception):
    """The event is invalid or does not belong to this booking."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class IngestResult:
    outcome: str  # stored | updated | duplicate | stale | ignored
    message: str | None = None


def _reference(booking: BookingState) -> str | None:
    request = booking.creation.request if booking.creation else None
    return request.reference if request else None


def _ensure_state(booking: BookingState) -> OfferState:
    if booking.offers is None:
        request = booking.creation.request if booking.creation else None
        assert request is not None
        budget = request.payload.max_budget
        booking.offers = OfferState(
            budget=Budget(
                amount=budget.amount, currency=budget.currency or request.payload.currency
            )
        )
    return booking.offers


def _set_status(booking: BookingState, status: BookingStatus, action: NextAction) -> None:
    transition(booking, status)
    booking.next_actions = [action]


def _waiting_status(booking: BookingState) -> None:
    _set_status(booking, BookingStatus.booking_created, NextAction(type=NextActionType.none))


def _eligible(state: OfferState, now: datetime) -> list[Offer]:
    return [
        o
        for o in state.offers.values()
        if o.status == OfferStatus.open and (o.expires_at is None or o.expires_at > now)
    ]


def _budget(state: OfferState) -> Decimal:
    return Decimal(state.budget.amount)


def ingest(
    session: Session,
    event: OfferEvent,
    provider: OfferProvider,
    policy: RankingPolicy,
    now: datetime,
    checkpoint: Callable[[], None] | None = None,
) -> IngestResult:
    """Validate and store an offer, ignoring identical redeliveries, then re-evaluate.
    Event-id deduplication happens one level up, in `app.workflow.events`."""
    booking = session.booking
    reference = _reference(booking)
    if reference is None or event.booking_reference != reference:
        raise OfferEventError("unknown_booking", "Booking reference does not match")
    if not event.offer_id.strip():
        raise OfferEventError("invalid_offer", "offer_id is required")
    try:
        price = Decimal(event.price)
    except InvalidOperation:
        price = Decimal(0)
    if price <= 0:
        raise OfferEventError("invalid_offer", "price must be a positive amount")
    state = _ensure_state(booking)
    if event.currency != state.budget.currency:
        raise OfferEventError("invalid_offer", "currency does not match the booking")
    try:
        status = OfferStatus(event.status)
    except ValueError:
        status = None
    if status not in (OfferStatus.open, OfferStatus.withdrawn, OfferStatus.expired):
        raise OfferEventError("invalid_offer", "unknown offer status")

    if booking.status == BookingStatus.courier_assigned:
        return IngestResult("ignored")

    existing = state.offers.get(event.offer_id)
    if existing and existing.updated_at and event.updated_at:
        if event.updated_at < existing.updated_at:
            return IngestResult("stale")
    new_status = status
    if existing and existing.status == OfferStatus.unavailable and status == OfferStatus.open:
        new_status = OfferStatus.unavailable  # provider already refused this offer
    offer = Offer(
        offer_id=event.offer_id,
        courier=event.courier,
        price=format(price, "f"),
        currency=event.currency,
        status=new_status,
        expires_at=event.expires_at,
        pickup_distance_km=event.pickup_distance_km,
        pickup_eta_min=event.pickup_eta_min,
        received_at=existing.received_at if existing else now,
        updated_at=event.updated_at or (existing.updated_at if existing else None),
    )
    if existing is not None and existing.model_dump() == offer.model_dump():
        return IngestResult("duplicate")
    state.offers[offer.offer_id] = offer
    try:
        message = evaluate(session, provider, policy, now, checkpoint)
    except OperationPending:
        message = None  # offer stored; the assignment continues when the result arrives
    return IngestResult("updated" if existing else "stored", message)


def _confirm(booking: BookingState, state: OfferState, info: AssignmentInfo, now: datetime) -> str:
    assignment = state.assignment
    assert assignment is not None
    assignment.offer_id = info.offer_id
    assignment.courier_id = info.courier_id
    assignment.status = AssignmentStatus.confirmed
    assignment.confirmed_at = now
    state.pending_approval = None
    _set_status(booking, BookingStatus.courier_assigned, NextAction(type=NextActionType.none))
    offer = state.offers.get(info.offer_id)
    assert offer is not None
    return prompts.assigned(offer, _reference(booking))


def _send_assignment(
    booking: BookingState, state: OfferState, provider: OfferProvider, now: datetime
) -> tuple[str, str | None]:
    """Returns (outcome, message): assigned | rejected | unknown."""
    assignment = state.assignment
    assert assignment is not None
    reference = _reference(booking) or ""
    assignment.attempts += 1
    try:
        info = provider.assign_offer(reference, assignment.offer_id, assignment.idempotency_key)
    except OfferUnavailable:
        offer = state.offers.get(assignment.offer_id)
        if offer:
            offer.status = OfferStatus.unavailable
        state.assignment = None
        return "rejected", None
    except OfferProviderError:
        try:
            info = provider.find_assignment(reference)
        except OfferProviderError:
            info = None
        if info is None:
            return "unknown", None
    return "assigned", _confirm(booking, state, info, now)


def _assign(
    session: Session,
    state: OfferState,
    offer: Offer,
    provider: OfferProvider,
    now: datetime,
    approved: bool,
    checkpoint: Callable[[], None] | None,
) -> tuple[str, str | None]:
    state.assignment = AssignmentState(
        offer_id=offer.offer_id,
        courier_id=offer.courier.courier_id,
        idempotency_key=str(uuid5(session.id, f"assign:{offer.offer_id}")),
        approved_above_budget=approved,
    )
    state.pending_approval = None
    _set_status(
        session.booking, BookingStatus.assigning_courier, NextAction(type=NextActionType.none)
    )
    if checkpoint:
        checkpoint()  # persist the in-flight assignment before contacting the provider
    return _send_assignment(session.booking, state, provider, now)


def _reconcile(
    session: Session, state: OfferState, provider: OfferProvider, now: datetime
) -> tuple[str, str | None]:
    booking = session.booking
    try:
        info = provider.find_assignment(_reference(booking) or "")
    except OfferProviderError:
        return "unknown", None
    if info is not None:
        return "assigned", _confirm(booking, state, info, now)
    return _send_assignment(booking, state, provider, now)


def evaluate(
    session: Session,
    provider: OfferProvider,
    policy: RankingPolicy,
    now: datetime,
    checkpoint: Callable[[], None] | None = None,
) -> str | None:
    """Re-evaluate every stored offer. Returns a user message only on a meaningful change."""
    booking = session.booking
    state = _ensure_state(booking)
    if booking.status == BookingStatus.courier_assigned:
        return None
    if state.assignment and state.assignment.status == AssignmentStatus.requested:
        outcome, message = _reconcile(session, state, provider, now)
        if outcome == "assigned":
            return message
        if outcome == "unknown":
            return None

    rejected = False
    for _ in range(len(state.offers) + 1):
        within = [o for o in _eligible(state, now) if Decimal(o.price) <= _budget(state)]
        if not within:
            break
        ranking = policy.rank(within)
        chosen = ranking.ordered[0]
        state.decision = Decision(
            policy_version=policy.version,
            metric=ranking.metric,
            ranked_offer_ids=[o.offer_id for o in ranking.ordered],
            chosen_offer_id=chosen.offer_id,
            at=now,
        )
        state.pending_approval = None
        outcome, message = _assign(session, state, chosen, provider, now, False, checkpoint)
        if outcome == "assigned":
            return message
        if outcome == "unknown":
            return None  # stays assigning_courier; no other offer may be chosen meanwhile
        rejected = True

    eligible = _eligible(state, now)
    pending = state.pending_approval
    if pending is not None:
        offer = state.offers.get(pending.offer_id)
        if (
            offer in eligible
            and offer.price == pending.price
            and Decimal(offer.price) > _budget(state)
        ):
            _await_approval(booking)
            return None
        state.pending_approval = None
    candidates = [o for o in eligible if state.declined.get(o.offer_id) != o.price]
    if candidates:
        lowest = min(candidates, key=lambda o: (Decimal(o.price), o.received_at, o.offer_id))
        state.pending_approval = PendingApproval(
            offer_id=lowest.offer_id, price=lowest.price, requested_at=now
        )
        _await_approval(booking)
        return prompts.ask_above_budget(lowest, state.budget.amount, state.budget.currency)
    _waiting_status(booking)
    if pending is not None:
        return prompts.OFFER_GONE
    return prompts.ASSIGNMENT_FAILED if rejected else None


def _await_approval(booking: BookingState) -> None:
    _set_status(
        booking,
        BookingStatus.offers_awaiting_approval,
        NextAction(type=NextActionType.confirm, field="approve_offer"),
    )


def _budget_from(text: str, awaiting: bool) -> tuple[Decimal, str | None] | None:
    if not re.search(r"\d", text):
        return None
    if not (awaiting or BUDGET_WORDS.search(text)):
        return None
    return parse_budget(text)


def on_user_message(
    session: Session,
    text: str,
    provider: OfferProvider,
    policy: RankingPolicy,
    now: datetime,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    """Handle a turn after the booking exists (waiting, awaiting approval, assigned)."""
    booking = session.booking
    state = _ensure_state(booking)
    reference = _reference(booking)

    if booking.status == BookingStatus.courier_assigned:
        assignment = state.assignment
        assert assignment is not None
        return prompts.assigned(state.offers[assignment.offer_id], reference) + (
            " Changing or cancelling isn't supported here yet."
        )

    awaiting = booking.status == BookingStatus.offers_awaiting_approval
    parsed = _budget_from(text, awaiting)
    if parsed is not None:
        amount, currency = parsed
        if currency and currency != state.budget.currency:
            return prompts.budget_currency(state.budget.currency)
        state.budget.amount = format(amount, "f")
        state.budget.history.append(
            BudgetChange(amount=state.budget.amount, at=now, reason="user raised budget")
        )
        state.pending_approval = None
        state.declined.clear()
        return evaluate(session, provider, policy, now, checkpoint) or prompts.budget_updated(
            state.budget.amount, state.budget.currency
        )

    if awaiting and state.pending_approval is not None:
        if AFFIRMATIVE.search(text) and not NEGATIVE.search(text):
            return _approve(session, state, provider, policy, now, checkpoint)
        if NEGATIVE.search(text):
            state.declined[state.pending_approval.offer_id] = state.pending_approval.price
            state.pending_approval = None
            _waiting_status(booking)
            return prompts.DECLINED
        return prompts.REASK

    if state.assignment and state.assignment.status == AssignmentStatus.requested:
        outcome, message = _reconcile(session, state, provider, now)
        return message or (
            prompts.CONFIRMING if outcome == "unknown" else prompts.waiting(reference)
        )
    return prompts.waiting(reference)


def _approve(
    session: Session,
    state: OfferState,
    provider: OfferProvider,
    policy: RankingPolicy,
    now: datetime,
    checkpoint: Callable[[], None] | None,
) -> str:
    pending = state.pending_approval
    assert pending is not None
    offer = state.offers.get(pending.offer_id)
    if offer is None or offer not in _eligible(state, now) or offer.price != pending.price:
        state.pending_approval = None
        return evaluate(session, provider, policy, now, checkpoint) or prompts.OFFER_GONE
    outcome, message = _assign(session, state, offer, provider, now, True, checkpoint)
    if outcome == "assigned":
        return message or ""
    if outcome == "unknown":
        return prompts.CONFIRMING
    state.pending_approval = None
    return evaluate(session, provider, policy, now, checkpoint) or prompts.OFFER_GONE
