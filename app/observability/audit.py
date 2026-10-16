from app.observability.logs import log
from app.observability.metrics import AUDIT_EVENTS


def audit(event: str, **fields: object) -> None:
    """Record a workflow- or security-relevant event. Pass identifiers and outcomes,
    never payloads: values go through redaction, but the contract is "no personal data"."""
    AUDIT_EVENTS.inc(event=event)
    log(event, audit=True, **fields)
