"""Budget parsing shared by detail validation and offer budget negotiation."""

import re
from decimal import Decimal, InvalidOperation

CURRENCY = re.compile(r"\b(NGN|USD|GBP|EUR|GHS|KES|ZAR|naira|dollars?|pounds?)\b", re.I)


AMOUNT = re.compile(r"(\d+(?:[.,]\d{1,2})?)")


def parse_budget(value: str) -> tuple[Decimal, str | None] | None:
    currency_match = CURRENCY.search(value)
    currency = None
    if currency_match:
        raw = currency_match.group(1).upper()
        currency = {
            "NAIRA": "NGN",
            "DOLLAR": "USD",
            "DOLLARS": "USD",
            "POUND": "GBP",
            "POUNDS": "GBP",
        }.get(raw, raw)
    amount_match = AMOUNT.search(value.replace(",", ""))
    if not amount_match:
        return None
    try:
        amount = Decimal(amount_match.group(1))
    except InvalidOperation:
        return None
    if amount <= 0:
        return None
    return amount, currency
