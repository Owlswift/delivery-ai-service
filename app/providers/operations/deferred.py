from __future__ import annotations

from datetime import datetime, timedelta

from app.booking.models import BookingState
from app.providers.models import (
    AddressResolution,
    AssignmentInfo,
    BookingPayload,
    FareQuote,
    ProviderBooking,
    ResolvedAddress,
)
from app.providers.operations.book import OperationBook, OpsConfig, text_key
from app.providers.operations.models import (
    AssignOfferArgs,
    CreateBookingArgs,
    EstimateFareArgs,
    Operation,
    OperationStatus,
    OperationType,
    ResolveAddressArgs,
)
from app.providers.operations.pending import OperationPending
from app.providers.protocols import (
    BookingRejected,
    BookingTimeout,
    OfferTimeout,
    OfferUnavailable,
    ProviderError,
)


class DeferredProvider:
    """Implements the logistics, booking and offer provider protocols by recording
    operations for the provider's server-side executor instead of calling out.

    A call with no recorded result queues an operation and raises `OperationPending`.
    After the executor posts the result, the same call returns it (replay).
    """

    def __init__(self, booking: BookingState, now: datetime, config: OpsConfig) -> None:
        self.book = OperationBook(booking, now, config)
        self.now = now
        self.config = config

    # ---- non-idempotent reads: a result is handed over once, then a new call re-asks
    def _once(self, type: OperationType, base_key: str, args) -> dict:
        op = self.book.latest(base_key)
        if op is None or op.consumed:
            op = self.book.enqueue(type, base_key, args)
        if op.status == OperationStatus.pending:
            raise OperationPending(op.operation_id, type.value)
        op.consumed = True
        if op.status == OperationStatus.succeeded:
            assert op.result is not None
            return op.result
        raise ProviderError(op.error.message if op.error else f"{type.value} {op.status.value}")

    def resolve_address(self, text: str) -> AddressResolution:
        raw = self._once(
            OperationType.resolve_address,
            f"resolve:{text_key(text)}",
            ResolveAddressArgs(text=text),
        )
        return AddressResolution.model_validate(raw)

    def estimate_fare(self, pickup: ResolvedAddress, destination: ResolvedAddress) -> FareQuote:
        raw = self._once(
            OperationType.estimate_fare,
            f"estimate:{pickup.place_id}:{destination.place_id}",
            EstimateFareArgs(pickup=pickup, destination=destination),
        )
        return FareQuote.model_validate(raw)

    # ---- idempotent writes: same key always means the same operation
    def _idempotent(
        self, type: OperationType, base_key: str, args, key: str
    ) -> tuple[Operation, bool]:
        """Returns (operation, finished). Raises OperationPending while in flight."""
        op = self.book.latest(base_key)
        if op is None:
            op = self.book.enqueue(type, base_key, args, idempotency_key=key)
            raise OperationPending(op.operation_id, type.value)
        if op.status == OperationStatus.pending:
            raise OperationPending(op.operation_id, type.value)
        if op.status == OperationStatus.exhausted:
            cooldown = timedelta(seconds=self.config.exhausted_cooldown_s)
            if op.completed_at and self.now - op.completed_at >= cooldown:
                self.book.restart(op)  # same key: resending is the reconciliation
                raise OperationPending(op.operation_id, type.value)
            return op, False
        return op, True

    def create_booking(self, payload: BookingPayload, idempotency_key: str) -> ProviderBooking:
        op, finished = self._idempotent(
            OperationType.create_booking,
            f"create:{idempotency_key}",
            CreateBookingArgs(payload=payload),
            idempotency_key,
        )
        if not finished:
            raise BookingTimeout(idempotency_key)
        if op.status == OperationStatus.rejected:
            raise BookingRejected(op.error.message if op.error else "request refused")
        return ProviderBooking.model_validate(op.result)

    def find_booking(self, idempotency_key: str) -> ProviderBooking | None:
        op = self.book.latest(f"create:{idempotency_key}")
        if op is not None and op.status == OperationStatus.succeeded:
            return ProviderBooking.model_validate(op.result)
        return None

    def assign_offer(
        self, booking_reference: str, offer_id: str, idempotency_key: str
    ) -> AssignmentInfo:
        op, finished = self._idempotent(
            OperationType.assign_offer,
            f"assign:{idempotency_key}",
            AssignOfferArgs(booking_reference=booking_reference, offer_id=offer_id),
            idempotency_key,
        )
        if not finished:
            raise OfferTimeout(idempotency_key)
        if op.status == OperationStatus.rejected:
            raise OfferUnavailable(op.error.message if op.error else offer_id)
        return AssignmentInfo.model_validate(op.result)

    def find_assignment(self, booking_reference: str) -> AssignmentInfo | None:
        for op in self.book.booking.ops.values():
            if op.type == OperationType.assign_offer and op.status == OperationStatus.succeeded:
                return AssignmentInfo.model_validate(op.result)
        return None
