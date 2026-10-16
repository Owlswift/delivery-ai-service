import json
import logging
import sys
from datetime import UTC, datetime
from typing import IO

from app.observability.context import current
from app.observability.redaction import mask, redact

LOGGER_NAME = "app"
_HANDLER_TAG = "_app_observability_handler"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": mask(record.getMessage()),
            **current(),
            **redact(getattr(record, "fields", {})),
        }
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            payload["error"] = mask(f"{type(exc).__name__}: {exc}")  # no stack: it can carry data
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(
    level: str = "INFO", as_json: bool = True, stream: IO[str] | None = None
) -> None:
    """Install one handler on the `app` logger. Safe to call repeatedly."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    setattr(handler, _HANDLER_TAG, True)
    handler.setFormatter(
        JsonFormatter() if as_json else logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False


def log(event: str, level: int = logging.INFO, **fields: object) -> None:
    """Emit a structured event. Field values are redacted before they are written."""
    logging.getLogger(LOGGER_NAME).log(level, event, extra={"fields": fields})
