from app.booking.models import BookingRequest

STATUS_TEXT = {"awaiting_offers": "awaiting courier offers"}

UNCONFIRMED = (
    "I sent your delivery request but haven't had confirmation from the delivery "
    "provider yet, so I can't say it's booked. Send any message and I'll check "
    "again. Checking again will not create a duplicate."
)


def status_text(provider_status: str) -> str:
    return STATUS_TEXT.get(provider_status, provider_status.replace("_", " "))


def created(request: BookingRequest) -> str:
    return (
        f"Your delivery request has been created (reference {request.reference}). "
        f"Status: {status_text(request.provider_status or 'unknown')}."
    )


def already_created(request: BookingRequest) -> str:
    return f"{created(request)} Changing or cancelling it isn't supported here yet."


def rejected(reason: str) -> str:
    return (
        f"The delivery provider declined the request: {reason}. "
        "No booking was created. Tell me what to change, or reply yes to try again."
    )


def fare_changed(old: str, new: str) -> str:
    return (
        f"The fare changed from {old} to {new} before I could book. "
        "No booking was created. Please confirm the new fare to continue."
    )
