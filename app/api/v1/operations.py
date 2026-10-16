from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends

from app.api.v1.sessions import ERROR_RESPONSES, get_geocoder, get_ranking, get_store
from app.booking.machine import trigger
from app.errors import AppError
from app.events.resume import resume
from app.observability.context import attach
from app.observability.logs import log
from app.offers.ranking import RankingPolicy
from app.persistence.serialize import run_exclusive
from app.persistence.session_store import SessionStore
from app.providers.geocoder import Geocoder
from app.providers.operations.book import InvalidResult, OperationBook, ops_config
from app.providers.operations.deferred import DeferredProvider
from app.providers.operations.models import (
    LeaseRequest,
    OperationAck,
    OperationEnvelope,
    OperationResultRequest,
    OperationStatus,
    Outcome,
)
from app.schemas import Message, MessageRole, Session
from app.security.auth import ProviderPrincipal, require_provider

router = APIRouter(prefix="/operations", tags=["operations"])


@router.post("/lease", response_model=list[OperationEnvelope], responses=ERROR_RESPONSES)
def lease_operations(
    body: LeaseRequest,
    principal: Annotated[ProviderPrincipal, Depends(require_provider)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> list[OperationEnvelope]:
    """Hand due operations to the provider's executor and hide them until the lease
    expires. Delivery is at-least-once; create and assign carry idempotency keys."""
    attach(provider_id=principal.provider_id, correlation_id=principal.correlation_id)
    now = datetime.now(UTC)
    config = ops_config()
    leased: list[OperationEnvelope] = []
    for session_id in store.find_pending_operation_sessions(principal.provider_id, now, body.limit):
        if len(leased) >= body.limit:
            break

        def take(session: Session, room: int = body.limit - len(leased)) -> list[OperationEnvelope]:
            book = OperationBook(session.booking, now, config)
            due = sorted(
                (op for op in session.booking.ops.values() if book.is_due(op)),
                key=lambda op: op.created_at,
            )
            return [book.lease(op, principal.provider_id, str(session.id)) for op in due[:room]]

        leased += run_exclusive(store, session_id, principal.provider_id, take)
    log("operations.leased", count=len(leased))
    return leased


@router.post("/{operation_id}/result", response_model=OperationAck, responses=ERROR_RESPONSES)
def post_result(
    operation_id: str,
    body: OperationResultRequest,
    principal: Annotated[ProviderPrincipal, Depends(require_provider)],
    store: Annotated[SessionStore, Depends(get_store)],
    geocoder: Annotated[Geocoder, Depends(get_geocoder)],
    ranking: Annotated[RankingPolicy, Depends(get_ranking)],
) -> OperationAck:
    """Record an executor's outcome and resume the workflow in the same atomic update."""
    provider_id = principal.provider_id
    attach(provider_id=provider_id, operation_id=operation_id)
    session_id = store.find_operation_session(operation_id, provider_id)
    if session_id is None:
        raise AppError(404, "not_found", "Operation not found")

    def apply(session: Session) -> OperationAck:
        now = datetime.now(UTC)
        config = ops_config()
        op = session.booking.ops.get(operation_id)
        if op is None:
            raise AppError(404, "not_found", "Operation not found")
        book = OperationBook(session.booking, now, config)

        if op.status != OperationStatus.pending:  # already closed: same outcome is a repeat
            same = body.outcome.value == op.last_outcome
            if same and body.outcome == Outcome.succeeded:
                try:
                    same = book._validate_result(op, body.result) == op.result
                except InvalidResult:
                    same = False
            if not same:
                raise AppError(409, "operation_closed", "Operation already completed")
            return OperationAck(operation_id=op.operation_id, status=op.status, duplicate=True)

        try:
            applied = book.apply(op, body)
        except InvalidResult as exc:
            raise AppError(422, "invalid_result", str(exc)) from exc
        if applied == "duplicate":
            return OperationAck(operation_id=op.operation_id, status=op.status, duplicate=True)
        if op.status == OperationStatus.pending:  # retry scheduled; nothing to resume yet
            return OperationAck(operation_id=op.operation_id, status=op.status)

        deferred = DeferredProvider(session.booking, now, config)
        with trigger(f"operation:{op.type.value}", op.operation_id):
            message = resume(session, deferred, geocoder, ranking, now)
        if message:
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
        return OperationAck(
            operation_id=op.operation_id, status=op.status, notified=message is not None
        )

    return run_exclusive(store, session_id, provider_id, apply)
