from app.providers.models import FareQuote, ResolvedAddress

PROVIDER_UNAVAILABLE = (
    "I couldn't check that with the delivery provider just now. "
    "Please send any message and I'll try again."
)

PROCEED_QUESTION = (
    "Please confirm these details to book the delivery. "
    "Reply yes to book, or tell me what to change."
)

READY = "Great — you're confirmed. I'll go ahead and book this delivery."

DECLINED = "No problem. What would you like to change?"

REASK = "Please reply yes to proceed, or tell me what you'd like to change."


def address_not_found(which: str) -> str:
    return (
        f"I couldn't find that {which} address. "
        "Could you send a more complete or corrected address?"
    )


def address_choices(which: str, candidates: list[ResolvedAddress]) -> str:
    lines = [f"{i}. {c.formatted_address}" for i, c in enumerate(candidates, 1)]
    return (
        f"I found more than one match for the {which} address. Which one is it? "
        "Reply with the number.\n" + "\n".join(lines)
    )


def budget_too_low(budget: str, quote: FareQuote) -> str:
    return (
        f"The minimum fare for this route is {quote.base_fare} {quote.currency}, "
        f"which is above your budget of {budget}. "
        "What maximum budget would you like to set?"
    )


def budget_currency_mismatch(quote: FareQuote) -> str:
    return (
        f"This route is priced in {quote.currency}. "
        f"Please give your maximum budget in {quote.currency}."
    )


def route_line(quote: FareQuote) -> str:
    if quote.route is None:
        return ""
    return f"Route: {quote.route.distance_km:g} km, about {quote.route.duration_min} min\n"


def present_quote(
    pickup: ResolvedAddress,
    destination: ResolvedAddress,
    quote: FareQuote,
    budget: str,
    package: str,
    recipient: str,
) -> str:
    expires = quote.expires_at.strftime("%H:%M UTC")
    return (
        f"Pickup: {pickup.formatted_address}\n"
        f"Destination: {destination.formatted_address}\n"
        f"Package: {package}\n"
        f"Recipient: {recipient}\n"
        f"{route_line(quote)}"
        f"Base fare: {quote.base_fare} {quote.currency} (quote valid until {expires})\n"
        f"Your maximum budget of {budget} covers this fare.\n"
        f"{PROCEED_QUESTION}"
    )


def fare_changed(old: str, quote: FareQuote) -> str:
    return (
        f"The fare has changed from {old} to {quote.base_fare} {quote.currency}. {PROCEED_QUESTION}"
    )
