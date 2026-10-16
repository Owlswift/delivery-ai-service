from app.providers.models import BookingPayload, ProviderBooking
from app.providers.protocols import BookingProviderError, BookingRejected, BookingTimeout


class FakeBookingProvider:
    """Idempotent in-memory provider. `script` holds one behavior per create call
    (default "ok"): ok, reject, timeout_before (nothing created), timeout_after
    (created, response lost), crash_after (created, then RuntimeError)."""

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        self.bookings: dict[str, ProviderBooking] = {}
        self.payloads: dict[str, BookingPayload] = {}
        self.create_calls: list[str] = []
        self.find_fails = False
        self.status = "awaiting_offers"

    def create_booking(self, payload: BookingPayload, idempotency_key: str) -> ProviderBooking:
        self.create_calls.append(idempotency_key)
        behavior = self.script.pop(0) if self.script else "ok"
        if behavior == "reject":
            raise BookingRejected("Pickup area is not serviceable")
        if behavior == "timeout_before":
            raise BookingTimeout(idempotency_key)
        existing = self.bookings.get(idempotency_key)
        if existing is None:
            existing = ProviderBooking(
                reference=f"BK-{len(self.bookings) + 1:05d}", status=self.status
            )
            self.bookings[idempotency_key] = existing
            self.payloads[idempotency_key] = payload
        if behavior == "timeout_after":
            raise BookingTimeout(idempotency_key)
        if behavior == "crash_after":
            raise RuntimeError("process died")
        return existing

    def find_booking(self, idempotency_key: str) -> ProviderBooking | None:
        if self.find_fails:
            raise BookingProviderError("lookup failed")
        return self.bookings.get(idempotency_key)
