from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.booking.models import BookingState


class Channel(StrEnum):
    text = "text"
    voice_transcript = "voice_transcript"


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"
    system = "system"


class SessionStatus(StrEnum):
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class Message(BaseModel):
    id: UUID
    role: MessageRole
    content: str
    channel: Channel
    created_at: datetime


class Session(BaseModel):
    id: UUID
    company_id: str
    status: SessionStatus
    locale: str
    channel: Channel
    external_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    messages: list[Message] = Field(default_factory=list)
    booking: BookingState = Field(default_factory=BookingState)
    version: int = 0
    created_at: datetime
    updated_at: datetime


class CreateSessionRequest(BaseModel):
    sender_phone: str = Field(min_length=8)
    channel: Channel = Channel.text
    locale: str = "en-US"
    external_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)
    channel: Channel | None = None
    client_message_id: UUID | None = None


class MessageTurnResponse(BaseModel):
    message: Message
    session: Session


class HealthResponse(BaseModel):
    status: str = "ok"
