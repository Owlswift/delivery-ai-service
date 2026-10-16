"""Versioned SQLite migrations.

Each step is applied once, in order, inside its own transaction, and is written to be
safe on a database that already has the structure (databases created before migrations
existed are upgraded in place). Add a step by appending to `MIGRATIONS`; never edit or
reorder an applied step.

    python -m app.persistence.migrations [path]
"""

import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime

Step = tuple[int, str, Callable[[sqlite3.Connection], None]]


class MigrationError(Exception):
    pass


def _sessions_and_idempotency(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        "id TEXT PRIMARY KEY, company_id TEXT NOT NULL, data TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS idempotency ("
        "session_id TEXT NOT NULL, client_message_id TEXT NOT NULL, "
        "data TEXT NOT NULL, PRIMARY KEY (session_id, client_message_id))"
    )


def _session_version(conn: sqlite3.Connection) -> None:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
    if "version" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN version INTEGER NOT NULL DEFAULT 0")


def _operations_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS operations ("
        "operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "company_id TEXT NOT NULL, status TEXT NOT NULL, "
        "available_at REAL NOT NULL, leased_until REAL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS operations_due ON operations (company_id, status, available_at)"
    )


MIGRATIONS: list[Step] = [
    (1, "sessions and idempotency tables", _sessions_and_idempotency),
    (2, "optimistic-concurrency version on sessions", _session_version),
    (3, "derived operations index", _operations_index),
]


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    return [
        row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]


def migrate(conn: sqlite3.Connection, migrations: list[Step] | None = None) -> list[int]:
    """Bring the database up to date. Returns the versions applied by this call."""
    steps = sorted(MIGRATIONS if migrations is None else migrations, key=lambda s: s[0])
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
    done = set(applied_versions(conn))
    known = {version for version, _, _ in steps}
    if done - known:
        raise MigrationError(
            f"database has migrations {sorted(done - known)} that this version does not know; "
            "refusing to run against a newer schema"
        )
    applied: list[int] = []
    for version, name, step in steps:
        if version in done:
            continue
        # sqlite3 does not open a transaction for DDL on its own, so begin explicitly:
        # one transaction per step; a failure rolls the step back, earlier steps stay.
        conn.execute("BEGIN")
        try:
            step(conn)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (version, name, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        applied.append(version)
    return applied


if __name__ == "__main__":
    from app.config import get_settings

    path = sys.argv[1] if len(sys.argv) > 1 else get_settings().database_path
    connection = sqlite3.connect(path, timeout=30)
    print(f"{path}: applied {migrate(connection) or 'nothing (up to date)'}")
