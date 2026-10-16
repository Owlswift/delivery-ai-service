"""Provider-event envelope and results. Data module."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProviderEvent(BaseModel):
    event_id: str = Field(min_length=1)
    booking_reference: str = Field(min_length=1)
    type: Literal[
        "offer.created",
        "offer.updated",
        "offer.withdrawn",
        "offer.expired",
        "booking.cancelled",
        "booking.expired",
    ]
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class EventResponse(BaseModel):
    result: str
    notified: bool
    original_result: str | None = None


class EventError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class EventAck:
    outcome: str  # stored | updated | duplicate | stale | ignored | cancelled | failed
    message: str | None = None
    original: str | None = None  # for duplicates: the first outcome

    @property
    def notified(self) -> bool:
        return self.message is not None
