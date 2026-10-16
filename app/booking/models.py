"""The booking document: everything the service knows about one booking.

`Session.booking` is a `BookingState`. It composes parts owned by other modules (offers,
operations, provider value objects), which is why models are a shared data layer that
may import each other but never import logic. Sections below: workflow audit, collected
details, quote state, creation state, results, and the document itself.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.offers.models import OfferState
from app.providers.models import (
    AddressResolution,
    BookingPayload,
    FareQuote,
    ResolvedAddress,
)
from app.providers.operations.models import Operation, SuspendedTurn

# workflow audit
# --------------------------------------------------------------------------------------


class WorkflowState(StrEnum):
    COLLECTING_DETAILS = "COLLECTING_DETAILS"
    AWAITING_FARE = "AWAITING_FARE"
    AWAITING_USER_CONFIRMATION = "AWAITING_USER_CONFIRMATION"
    CREATING_BOOKING = "CREATING_BOOKING"
    AWAITING_OFFERS = "AWAITING_OFFERS"
    AWAITING_BUDGET_APPROVAL = "AWAITING_BUDGET_APPROVAL"
    ASSIGNING_COURIER = "ASSIGNING_COURIER"
    COURIER_ASSIGNED = "COURIER_ASSIGNED"
    FAILED_OR_CANCELLED = "FAILED_OR_CANCELLED"


class Transition(BaseModel):
    seq: int
    at: datetime
    from_status: str | None
    to_status: str
    from_state: WorkflowState | None
    to_state: WorkflowState
    trigger: str
    reason: str | None = None
    event_id: str | None = None


class WorkflowRecord(BaseModel):
    state: WorkflowState = WorkflowState.COLLECTING_DETAILS
    history: list[Transition] = Field(default_factory=list)
    seen_event_ids: dict[str, str] = Field(default_factory=dict)  # event_id -> outcome


# collected details
# --------------------------------------------------------------------------------------


class FieldStatus(StrEnum):
    missing = "missing"
    valid = "valid"
    ambiguous = "ambiguous"
    invalid = "invalid"


class CollectedField(BaseModel):
    value: str | None = None
    status: FieldStatus = FieldStatus.missing
    issue: str | None = None


class BudgetAmount(BaseModel):
    amount: str
    currency: str | None = None


USER_DETAIL_FIELDS = (
    "pickup_address",
    "destination_address",
    "package_description",
    "recipient_name",
    "recipient_phone",
    "sender_max_budget",
)


PROVIDER_DETAIL_FIELDS = ("sender_phone",)


REQUIRED_SLOTS = USER_DETAIL_FIELDS


class DeliveryDetails(BaseModel):
    pickup_address: CollectedField = Field(default_factory=CollectedField)
    destination_address: CollectedField = Field(default_factory=CollectedField)
    package_description: CollectedField = Field(default_factory=CollectedField)
    recipient_name: CollectedField = Field(default_factory=CollectedField)
    recipient_phone: CollectedField = Field(default_factory=CollectedField)
    sender_max_budget: CollectedField = Field(default_factory=CollectedField)
    sender_phone: CollectedField = Field(default_factory=CollectedField)
    budget_amount: str | None = None
    budget_currency: str | None = None


class DetailsCompleteResult(BaseModel):
    type: Literal["details_complete"] = "details_complete"
    pickup_address: str
    destination_address: str
    package_description: str
    recipient_name: str
    recipient_phone: str
    sender_max_budget: BudgetAmount
    sender_phone: str


# quote state
# --------------------------------------------------------------------------------------


class PresentedFare(BaseModel):
    base_fare: str
    currency: str


class Acceptance(BaseModel):
    quote_id: str
    base_fare: str
    currency: str
    fingerprint: str
    accepted_at: datetime


class QuoteState(BaseModel):
    pickup: AddressResolution | None = None
    destination: AddressResolution | None = None
    quote: FareQuote | None = None
    presented_fare: PresentedFare | None = None
    acceptance: Acceptance | None = None


# booking creation state
# --------------------------------------------------------------------------------------


class RequestStatus(StrEnum):
    pending = "pending"
    created = "created"


class BookingRequest(BaseModel):
    idempotency_key: str
    payload: BookingPayload
    status: RequestStatus = RequestStatus.pending
    attempts: int = 0
    reference: str | None = None
    provider_status: str | None = None


class CreationState(BaseModel):
    generation: int = 0
    request: BookingRequest | None = None
    last_error: str | None = None


# results and the document
# --------------------------------------------------------------------------------------


class QuoteSummary(BaseModel):
    quote_id: str
    base_fare: str
    currency: str
    expires_at: datetime


class ReadyToBookResult(BaseModel):
    type: Literal["ready_to_book"] = "ready_to_book"
    pickup: ResolvedAddress
    destination: ResolvedAddress
    quote: QuoteSummary
    details: DetailsCompleteResult


class BookingStatus(StrEnum):
    collecting = "collecting"
    details_complete = "details_complete"
    address_clarification = "address_clarification"
    budget_revision = "budget_revision"
    ready_to_book = "ready_to_book"
    booking_pending = "booking_pending"
    booking_created = "booking_created"
    offers_awaiting_approval = "offers_awaiting_approval"
    assigning_courier = "assigning_courier"
    courier_assigned = "courier_assigned"
    failed = "failed"
    ready_for_quote = "ready_for_quote"
    awaiting_confirmation = "awaiting_confirmation"
    completed = "completed"
    cancelled = "cancelled"


class NextActionType(StrEnum):
    ask_user = "ask_user"
    confirm = "confirm"
    wait = "wait"  # operations are pending with the provider
    none = "none"


class NextAction(BaseModel):
    type: NextActionType
    field: str | None = None
    prompt: str | None = None


class BookingCreatedResult(BaseModel):
    type: Literal["booking_created"] = "booking_created"
    reference: str
    provider_status: str
    payload: BookingPayload


class BookingState(BaseModel):
    status: BookingStatus = BookingStatus.collecting
    details: DeliveryDetails = Field(default_factory=DeliveryDetails)
    missing_fields: list[str] = Field(default_factory=lambda: list(USER_DETAIL_FIELDS))
    ambiguous_fields: list[str] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    result: DetailsCompleteResult | ReadyToBookResult | BookingCreatedResult | None = None
    quote: QuoteState | None = None
    creation: CreationState | None = None
    offers: OfferState | None = None
    workflow: WorkflowRecord = Field(default_factory=WorkflowRecord)
    ops: dict[str, Operation] = Field(default_factory=dict)
    suspended_turn: SuspendedTurn | None = None
