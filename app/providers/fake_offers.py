from app.providers.models import AssignmentInfo
from app.providers.protocols import OfferProviderError, OfferTimeout, OfferUnavailable


class FakeOfferProvider:
    """Idempotent in-memory provider. `script` holds one behavior per assign call
    (default "ok"): ok, reject, timeout_before (nothing assigned),
    timeout_after (assigned, response lost). `courier_ids` maps offer -> courier."""

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        self.assignments: dict[str, AssignmentInfo] = {}  # by booking reference
        self.by_key: dict[str, AssignmentInfo] = {}
        self.assign_calls: list[tuple[str, str]] = []  # (offer_id, key)
        self.find_fails = False
        self.courier_ids: dict[str, str] = {}

    def assign_offer(
        self, booking_reference: str, offer_id: str, idempotency_key: str
    ) -> AssignmentInfo:
        self.assign_calls.append((offer_id, idempotency_key))
        behavior = self.script.pop(0) if self.script else "ok"
        if behavior == "reject":
            raise OfferUnavailable(offer_id)
        if behavior == "timeout_before":
            raise OfferTimeout(offer_id)
        existing = self.by_key.get(idempotency_key)
        if existing is None:
            existing = self.assignments.get(booking_reference)
            if existing is None:
                existing = AssignmentInfo(
                    offer_id=offer_id,
                    courier_id=self.courier_ids.get(offer_id, f"courier-of-{offer_id}"),
                )
                self.assignments[booking_reference] = existing
            self.by_key[idempotency_key] = existing
        if behavior == "timeout_after":
            raise OfferTimeout(offer_id)
        return existing

    def find_assignment(self, booking_reference: str) -> AssignmentInfo | None:
        if self.find_fails:
            raise OfferProviderError("lookup failed")
        return self.assignments.get(booking_reference)
