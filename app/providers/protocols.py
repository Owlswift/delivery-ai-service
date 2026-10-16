"""The provider interfaces the workflow depends on, and their errors.

Three ports, one per concern: address/fare (`LogisticsProvider`), booking creation
(`BookingProvider`) and courier assignment (`OfferProvider`). Implementations are the
deferred provider (operations for the provider's executor) and the in-process fakes.
"""

from typing import Protocol

from app.providers.models import (
    AddressResolution,
    AssignmentInfo,
    BookingPayload,
    FareQuote,
    ProviderBooking,
    ResolvedAddress,
)


class ProviderError(Exception):
    """The provider failed to answer."""


class ProviderTimeout(ProviderError):
    """The provider did not answer in time."""


class LogisticsProvider(Protocol):
    def resolve_address(self, text: str) -> AddressResolution: ...

    def estimate_fare(self, pickup: ResolvedAddress, destination: ResolvedAddress) -> FareQuote: ...


class BookingRejected(Exception):
    """Definite refusal: the provider did not create a booking."""


class BookingProviderError(Exception):
    """Outcome unknown: the provider failed to answer."""


class BookingTimeout(BookingProviderError):
    """Outcome unknown: the provider did not answer in time."""


class BookingProvider(Protocol):
    def create_booking(self, payload: BookingPayload, idempotency_key: str) -> ProviderBooking:
        """Must return the existing booking when the key was already used."""

    def find_booking(self, idempotency_key: str) -> ProviderBooking | None: ...


class OfferUnavailable(Exception):
    """Definite refusal: the offer cannot be assigned."""


class OfferProviderError(Exception):
    """Outcome unknown: the provider failed to answer."""


class OfferTimeout(OfferProviderError):
    """Outcome unknown: the provider did not answer in time."""


class OfferProvider(Protocol):
    def assign_offer(
        self, booking_reference: str, offer_id: str, idempotency_key: str
    ) -> AssignmentInfo:
        """Must return the existing assignment when the key was already used."""

    def find_assignment(self, booking_reference: str) -> AssignmentInfo | None: ...
