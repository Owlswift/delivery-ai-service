from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends

from app.api.v1.sessions import ERROR_RESPONSES, get_offer_provider, get_ranking, get_store
from app.errors import AppError
from app.events.handler import handle_event
from app.events.models import EventAck, EventError, EventResponse, ProviderEvent
from app.observability.context import attach
from app.offers.ranking import RankingPolicy
from app.persistence.serialize import run_exclusive
from app.persistence.session_store import SessionStore
from app.providers.protocols import OfferProvider
from app.providers.selection import Providers, providers_for
from app.schemas import Message, MessageRole, Session
from app.security.auth import ProviderPrincipal, require_provider

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventResponse, responses=ERROR_RESPONSES)
def receive_event(
    event: ProviderEvent,
    principal: Annotated[ProviderPrincipal, Depends(require_provider)],
    store: Annotated[SessionStore, Depends(get_store)],
    offer_provider: Annotated[OfferProvider, Depends(get_offer_provider)],
    ranking: Annotated[RankingPolicy, Depends(get_ranking)],
) -> EventResponse:
    company_id = principal.provider_id
    attach(provider_id=company_id, event_id=event.event_id)
    found = store.get_by_booking_reference(event.booking_reference, company_id)
    if found is None:
        raise AppError(404, "not_found", "Booking not found")

    def apply(session: Session) -> EventAck:
        now = datetime.now(UTC)
        active = providers_for(
            session.booking,
            now,
            Providers(None, None, offer_provider),  # type: ignore[arg-type]
        )
        try:
            ack = handle_event(
                session,
                event,
                active.offers,
                ranking,
                now,
                checkpoint=lambda: store.save(session),
            )
        except EventError as exc:
            raise AppError(
                404 if exc.code == "unknown_booking" else 422, exc.code, str(exc)
            ) from exc
        if ack.message:
            session.messages.append(
                Message(
                    id=uuid4(),
                    role=MessageRole.assistant,
                    content=ack.message,
                    channel=session.channel,
                    created_at=now,
                )
            )
        session.updated_at = now
        return ack

    ack = run_exclusive(store, found.id, company_id, apply)
    return EventResponse(result=ack.outcome, notified=ack.notified, original_result=ack.original)
