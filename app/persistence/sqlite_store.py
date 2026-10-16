import sqlite3
import threading
from datetime import datetime
from uuid import UUID

from app.persistence.migrations import migrate
from app.persistence.session_store import ConcurrencyConflict
from app.schemas import Message, Session


class SqliteSessionStore:
    """Durable session store; state survives process restarts. `save` is a
    compare-and-swap on the session version, safe across processes."""

    def __init__(self, path: str = "delivery_ai.db") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        with self._lock:
            migrate(self._conn)

    def _sync_operations(self, session: Session) -> None:
        """Derived index of the session's operations; rewritten on every save."""
        self._conn.execute("DELETE FROM operations WHERE session_id = ?", (str(session.id),))
        self._conn.executemany(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    op.operation_id,
                    str(session.id),
                    session.company_id,
                    op.status.value,
                    op.available_at.timestamp(),
                    op.leased_until.timestamp() if op.leased_until else None,
                )
                for op in session.booking.ops.values()
            ],
        )

    def create(self, session: Session) -> Session:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (id, company_id, data, version) VALUES (?, ?, ?, ?)",
                (str(session.id), session.company_id, session.model_dump_json(), session.version),
            )
            self._sync_operations(session)
        return session

    def get(self, session_id: UUID, company_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM sessions WHERE id = ? AND company_id = ?",
                (str(session_id), company_id),
            ).fetchone()
        return Session.model_validate_json(row[0]) if row else None

    def save(self, session: Session) -> Session:
        expected = session.version
        session.version = expected + 1
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET data = ?, version = ? WHERE id = ? AND version = ?",
                (session.model_dump_json(), session.version, str(session.id), expected),
            )
            if cursor.rowcount == 0:
                session.version = expected
                raise ConcurrencyConflict(str(session.id))
            self._sync_operations(session)
        return session

    def get_by_booking_reference(self, reference: str, company_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM sessions WHERE company_id = ? AND "
                "json_extract(data, '$.booking.creation.request.reference') = ?",
                (company_id, reference),
            ).fetchone()
        return Session.model_validate_json(row[0]) if row else None

    def list_by_booking_status(self, statuses: list[str]) -> list[Session]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM sessions WHERE json_extract(data, '$.booking.status') IN ({marks})",
                statuses,
            ).fetchall()
        return [Session.model_validate_json(r[0]) for r in rows]

    def find_pending_operation_sessions(
        self, company_id: str, now: datetime, limit: int
    ) -> list[UUID]:
        ts = now.timestamp()
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT session_id FROM operations WHERE company_id = ? AND "
                "status = 'pending' AND available_at <= ? AND "
                "(leased_until IS NULL OR leased_until <= ?) LIMIT ?",
                (company_id, ts, ts, limit),
            ).fetchall()
        return [UUID(r[0]) for r in rows]

    def find_operation_session(self, operation_id: str, company_id: str) -> UUID | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id FROM operations WHERE operation_id = ? AND company_id = ?",
                (operation_id, company_id),
            ).fetchone()
        return UUID(row[0]) if row else None

    def get_idempotent_reply(self, session_id: UUID, client_message_id: UUID) -> Message | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM idempotency WHERE session_id = ? AND client_message_id = ?",
                (str(session_id), str(client_message_id)),
            ).fetchone()
        return Message.model_validate_json(row[0]) if row else None

    def remember_idempotent_reply(
        self, session_id: UUID, client_message_id: UUID, message: Message
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO idempotency VALUES (?, ?, ?)",
                (str(session_id), str(client_message_id), message.model_dump_json()),
            )
