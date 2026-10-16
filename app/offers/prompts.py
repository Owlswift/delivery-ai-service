from app.offers.models import Offer

REASK = (
    "Please reply yes to accept that courier, tell me a new maximum budget, "
    "or say no to keep waiting."
)

DECLINED = "Okay. I'll keep your request open and let you know when a courier is assigned."

CONFIRMING = (
    "I'm confirming the courier with the delivery provider and can't say a courier is "
    "assigned yet. I'll update you as soon as it's confirmed."
)

OFFER_GONE = "That offer is no longer available. I'll keep your request open."

ASSIGNMENT_FAILED = (
    "The provider couldn't assign the selected courier, and there is no other "
    "available offer yet. I'll keep your request open."
)


def waiting(reference: str | None) -> str:
    return (
        f"Your delivery request ({reference}) is still awaiting courier offers. "
        "I'll message you when a courier is assigned or I need your decision."
    )


def _courier(offer: Offer) -> str:
    name = offer.courier.name or "A courier"
    parts = []
    if offer.pickup_eta_min is not None:
        parts.append(f"about {offer.pickup_eta_min} min to pickup")
    if offer.pickup_distance_km is not None:
        parts.append(f"{offer.pickup_distance_km:g} km from the pickup")
    return name + (f" ({', '.join(parts)})" if parts else "")


def assigned(offer: Offer, reference: str | None) -> str:
    ref = f" for request {reference}" if reference else ""
    return (
        f"A courier has been assigned{ref}: {_courier(offer)}. "
        f"Price: {offer.price} {offer.currency}."
    )


def ask_above_budget(offer: Offer, budget: str, currency: str) -> str:
    return (
        f"No courier is available within your budget of {budget} {currency} yet. "
        f"The lowest offer is {offer.price} {offer.currency} from {_courier(offer)}. "
        "Would you like to accept it? You can also give me a higher maximum budget, "
        "or say no to keep waiting."
    )


def budget_updated(amount: str, currency: str) -> str:
    return (
        f"Got it, your maximum budget for this delivery is now {amount} {currency}. "
        "I'll keep watching for courier offers within it."
    )


def budget_currency(currency: str) -> str:
    return f"This delivery is priced in {currency}. Please give the budget in {currency}."
