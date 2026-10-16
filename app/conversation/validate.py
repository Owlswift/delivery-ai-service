import re

from app.booking.models import USER_DETAIL_FIELDS, CollectedField, DeliveryDetails, FieldStatus
from app.booking.money import parse_budget

VAGUE_ADDRESSES = {
    "mall",
    "the mall",
    "airport",
    "the airport",
    "downtown",
    "city center",
    "city centre",
    "home",
    "my house",
    "house",
    "there",
    "here",
    "same place",
    "nearby",
}

STREET_HINT = re.compile(
    r"\d|"
    r"\b(street|st\.?|road|rd\.?|avenue|ave\.?|close|drive|dr\.?|"
    r"way|boulevard|blvd\.?|lane|ln\.?|crescent|highway|expressway)\b",
    re.IGNORECASE,
)

PHONE_CHARS = re.compile(r"[\s\-().]")
PHONE_VALID = re.compile(r"^\+?\d{8,15}$")
HAS_LETTER = re.compile(r"[A-Za-z]")


def digits_only_phone(value: str) -> str:
    compact = PHONE_CHARS.sub("", value.strip())
    return compact


def normalize_phone(value: str) -> str | None:
    compact = digits_only_phone(value)
    if not PHONE_VALID.match(compact):
        return None
    return compact


def _set(field: CollectedField, status: FieldStatus, issue: str | None = None) -> None:
    field.status = status
    field.issue = issue
    if status == FieldStatus.missing:
        field.value = None


def validate_address(field: CollectedField) -> None:
    if not field.value or not field.value.strip():
        _set(field, FieldStatus.missing)
        return
    text = re.sub(r"\s+", " ", field.value.strip())
    field.value = text
    lowered = text.lower().rstrip(".")
    if lowered in VAGUE_ADDRESSES or len(text) < 8 or " " not in text:
        _set(
            field,
            FieldStatus.ambiguous,
            "Address is too vague; street and area/city are required",
        )
        return
    has_street = bool(STREET_HINT.search(text))
    has_locality = "," in text or len(text.split()) >= 4
    if not has_street or not has_locality:
        _set(
            field,
            FieldStatus.ambiguous,
            "Address needs a street-level part and an area or city",
        )
        return
    _set(field, FieldStatus.valid)


def validate_package(field: CollectedField) -> None:
    if not field.value or len(field.value.strip()) < 3:
        _set(
            field,
            FieldStatus.missing if not field.value else FieldStatus.invalid,
            "Package description is too short",
        )
        if not field.value:
            field.issue = None
        return
    field.value = field.value.strip()
    _set(field, FieldStatus.valid)


def validate_name(field: CollectedField) -> None:
    if not field.value or not field.value.strip():
        _set(field, FieldStatus.missing)
        return
    text = field.value.strip()
    if not HAS_LETTER.search(text) or len(HAS_LETTER.findall(text)) < 2:
        _set(field, FieldStatus.invalid, "Name must contain letters")
        return
    field.value = text
    _set(field, FieldStatus.valid)


def validate_phone(field: CollectedField) -> None:
    if not field.value or not field.value.strip():
        _set(field, FieldStatus.missing)
        return
    normalized = normalize_phone(field.value)
    if normalized is None:
        _set(field, FieldStatus.invalid, "Phone number must contain 8 to 15 digits")
        return
    field.value = normalized
    _set(field, FieldStatus.valid)


def validate_budget(details: DeliveryDetails) -> None:
    field = details.sender_max_budget
    if not field.value or not field.value.strip():
        _set(field, FieldStatus.missing)
        details.budget_amount = None
        details.budget_currency = None
        return
    parsed = parse_budget(field.value)
    if parsed is None:
        _set(field, FieldStatus.invalid, "Budget must be a positive amount")
        details.budget_amount = None
        details.budget_currency = None
        return
    amount, currency = parsed
    details.budget_amount = format(amount, "f")
    details.budget_currency = currency
    field.value = f"{amount} {currency}".strip() if currency else str(amount)
    _set(field, FieldStatus.valid)


def validate_details(details: DeliveryDetails) -> None:
    validate_address(details.pickup_address)
    validate_address(details.destination_address)
    validate_package(details.package_description)
    validate_name(details.recipient_name)
    validate_phone(details.recipient_phone)
    validate_phone(details.sender_phone)
    validate_budget(details)


def gap_fields(details: DeliveryDetails) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    ambiguous: list[str] = []
    for name in USER_DETAIL_FIELDS:
        field: CollectedField = getattr(details, name)
        if field.status == FieldStatus.missing:
            missing.append(name)
        elif field.status in (FieldStatus.ambiguous, FieldStatus.invalid):
            ambiguous.append(name)
    return missing, ambiguous
