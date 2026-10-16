"""Frozen wire schemas for provider integration v1.

`docs/contract/v1/schemas.json` is generated from this module by
`python -m scripts.export_contract`; a test fails when they differ. Changing the
contract therefore means regenerating that file in the same, reviewed change.
"""

from pydantic import BaseModel

from app.errors import ErrorResponse
from app.events.models import EventResponse, ProviderEvent
from app.offers.models import Courier, OfferEventPayload
from app.providers.models import (
    AddressResolution,
    AssignmentInfo,
    BookingPayload,
    Confirmation,
    FareQuote,
    ProviderBooking,
    ResolvedAddress,
)
from app.providers.operations.models import (
    ARGS_BY_TYPE,
    RESULT_BY_TYPE,
    BookingStatusResult,
    LeaseRequest,
    OperationAck,
    OperationEnvelope,
    OperationError,
    OperationResultRequest,
)

VERSION = "1"

WIRE_MODELS: dict[str, type[BaseModel]] = {
    model.__name__: model
    for model in (
        # operation envelope and result reporting
        OperationEnvelope,
        LeaseRequest,
        OperationResultRequest,
        OperationAck,
        OperationError,
        # shared value objects
        ResolvedAddress,
        Courier,
        Confirmation,
        BookingPayload,
        # arguments and results per operation
        *ARGS_BY_TYPE.values(),
        AddressResolution,
        FareQuote,
        ProviderBooking,
        AssignmentInfo,
        BookingStatusResult,
        # events
        ProviderEvent,
        OfferEventPayload,
        EventResponse,
        ErrorResponse,
    )
}

AUTH = {
    "scheme": "HMAC-SHA256 request signature",
    "headers": ["X-Provider-Id", "X-Timestamp", "X-Signature", "X-Correlation-Id (optional)"],
    "signed_message": "timestamp \\n METHOD \\n path+query \\n sha256(body) hex",
    "timestamp": "unix seconds; rejected outside the configured window",
}

EVENT_TYPES = [
    "offer.created",
    "offer.updated",
    "offer.withdrawn",
    "offer.expired",
    "booking.cancelled",
    "booking.expired",
]

OUTCOMES = {
    "succeeded": "result required and schema-validated",
    "rejected": "definite refusal or validation failure; never retried",
    "unavailable": "not processed; retried with backoff",
    "unknown": "may have been processed; retried with the same idempotency key (reconcile=true)",
}


def build_contract() -> dict:
    return {
        "version": VERSION,
        "auth": AUTH,
        "event_types": EVENT_TYPES,
        "outcomes": OUTCOMES,
        "operations": {
            op_type.value: {
                "args": ARGS_BY_TYPE[op_type].__name__,
                "result": RESULT_BY_TYPE[op_type].__name__,
                "executor": "provider_server",
                "idempotency_key": op_type.value in ("create_booking", "assign_offer"),
            }
            for op_type in ARGS_BY_TYPE
        },
        "schemas": {name: m.model_json_schema() for name, m in sorted(WIRE_MODELS.items())},
    }
