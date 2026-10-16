import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.observability.logs import log
from app.observability.metrics import SPAN_LATENCY


@contextmanager
def span(name: str, **fields: object) -> Iterator[None]:
    """Time a named step; record it as a metric and a debug log line. The correlation id
    from the surrounding request is the trace id."""
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        elapsed = time.perf_counter() - started
        SPAN_LATENCY.observe(elapsed, name=name)
        log(
            "span",
            logging.DEBUG,
            span=name,
            outcome=outcome,
            duration_ms=round(elapsed * 1000, 2),
            **fields,
        )
