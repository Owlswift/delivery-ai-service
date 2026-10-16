from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from app.config import get_settings
from app.conversation.engine import start_collection
from app.conversation.extract import RuleBasedExtractor
from app.conversation.messages import assert_accepts_messages, assistant_message, greeting_message
from app.conversation.turn import handle_user_turn
from app.conversation.validate import normalize_phone
from app.errors import AppError, ErrorResponse
from app.offers.ranking import NearestPickupPolicy, RankingPolicy
from app.persistence.serialize import run_exclusive
from app.persistence.session_store import SessionStore
from app.persistence.sqlite_store import SqliteSessionStore
from app.providers.fake_booking import FakeBookingProvider
from app.providers.fake_logistics import FakeLogisticsProvider
from app.providers.fake_offers import FakeOfferProvider
from app.providers.geocoder import Geocoder, UnconfiguredGeocoder
from app.providers.google_geocoder import GoogleMapsGeocoder
from app.providers.protocols import BookingProvider, LogisticsProvider, OfferProvider
from app.providers.selection import Providers, providers_for
from app.schemas import (
    CreateSessionRequest,
    Message,
    MessageRole,
    MessageTurnResponse,
    SendMessageRequest,
    Session,
    SessionStatus,
)
from app.security.auth import mint_client_token, require_company_id, require_session_access

router = APIRouter(prefix="/sessions", tags=["sessions"])

_extractor = RuleBasedExtractor()
_store: SessionStore | None = None


_provider: LogisticsProvider = FakeLogisticsProvider()


_booking_provider: BookingProvider = FakeBookingProvider()


_offer_provider: OfferProvider = FakeOfferProvider()
_ranking: RankingPolicy = NearestPickupPolicy()


def get_offer_provider() -> OfferProvider:
    return _offer_provider


def get_ranking() -> RankingPolicy:
    return _ranking


_geocoder: Geocoder | None = None


def get_geocoder() -> Geocoder:
    global _geocoder
    if _geocoder is None:
        settings = get_settings()
        _geocoder = (
            GoogleMapsGeocoder(
                settings.google_maps_api_key.get_secret_value(), settings.geocoder_timeout_s
            )
            if settings.google_maps_api_key
            else UnconfiguredGeocoder()
        )
    return _geocoder


def get_booking_provider() -> BookingProvider:
    return _booking_provider


def get_provider() -> LogisticsProvider:
    return _provider


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SqliteSessionStore(get_settings().database_path)
    return _store


def _now() -> datetime:
    return datetime.now(UTC)


ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=Session,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def create_session(
    body: CreateSessionRequest,
    company_id: Annotated[str, Depends(require_company_id)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> Session:
    sender_phone = normalize_phone(body.sender_phone)
    if sender_phone is None:
        raise AppError(
            422,
            "validation_error",
            "sender_phone must contain 8 to 15 digits",
            {"field": "sender_phone"},
        )
    now = _now()
    greeting = greeting_message(body.channel)
    session = Session(
        id=uuid4(),
        company_id=company_id,
        status=SessionStatus.active,
        locale=body.locale,
        channel=body.channel,
        external_ref=body.external_ref,
        metadata=body.metadata,
        messages=[greeting],
        booking=start_collection(sender_phone),
        created_at=now,
        updated_at=now,
    )
    return store.create(session)


@router.get(
    "/{session_id}",
    response_model=Session,
    responses=ERROR_RESPONSES,
)
def get_session(
    session_id: UUID,
    company_id: Annotated[str, Depends(require_session_access)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> Session:
    session = store.get(session_id, company_id)
    if session is None:
        raise AppError(404, "not_found", "Session not found")
    return session


@router.post(
    "/{session_id}/messages",
    response_model=MessageTurnResponse,
    responses=ERROR_RESPONSES,
)
def send_message(
    session_id: UUID,
    body: SendMessageRequest,
    company_id: Annotated[str, Depends(require_session_access)],
    store: Annotated[SessionStore, Depends(get_store)],
    provider: Annotated[LogisticsProvider, Depends(get_provider)],
    booking_provider: Annotated[BookingProvider, Depends(get_booking_provider)],
    offer_provider: Annotated[OfferProvider, Depends(get_offer_provider)],
    ranking: Annotated[RankingPolicy, Depends(get_ranking)],
    geocoder: Annotated[Geocoder, Depends(get_geocoder)],
) -> MessageTurnResponse:
    def apply(session: Session) -> tuple[Message, Message | None]:
        if body.client_message_id is not None:
            cached = store.get_idempotent_reply(session_id, body.client_message_id)
            if cached is not None:
                return cached, None
        assert_accepts_messages(session)
        channel = body.channel or session.channel
        session.messages.append(
            Message(
                id=uuid4(),
                role=MessageRole.user,
                content=body.content,
                channel=channel,
                created_at=_now(),
            )
        )
        now = _now()
        active = providers_for(
            session.booking, now, Providers(provider, booking_provider, offer_provider)
        )
        reply = handle_user_turn(
            session,
            body.content,
            _extractor,
            active.logistics,
            now=now,
            booking_provider=active.booking,
            checkpoint=lambda: store.save(session),
            offer_provider=active.offers,
            ranking=ranking,
            geocoder=geocoder,
        )
        assistant = assistant_message(reply, channel)
        session.messages.append(assistant)
        session.updated_at = _now()
        return assistant, assistant

    assistant, fresh = run_exclusive(store, session_id, company_id, apply)
    if fresh is not None and body.client_message_id is not None:
        store.remember_idempotent_reply(session_id, body.client_message_id, assistant)
    session = store.get(session_id, company_id)
    assert session is not None
    return MessageTurnResponse(message=assistant, session=session)


class ClientTokenResponse(BaseModel):
    token: str
    expires_at: datetime


@router.post(
    "/{session_id}/client-token",
    response_model=ClientTokenResponse,
    responses=ERROR_RESPONSES,
)
def create_client_token(
    session_id: UUID,
    company_id: Annotated[str, Depends(require_company_id)],
    store: Annotated[SessionStore, Depends(get_store)],
) -> ClientTokenResponse:
    """Server-side only: mints a short-lived token for the frontend SDK, valid for this
    session's messages and reads and nothing else."""
    if store.get(session_id, company_id) is None:
        raise AppError(404, "not_found", "Session not found")
    token, expires = mint_client_token(get_settings(), session_id, company_id, _now())
    return ClientTokenResponse(token=token, expires_at=expires)
