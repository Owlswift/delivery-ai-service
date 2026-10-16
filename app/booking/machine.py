from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from app.booking.models import BookingState, BookingStatus, Transition, WorkflowState
from app.observability.audit import audit
from app.observability.metrics import TRANSITIONS

S = WorkflowState
B = BookingStatus

STATE_OF: dict[BookingStatus, WorkflowState] = {
    B.collecting: S.COLLECTING_DETAILS,
    B.details_complete: S.AWAITING_FARE,
    B.address_clarification: S.AWAITING_FARE,
    B.budget_revision: S.AWAITING_FARE,
    B.ready_for_quote: S.AWAITING_FARE,
    B.awaiting_confirmation: S.AWAITING_USER_CONFIRMATION,
    B.ready_to_book: S.CREATING_BOOKING,
    B.booking_pending: S.CREATING_BOOKING,
    B.booking_created: S.AWAITING_OFFERS,
    B.offers_awaiting_approval: S.AWAITING_BUDGET_APPROVAL,
    B.assigning_courier: S.ASSIGNING_COURIER,
    B.courier_assigned: S.COURIER_ASSIGNED,
    B.failed: S.FAILED_OR_CANCELLED,
    B.cancelled: S.FAILED_OR_CANCELLED,
    B.completed: S.COURIER_ASSIGNED,
}

ALLOWED: dict[WorkflowState, frozenset[WorkflowState]] = {
    S.COLLECTING_DETAILS: frozenset({S.AWAITING_FARE, S.FAILED_OR_CANCELLED}),
    S.AWAITING_FARE: frozenset(
        {S.COLLECTING_DETAILS, S.AWAITING_USER_CONFIRMATION, S.FAILED_OR_CANCELLED}
    ),
    S.AWAITING_USER_CONFIRMATION: frozenset(
        {S.COLLECTING_DETAILS, S.AWAITING_FARE, S.CREATING_BOOKING, S.FAILED_OR_CANCELLED}
    ),
    S.CREATING_BOOKING: frozenset(
        {S.AWAITING_OFFERS, S.AWAITING_USER_CONFIRMATION, S.FAILED_OR_CANCELLED}
    ),
    S.AWAITING_OFFERS: frozenset(
        {S.AWAITING_BUDGET_APPROVAL, S.ASSIGNING_COURIER, S.FAILED_OR_CANCELLED}
    ),
    S.AWAITING_BUDGET_APPROVAL: frozenset(
        {S.AWAITING_OFFERS, S.ASSIGNING_COURIER, S.FAILED_OR_CANCELLED}
    ),
    S.ASSIGNING_COURIER: frozenset(
        {S.COURIER_ASSIGNED, S.AWAITING_OFFERS, S.AWAITING_BUDGET_APPROVAL, S.FAILED_OR_CANCELLED}
    ),
    S.COURIER_ASSIGNED: frozenset(),
    S.FAILED_OR_CANCELLED: frozenset({S.AWAITING_FARE}),  # recoverable `failed` only
}

TERMINAL_STATUSES = frozenset({B.courier_assigned, B.cancelled, B.completed})

_trigger: ContextVar[tuple[str, str | None]] = ContextVar(
    "workflow_trigger", default=("system", None)
)


class IllegalTransition(Exception):
    """Mapped to HTTP 409 `illegal_transition` by the app's exception handler."""

    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(f"Cannot move a booking from {from_status} to {to_status}")
        self.from_status = from_status
        self.to_status = to_status


@contextmanager
def trigger(name: str, event_id: str | None = None) -> Iterator[None]:
    token = _trigger.set((name, event_id))
    try:
        yield
    finally:
        _trigger.reset(token)


def state_of(status: BookingStatus) -> WorkflowState:
    return STATE_OF[status]


def is_terminal(status: BookingStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(old: BookingStatus, new: BookingStatus) -> bool:
    if old == new:
        return True
    if old in TERMINAL_STATUSES:
        return False
    a, b = STATE_OF[old], STATE_OF[new]
    if a == b:
        return True
    if a == S.FAILED_OR_CANCELLED and old != B.failed:
        return False
    return b in ALLOWED[a]


def init(booking: BookingState, now: datetime | None = None) -> None:
    """Record the first audit entry for a new booking."""
    if booking.workflow.history:
        return
    name, event_id = _trigger.get()
    booking.workflow.state = STATE_OF[booking.status]
    booking.workflow.history.append(
        Transition(
            seq=0,
            at=now or datetime.now(UTC),
            from_status=None,
            to_status=booking.status.value,
            from_state=None,
            to_state=booking.workflow.state,
            trigger="session_created",
            event_id=event_id,
        )
    )


def transition(
    booking: BookingState,
    new: BookingStatus,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Move the booking to `new` if legal. Records an audit entry on a real change."""
    old = booking.status
    if old == new:
        return False
    if not can_transition(old, new):
        raise IllegalTransition(old.value, new.value)
    name, event_id = _trigger.get()
    booking.status = new
    state = STATE_OF[new]
    workflow = booking.workflow
    workflow.history.append(
        Transition(
            seq=len(workflow.history),
            at=now or datetime.now(UTC),
            from_status=old.value,
            to_status=new.value,
            from_state=STATE_OF[old],
            to_state=state,
            trigger=name,
            reason=reason,
            event_id=event_id,
        )
    )
    workflow.state = state
    TRANSITIONS.inc(from_state=STATE_OF[old].value, to_state=state.value)
    audit(
        "booking.transition",
        from_status=old.value,
        to_status=new.value,
        from_state=STATE_OF[old].value,
        to_state=state.value,
        trigger=name,
        event_id=event_id,
    )
    return True
