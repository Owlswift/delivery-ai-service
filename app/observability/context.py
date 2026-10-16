import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_context: ContextVar[dict[str, str] | None] = ContextVar("observability_context", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def current() -> dict[str, str]:
    return dict(_context.get() or {})


def attach(**fields: str | None) -> None:
    """Add identifiers for the rest of the current request. Request handlers run in their
    own copy of the context, so this does not leak between requests."""
    _context.set({**(_context.get() or {}), **{k: v for k, v in fields.items() if v}})


@contextmanager
def bind(**fields: str | None) -> Iterator[None]:
    """Attach identifiers (correlation_id, provider_id, session_id…) to every log line
    emitted inside the block."""
    merged = {**(_context.get() or {}), **{k: v for k, v in fields.items() if v}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)
