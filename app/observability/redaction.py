import re
from typing import Any

REDACTED = "[REDACTED]"
MAX_LENGTH = 300

# A key containing any of these is never logged, whatever its value.
SENSITIVE_KEY_PARTS = (
    "phone",
    "address",
    "recipient",
    "secret",
    "signature",
    "token",
    "authorization",
    "password",
    "api_key",
    "api-key",
    "apikey",
    "credential",
    "text",
    "content",
    "body",
    "courier",
    "name",
)
# Identifiers that are safe and useful for tracing, even though they contain "key"/"name".
SAFE_KEYS = frozenset(
    {
        "idempotency_key",
        "correlation_id",
        "operation_id",
        "event_id",
        "session_id",
        "provider_id",
        "event",
        "logger",
        "level",
        "ts",
        "route",
        "method",
        "type",
        "trigger",
    }
)

PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
CREDENTIAL = re.compile(r"(?i)\b(bearer|basic|token|key|secret)[\s:=]+[A-Za-z0-9._~+/=-]{8,}")


def sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered not in SAFE_KEYS and any(part in lowered for part in SENSITIVE_KEY_PARTS)


def mask(text: str) -> str:
    text = CREDENTIAL.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = PHONE.sub("[phone]", text)
    return text if len(text) <= MAX_LENGTH else text[:MAX_LENGTH] + "…"


def redact(value: Any, key: str | None = None) -> Any:
    """A JSON-safe copy of `value` with sensitive keys removed and free text masked."""
    if key is not None and sensitive(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return mask(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return mask(str(value))
