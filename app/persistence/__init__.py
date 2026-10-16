"""Persistence: sessions, locking and optimistic concurrency.

session_store  store protocol, in-memory store, `ConcurrencyConflict`
sqlite_store   durable SQLite store (compare-and-swap saves, operation index)
migrations     versioned schema migrations
serialize      `run_exclusive`: lock, load, run, compare-and-swap save, retry
locks          per-session in-process locks
"""
