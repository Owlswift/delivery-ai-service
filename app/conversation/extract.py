from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from app.booking.models import USER_DETAIL_FIELDS

if TYPE_CHECKING:
    from app.schemas import Session

USER_PHONE_MARKERS = re.compile(
    r"\b(my number|my phone|sender(?:'s)? phone|call me on)\b",
    re.IGNORECASE,
)

PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{6,16}\d")

FROM_TO = re.compile(
    r"(?:pick(?:\s*up)?(?:\s+(?:from|at))?|from)\s+"
    r"(?P<pickup>.+?)\s+"
    r"(?:and\s+)?(?:deliver(?:\s+to)?|drop[- ]?off(?:\s+(?:at|to))?|destination|to)\s+"
    r"(?P<dest>.+?)(?="
    r"\.\s|sending|package|it'?s\s+a|recipient|budget|maximum|phone|$)",
    re.IGNORECASE,
)

PACKAGE_RE = re.compile(
    r"(?:sending|package(?:\s+is)?|it'?s|its)\s+(?:a |an )?"
    r"(?P<pkg>.+?)(?=\.\s|recipient|budget|maximum|phone|$)",
    re.IGNORECASE,
)

RECIPIENT_RE = re.compile(
    r"recipient(?:\s+is|\s+name)?\s+(?P<name>[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z'\-]+)*)",
    re.IGNORECASE,
)

BUDGET_RE = re.compile(
    r"(?:maximum\s+budget|max(?:imum)?\s+budget|budget|willing to pay|pay up to)"
    r"[^\d]{0,24}(?P<amount>\d[\d,]*(?:\.\d{1,2})?)\s*(?P<currency>[A-Za-z]{3,}|naira|dollars?)?",
    re.IGNORECASE,
)

LABELED = {
    "pickup_address": re.compile(
        r"(?:pickup(?:\s+address)?|collect from|pick up from)\s*[:\-]\s*(?P<val>[^\n.]+)",
        re.IGNORECASE,
    ),
    "destination_address": re.compile(
        r"(?:destination(?:\s+address)?|drop[- ]?off|deliver to)\s*[:\-]\s*(?P<val>[^\n.]+)",
        re.IGNORECASE,
    ),
    "package_description": re.compile(
        r"(?:package|item)\s*[:\-]\s*(?P<val>[^\n.]+)",
        re.IGNORECASE,
    ),
    "recipient_name": re.compile(
        r"(?:recipient(?:\s+name)?|name)\s*[:\-]\s*(?P<val>[^\n.]+)",
        re.IGNORECASE,
    ),
    "recipient_phone": re.compile(
        r"(?:recipient(?:'s)?\s+(?:phone|number)|(?<!sender )phone(?:\s+number)?)\s*[:\-]\s*(?P<val>[^\n]+)",
        re.IGNORECASE,
    ),
    "sender_max_budget": re.compile(
        r"(?:budget|max(?:imum)?)\s*[:\-]\s*(?P<val>[^\n.]+)",
        re.IGNORECASE,
    ),
}

CORRECTION = re.compile(
    r"\b(actually|instead|change|update|correction|no[, ]+|wait)\b",
    re.IGNORECASE,
)

CORRECTION_FIELD = [
    (re.compile(r"\bpickup\b", re.I), "pickup_address"),
    (re.compile(r"\b(destination|drop[- ]?off|deliver)\b", re.I), "destination_address"),
    (re.compile(r"\bpackage\b", re.I), "package_description"),
    (re.compile(r"\b(recipient(?:'s)? name|name)\b", re.I), "recipient_name"),
    (re.compile(r"\b(recipient(?:'s)? phone|phone)\b", re.I), "recipient_phone"),
    (re.compile(r"\b(budget|pay)\b", re.I), "sender_max_budget"),
]


@dataclass
class Extraction:
    pickup_address: str | None = None
    destination_address: str | None = None
    package_description: str | None = None
    recipient_name: str | None = None
    recipient_phone: str | None = None
    sender_max_budget: str | None = None
    ambiguous: dict[str, str] = field(default_factory=dict)

    def any_user_slot(self) -> bool:
        return any(getattr(self, name) for name in USER_DETAIL_FIELDS)


class Extractor(Protocol):
    def extract(self, text: str, session: Session) -> Extraction: ...


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value).strip(" \t\n\r,.;:")
    return text or None


class RuleBasedExtractor:
    """Deterministic extractor. Only returns slots evidenced in the utterance."""

    def extract(self, text: str, session: Session) -> Extraction:
        extraction = Extraction()
        self._labeled(text, extraction)
        self._narrative(text, extraction)
        self._phones_and_budget(text, extraction)
        self._last_question_fallback(text, session, extraction)
        return extraction

    def _labeled(self, text: str, extraction: Extraction) -> None:
        for name, pattern in LABELED.items():
            match = pattern.search(text)
            if match:
                setattr(extraction, name, _clean(match.group("val")))

    def _narrative(self, text: str, extraction: Extraction) -> None:
        from_to = FROM_TO.search(text)
        if from_to:
            if not extraction.pickup_address:
                extraction.pickup_address = _clean(from_to.group("pickup"))
            if not extraction.destination_address:
                extraction.destination_address = _clean(from_to.group("dest"))
        package = PACKAGE_RE.search(text)
        if package and not extraction.package_description:
            extraction.package_description = _clean(package.group("pkg"))
        recipient = RECIPIENT_RE.search(text)
        if recipient and not extraction.recipient_name:
            extraction.recipient_name = _clean(recipient.group("name"))

    def _phones_and_budget(self, text: str, extraction: Extraction) -> None:
        if not extraction.sender_max_budget:
            budget = BUDGET_RE.search(text)
            if budget:
                amount = budget.group("amount")
                currency = budget.group("currency") or ""
                extraction.sender_max_budget = _clean(f"{amount} {currency}")
        if extraction.recipient_phone:
            return
        if USER_PHONE_MARKERS.search(text) and "recipient" not in text.lower():
            return
        phones = PHONE_RE.findall(text)
        if phones:
            extraction.recipient_phone = _clean(phones[0])

    def _last_question_fallback(self, text: str, session: Session, extraction: Extraction) -> None:
        actions = session.booking.next_actions
        if not actions or actions[0].field is None:
            return
        target = actions[0].field
        if target not in USER_DETAIL_FIELDS:
            return
        if getattr(extraction, target):
            return
        if extraction.any_user_slot() and not CORRECTION.search(text):
            return
        if CORRECTION.search(text):
            for pattern, name in CORRECTION_FIELD:
                if pattern.search(text) and name != target:
                    return
        setattr(extraction, target, _clean(text))
