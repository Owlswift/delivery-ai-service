"""Operation state and wire schemas. Must not import from app.collection."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.providers.models import (
    AddressResolution,
    AssignmentInfo,
    BookingPayload,
    FareQuote,
    ProviderBooking,
    ResolvedAddress,
)


class OperationType(StrEnum):
    resolve_address = "resolve_address"
    estimate_fare = "estimate_fare"
    create_booking = "create_booking"
    assign_offer = "assign_offer"
    get_booking_status = "get_booking_status"


class OperationStatus(StrEnum):
    pending = "pending"
    succeeded = "succeeded"
    rejected = "rejected"
    exhausted = "exhausted"


class Executor(StrEnum):
    provider_server = "provider_server"  # the only executor kind in v1


class Outcome(StrEnum):
    succeeded = "succeeded"
    rejected = "rejected"  # definite refusal or validation failure; never retried
    unavailable = "unavailable"  # not processed; retry with backoff
    unknown = "unknown"  # may have been processed; retry with the same key


class OperationError(BaseModel):
    code: str
    message: str
    fields: dict[str, str] | None = None


# ---- arguments (frozen wire schemas) ----------------------------------------
class ResolveAddressArgs(BaseModel):
    text: str


class EstimateFareArgs(BaseModel):
    pickup: ResolvedAddress
    destination: ResolvedAddress


class CreateBookingArgs(BaseModel):
    payload: BookingPayload  # includes the recorded user confirmation


class AssignOfferArgs(BaseModel):
    booking_reference: str
    offer_id: str


class GetBookingStatusArgs(BaseModel):
    booking_reference: str


# ---- results (frozen wire schemas) ------------------------------------------
class BookingStatusResult(BaseModel):
    booking_reference: str
    booking_status: str
    assignment: AssignmentInfo | None = None


ARGS_BY_TYPE: dict[OperationType, type[BaseModel]] = {
    OperationType.resolve_address: ResolveAddressArgs,
    OperationType.estimate_fare: EstimateFareArgs,
    OperationType.create_booking: CreateBookingArgs,
    OperationType.assign_offer: AssignOfferArgs,
    OperationType.get_booking_status: GetBookingStatusArgs,
}

RESULT_BY_TYPE: dict[OperationType, type[BaseModel]] = {
    OperationType.resolve_address: AddressResolution,
    OperationType.estimate_fare: FareQuote,
    OperationType.create_booking: ProviderBooking,
    OperationType.assign_offer: AssignmentInfo,
    OperationType.get_booking_status: BookingStatusResult,
}


# ---- persisted state ---------------------------------------------------------
class Operation(BaseModel):
    operation_id: str
    type: OperationType
    base_key: str
    seq: int = 1
    idempotency_key: str | None = None
    args: dict[str, Any]
    status: OperationStatus = OperationStatus.pending
    attempts: int = 0
    reconcile: bool = False
    available_at: datetime
    leased_until: datetime | None = None
    correlation_id: str
    created_at: datetime
    completed_at: datetime | None = None
    last_outcome: str | None = None
    result: dict[str, Any] | None = None
    error: OperationError | None = None
    consumed: bool = False  # result already handed to the engine (resolve/estimate)


class SuspendedTurn(BaseModel):
    """The quoting turn that was interrupted by a pending operation."""

    text: str
    details_changed: bool
    prev_status: str
    at: datetime


# ---- wire envelopes ------------------------------------------------------------
class OperationEnvelope(BaseModel):
    operation_id: str
    type: OperationType
    executor: Executor = Executor.provider_server
    provider_id: str
    session_id: str
    correlation_id: str
    idempotency_key: str | None = None
    attempt: int
    reconcile: bool = False
    timeout_s: int
    created_at: datetime
    lease_expires_at: datetime
    args: dict[str, Any]


class LeaseRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)


class OperationResultRequest(BaseModel):
    attempt: int = Field(ge=1)
    outcome: Outcome
    result: dict[str, Any] | None = None
    error: OperationError | None = None


class OperationAck(BaseModel):
    operation_id: str
    status: OperationStatus
    duplicate: bool = False
    notified: bool = False


ResultKind = Literal["succeeded", "rejected", "unavailable", "unknown"]
