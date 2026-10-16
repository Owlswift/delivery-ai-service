from app.booking.models import FieldStatus

GREETING = (
    "Hi — I can help you book a delivery. Please share the pickup address, "
    "destination address, what the package is, the recipient's name and phone "
    "number, and the most you are willing to pay."
)

COMPLETE = "I have all the delivery details I need. Next I can estimate the fare."

FIELD_PROMPTS: dict[str, str] = {
    "pickup_address": "What is the pickup address, including street and area/city?",
    "destination_address": ("What is the destination address, including street and area/city?"),
    "package_description": "What is in the package?",
    "recipient_name": "What is the recipient's name?",
    "recipient_phone": "What is the recipient's phone number?",
    "sender_max_budget": "What is the most you are willing to pay for this delivery?",
}

CLARIFY_PROMPTS: dict[str, str] = {
    "pickup_address": (
        "I need a more specific pickup address — street and area/city, not just a landmark."
    ),
    "destination_address": (
        "I need a more specific destination address — street and area/city, not just a landmark."
    ),
    "package_description": "Could you describe the package a bit more clearly?",
    "recipient_name": "What is the recipient's full name?",
    "recipient_phone": "That phone number does not look valid. What is the recipient's phone number?",
    "sender_max_budget": "What is your maximum budget, as a positive amount (and currency if you can)?",
}


def prompt_for_field(field: str, status: FieldStatus) -> str:
    if status in (FieldStatus.ambiguous, FieldStatus.invalid):
        return CLARIFY_PROMPTS.get(field, FIELD_PROMPTS[field])
    return FIELD_PROMPTS[field]
