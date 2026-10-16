from datetime import UTC, datetime
from uuid import uuid4

from app.conversation.prompts import GREETING
from app.schemas import (
    Channel,
    Message,
    MessageRole,
    Session,
    SessionStatus,
)


def _now() -> datetime:
    return datetime.now(UTC)


def greeting_message(channel: Channel) -> Message:
    return assistant_message(GREETING, channel)


def assistant_message(content: str, channel: Channel) -> Message:
    return Message(
        id=uuid4(),
        role=MessageRole.assistant,
        content=content,
        channel=channel,
        created_at=_now(),
    )


def assert_accepts_messages(session: Session) -> None:
    from app.errors import AppError

    if session.status != SessionStatus.active:
        raise AppError(
            409,
            "conflict",
            "Session is not accepting messages",
            {"status": session.status},
        )
