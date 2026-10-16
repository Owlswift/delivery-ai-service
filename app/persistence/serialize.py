from collections.abc import Callable
from uuid import UUID

from app.errors import AppError
from app.observability.logs import log
from app.observability.metrics import SESSION_CONFLICTS
from app.persistence.locks import session_lock
from app.persistence.session_store import ConcurrencyConflict, SessionStore
from app.schemas import Session


def run_exclusive[T](
    store: SessionStore,
    session_id: UUID,
    company_id: str,
    fn: Callable[[Session], T],
    retries: int = 5,
) -> T:
    """Load, run `fn`, save with compare-and-swap. Serialised in-process by a lock;
    across processes a lost race reloads and runs again. Re-running is safe because
    outbound provider calls carry deterministic idempotency keys."""
    with session_lock(session_id):
        for _ in range(retries):
            session = store.get(session_id, company_id)
            if session is None:
                raise AppError(404, "not_found", "Session not found")
            try:
                result = fn(session)
                store.save(session)
                return result
            except ConcurrencyConflict:
                SESSION_CONFLICTS.inc()
                log("session.conflict", session_id=str(session_id))
                continue
    raise AppError(409, "conflict", "Session is busy; please retry")
