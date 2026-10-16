from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

from app.providers.fake_geocoder import coordinates_for
from app.providers.models import (
    AddressResolution,
    FareQuote,
    ResolutionStatus,
    ResolvedAddress,
    Route,
)
from app.providers.protocols import ProviderError, ProviderTimeout


def _place(text: str, suffix: str = "", coordinates: bool = True) -> ResolvedAddress:
    slug = "".join(c for c in text.lower() if c.isalnum())[:40]
    lat, lng = coordinates_for(text + suffix) if coordinates else (None, None)
    return ResolvedAddress(
        place_id=f"fake:{slug}{suffix}",
        formatted_address=f"{text}{suffix and ' ' + suffix}",
        latitude=lat,
        longitude=lng,
        coordinates_source="provider" if coordinates else None,
    )


class FakeLogisticsProvider:
    """Deterministic stand-in. Address text markers drive the outcome:

    "timeout" -> ProviderTimeout, "unresolvable" -> not_found,
    "ambiguous" -> two candidates, anything else resolves to itself.
    Tests may change `fare`, `currency`, `ttl` and `now` between calls.
    """

    def __init__(
        self,
        fare: str = "5000",
        currency: str = "NGN",
        ttl: timedelta = timedelta(minutes=10),
        now=lambda: datetime.now(UTC),
    ) -> None:
        self.fare = fare
        self.currency = currency
        self.ttl = ttl
        self.now = now
        self.fail_estimate = False
        self.coordinates = True  # set False to return locations without coordinates
        self.resolve_calls: list[str] = []
        self.estimate_calls = 0
        self._ids = count(1)

    def resolve_address(self, text: str) -> AddressResolution:
        self.resolve_calls.append(text)
        lowered = text.lower()
        if "timeout" in lowered:
            raise ProviderTimeout(text)
        if "unresolvable" in lowered:
            return AddressResolution(input=text, status=ResolutionStatus.not_found)
        if "ambiguous" in lowered:
            return AddressResolution(
                input=text,
                status=ResolutionStatus.ambiguous,
                candidates=[
                    _place(text, "(North)", self.coordinates),
                    _place(text, "(South)", self.coordinates),
                ],
            )
        return AddressResolution(
            input=text,
            status=ResolutionStatus.resolved,
            resolved=_place(text, "", self.coordinates),
        )

    def estimate_fare(self, pickup: ResolvedAddress, destination: ResolvedAddress) -> FareQuote:
        self.estimate_calls += 1
        if self.fail_estimate:
            raise ProviderError("estimate failed")
        return FareQuote(
            quote_id=f"q{next(self._ids)}",
            base_fare=format(Decimal(self.fare), "f"),
            currency=self.currency,
            expires_at=self.now() + self.ttl,
            route=Route(distance_km=12.5, duration_min=35),
            pickup_place_id=pickup.place_id,
            destination_place_id=destination.place_id,
        )
