from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from app.booking.models import BookingState
from app.observability.audit import audit
from app.observability.metrics import OPERATION_LATENCY, OPERATIONS
from app.providers.operations.models import (
    ARGS_BY_TYPE,
    RESULT_BY_TYPE,
    AssignOfferArgs,
    EstimateFareArgs,
    GetBookingStatusArgs,
    Operation,
    OperationEnvelope,
    OperationError,
    OperationResultRequest,
    OperationStatus,
    OperationType,
    Outcome,
)


@dataclass
class OpsConfig:
    max_attempts: int = 5
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 60.0
    lease_s: int = 60
    timeout_s: int = 30
    exhausted_cooldown_s: int = 300


def ops_config() -> OpsConfig:
    from app.config import get_settings

    s = get_settings()
    return OpsConfig(
        max_attempts=s.operation_max_attempts,
        backoff_base_s=s.operation_backoff_base_s,
        backoff_cap_s=s.operation_backoff_cap_s,
        lease_s=s.operation_lease_s,
        timeout_s=s.operation_timeout_s,
        exhausted_cooldown_s=s.operation_exhausted_cooldown_s,
    )


def text_key(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:16]


class InvalidResult(Exception):
    """The posted result does not satisfy the frozen schema or contradicts the arguments."""


class OperationBook:
    """Operations recorded on one booking (`booking.ops`)."""

    def __init__(self, booking: BookingState, now: datetime, config: OpsConfig) -> None:
        self.booking = booking
        self.now = now
        self.config = config

    def latest(self, base_key: str) -> Operation | None:
        found = [o for o in self.booking.ops.values() if o.base_key == base_key]
        return max(found, key=lambda o: o.seq) if found else None

    def enqueue(
        self,
        type: OperationType,
        base_key: str,
        args: BaseModel,
        idempotency_key: str | None = None,
        reconcile: bool = False,
    ) -> Operation:
        previous = self.latest(base_key)
        op = Operation(
            operation_id=str(uuid4()),
            type=type,
            base_key=base_key,
            seq=previous.seq + 1 if previous else 1,
            idempotency_key=idempotency_key,
            args=args.model_dump(mode="json"),
            reconcile=reconcile,
            available_at=self.now,
            correlation_id=str(uuid4()),
            created_at=self.now,
        )
        self.booking.ops[op.operation_id] = op
        # superseded finished operations for the same key are dropped so the session
        # record does not grow without bound; the audit trail keeps the history
        for old in [
            o
            for o in self.booking.ops.values()
            if o.base_key == base_key and o.seq < op.seq and o.status != OperationStatus.pending
        ]:
            del self.booking.ops[old.operation_id]
        OPERATIONS.inc(type=type.value, event="enqueued")
        return op

    def restart(self, op: Operation) -> None:
        op.status = OperationStatus.pending
        op.attempts = 0
        op.reconcile = True
        op.available_at = self.now
        op.leased_until = None
        op.completed_at = None

    def is_due(self, op: Operation) -> bool:
        return (
            op.status == OperationStatus.pending
            and op.available_at <= self.now
            and (op.leased_until is None or op.leased_until <= self.now)
        )

    def lease(self, op: Operation, provider_id: str, session_id: str) -> OperationEnvelope:
        op.leased_until = self.now + timedelta(seconds=self.config.lease_s)
        OPERATIONS.inc(type=op.type.value, event="leased")
        return OperationEnvelope(
            operation_id=op.operation_id,
            type=op.type,
            provider_id=provider_id,
            session_id=session_id,
            correlation_id=op.correlation_id,
            idempotency_key=op.idempotency_key,
            attempt=op.attempts + 1,
            reconcile=op.reconcile,
            timeout_s=self.config.timeout_s,
            created_at=op.created_at,
            lease_expires_at=op.leased_until,
            args=op.args,
        )

    def _validate_result(self, op: Operation, raw: dict | None) -> dict:
        if raw is None:
            raise InvalidResult("result is required for a succeeded outcome")
        try:
            parsed = RESULT_BY_TYPE[op.type].model_validate(raw)
        except ValidationError as exc:
            raise InvalidResult("result does not match the schema") from exc
        args = ARGS_BY_TYPE[op.type].model_validate(op.args)
        if isinstance(args, EstimateFareArgs):
            if (parsed.pickup_place_id, parsed.destination_place_id) != (
                args.pickup.place_id,
                args.destination.place_id,
            ):
                raise InvalidResult("quote is not for the requested locations")
        elif isinstance(args, AssignOfferArgs):
            if parsed.offer_id != args.offer_id:
                raise InvalidResult("assignment is not for the requested offer")
        elif isinstance(args, GetBookingStatusArgs):
            if parsed.booking_reference != args.booking_reference:
                raise InvalidResult("status is not for the requested booking")
        return parsed.model_dump(mode="json")

    def apply(self, op: Operation, request: OperationResultRequest) -> str:
        """Apply an executor's result to a pending operation. Returns "applied" or
        "duplicate" (stale attempt); raises InvalidResult for a bad result."""
        if request.attempt != op.attempts + 1:
            return "duplicate"
        cfg = self.config
        if request.outcome == Outcome.succeeded:
            op.result = self._validate_result(op, request.result)
            op.status = OperationStatus.succeeded
        elif request.outcome == Outcome.rejected:
            op.error = request.error or OperationError(code="rejected", message="request refused")
            op.status = OperationStatus.rejected
        else:
            op.error = request.error
            if op.attempts + 1 >= cfg.max_attempts:
                op.status = OperationStatus.exhausted
            else:
                delay = min(cfg.backoff_cap_s, cfg.backoff_base_s * 2**op.attempts)
                op.available_at = self.now + timedelta(seconds=delay)
                op.reconcile = request.outcome == Outcome.unknown
        op.attempts += 1
        op.last_outcome = request.outcome.value
        op.leased_until = None
        if op.status != OperationStatus.pending:
            op.completed_at = self.now
            OPERATION_LATENCY.observe(
                (self.now - op.created_at).total_seconds(), type=op.type.value
            )
        event = op.status.value if op.status != OperationStatus.pending else "retry"
        OPERATIONS.inc(type=op.type.value, event=event)
        audit(
            "operation.result",
            operation_id=op.operation_id,
            type=op.type.value,
            outcome=request.outcome.value,
            status=op.status.value,
            attempts=op.attempts,
            correlation_id=op.correlation_id,
        )
        return "applied"
