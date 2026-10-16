import threading
from collections import defaultdict
from uuid import UUID

_guard = threading.Lock()
_locks: dict[UUID, threading.RLock] = defaultdict(threading.RLock)


def session_lock(session_id: UUID) -> threading.RLock:
    """Serialises load-modify-save on one session. Single-process only."""
    with _guard:
        return _locks[session_id]
