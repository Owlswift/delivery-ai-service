CANCELLED = "This delivery request was cancelled. Nothing further will happen on it."

USER_CANCELLED = "Okay, I've cancelled this delivery request. Nothing was booked."

PENDING_NOT_CANCELLABLE = (
    "I'm still confirming your request with the delivery provider, so I can't cancel "
    "it from here yet. I'll update you as soon as it's confirmed."
)


def not_cancellable(reference: str | None) -> str:
    ref = f" ({reference})" if reference else ""
    return (
        f"Your delivery request{ref} has already been created, and cancelling it "
        "isn't supported here yet. It is still active."
    )


def provider_cancelled(reason: str | None) -> str:
    why = f" Reason: {reason}." if reason else ""
    return f"The delivery provider cancelled your request.{why} No courier will be assigned."


def request_expired(reason: str | None) -> str:
    why = f" ({reason})" if reason else ""
    return (
        f"Your delivery request closed without a courier being assigned{why}. "
        "Reply 'try again' and I'll re-check the fare and ask you to confirm."
    )


def failed(reason: str | None) -> str:
    why = f" ({reason})" if reason else ""
    return (
        f"Your last request closed without a courier{why}. "
        "Reply 'try again' to start a fresh request with the same details."
    )


CHECKING = "One moment, I'm checking that with the delivery provider."
