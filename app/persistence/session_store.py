from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.schemas import Message, Session


class ConcurrencyConflict(Exception):
    """The stored session changed since it was loaded (compare-and-swap failed)."""


class SessionStore(Protocol):
    def create(self, session: Session) -> Session: ...

    def get(self, session_id: UUID, company_id: str) -> Session | None: ...

    def save(self, session: Session) -> Session:
        """Compare-and-swap on `session.version`; raises ConcurrencyConflict."""

    def get_by_booking_reference(self, reference: str, company_id: str) -> Session | None: ...

    def list_by_booking_status(self, statuses: list[str]) -> list[Session]: ...

    def find_pending_operation_sessions(
        self, company_id: str, now: datetime, limit: int
    ) -> list[UUID]:
        """Sessions with at least one due, unleased operation."""

    def find_operation_session(self, operation_id: str, company_id: str) -> UUID | None: ...

    def get_idempotent_reply(self, session_id: UUID, client_message_id: UUID) -> Message | None: ...

    def remember_idempotent_reply(
        self, session_id: UUID, client_message_id: UUID, message: Message
    ) -> None: ...


class InMemorySessionStore:
    """Stores serialized copies so callers cannot mutate stored state without `save`."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, str] = {}
        self._idempotency: dict[tuple[UUID, UUID], Message] = {}

    def create(self, session: Session) -> Session:
        self._sessions[session.id] = session.model_dump_json()
        return session

    def _load(self, session_id: UUID) -> Session | None:
        raw = self._sessions.get(session_id)
        return Session.model_validate_json(raw) if raw else None

    def get(self, session_id: UUID, company_id: str) -> Session | None:
        session = self._load(session_id)
        if session is None or session.company_id != company_id:
            return None
        return session

    def save(self, session: Session) -> Session:
        current = self._load(session.id)
        if current is None or current.version != session.version:
            raise ConcurrencyConflict(str(session.id))
        session.version += 1
        self._sessions[session.id] = session.model_dump_json()
        return session

    def get_by_booking_reference(self, reference: str, company_id: str) -> Session | None:
        for sid in self._sessions:
            session = self._load(sid)
            request = session.booking.creation.request if session.booking.creation else None
            if session.company_id == company_id and request and request.reference == reference:
                return session
        return None

    def list_by_booking_status(self, statuses: list[str]) -> list[Session]:
        loaded = (self._load(sid) for sid in list(self._sessions))
        return [s for s in loaded if s and s.booking.status.value in statuses]

    def find_pending_operation_sessions(
        self, company_id: str, now: datetime, limit: int
    ) -> list[UUID]:
        found: list[UUID] = []
        for sid in list(self._sessions):
            session = self._load(sid)
            if session is None or session.company_id != company_id:
                continue
            for op in session.booking.ops.values():
                if (
                    op.status.value == "pending"
                    and op.available_at <= now
                    and (op.leased_until is None or op.leased_until <= now)
                ):
                    found.append(sid)
                    break
            if len(found) >= limit:
                break
        return found

    def find_operation_session(self, operation_id: str, company_id: str) -> UUID | None:
        for sid in list(self._sessions):
            session = self._load(sid)
            if session and session.company_id == company_id and operation_id in session.booking.ops:
                return sid
        return None

    def get_idempotent_reply(self, session_id: UUID, client_message_id: UUID) -> Message | None:
        return self._idempotency.get((session_id, client_message_id))

    def remember_idempotent_reply(
        self, session_id: UUID, client_message_id: UUID, message: Message
    ) -> None:
        self._idempotency[(session_id, client_message_id)] = message
