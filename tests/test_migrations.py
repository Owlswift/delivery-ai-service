import sqlite3

import pytest

from app.persistence.migrations import MIGRATIONS, MigrationError, applied_versions, migrate
from app.persistence.sqlite_store import SqliteSessionStore


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_gets_every_step_once() -> None:
    conn = sqlite3.connect(":memory:")
    assert migrate(conn) == [1, 2, 3]
    assert {"sessions", "idempotency", "operations", "schema_migrations"} <= tables(conn)
    assert applied_versions(conn) == [1, 2, 3]
    assert migrate(conn) == []  # up to date: nothing to do


def test_database_created_before_migrations_is_upgraded_in_place() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, company_id TEXT NOT NULL, data TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO sessions VALUES ('s1', 'acme', '{}')")
    conn.commit()
    assert migrate(conn) == [1, 2, 3]
    assert conn.execute("SELECT id, company_id, version FROM sessions").fetchall() == [
        ("s1", "acme", 0)
    ]


def test_a_failing_step_rolls_back_and_leaves_earlier_steps_applied() -> None:
    conn = sqlite3.connect(":memory:")

    def broken(c):
        c.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("boom")

    steps = [*MIGRATIONS, (4, "broken", broken)]
    with pytest.raises(RuntimeError):
        migrate(conn, steps)
    assert applied_versions(conn) == [1, 2, 3]
    assert "half_done" not in tables(conn)


def test_database_from_a_newer_version_is_refused() -> None:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    conn.execute("INSERT INTO schema_migrations VALUES (99, 'future', 'now')")
    conn.commit()
    with pytest.raises(MigrationError, match="newer schema"):
        migrate(conn)


def test_store_migrates_on_open_and_reopening_is_harmless(tmp_path) -> None:
    path = str(tmp_path / "m.db")
    SqliteSessionStore(path)
    SqliteSessionStore(path)
    conn = sqlite3.connect(path)
    assert applied_versions(conn) == [1, 2, 3]
