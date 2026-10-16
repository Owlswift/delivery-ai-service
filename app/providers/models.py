"""Contract value objects exchanged with logistics providers. Data module."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ResolutionStatus(StrEnum):
    resolved = "resolved"
    ambiguous = "ambiguous"
    not_found = "not_found"


class CoordinatesSource(StrEnum):
    provider = "provider"
    geocoded = "geocoded"


class ResolvedAddress(BaseModel):
    place_id: str
    formatted_address: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    coordinates_source: CoordinatesSource | None = None
    precision: str | None = None  # geocoder location_type, when geocoded

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class AddressResolution(BaseModel):
    input: str
    status: ResolutionStatus
    resolved: ResolvedAddress | None = None
    candidates: list[ResolvedAddress] = Field(default_factory=list)


class Route(BaseModel):
    distance_km: float
    duration_min: int


class FareQuote(BaseModel):
    quote_id: str
    base_fare: str
    currency: str
    expires_at: datetime
    route: Route | None = None
    pickup_place_id: str
    destination_place_id: str


class MaxBudget(BaseModel):
    amount: str
    currency: str | None = None


class Confirmation(BaseModel):
    """The user's explicit confirmation, as recorded by the workflow."""

    confirmed_at: datetime
    quote_id: str
    fingerprint: str


class BookingPayload(BaseModel):
    pickup: ResolvedAddress
    destination: ResolvedAddress
    package_description: str
    recipient_name: str
    recipient_phone: str
    sender_phone: str
    quote_id: str
    base_fare: str
    currency: str
    max_budget: MaxBudget
    confirmation: Confirmation


class ProviderBooking(BaseModel):
    reference: str = Field(min_length=1)
    status: str


class AssignmentInfo(BaseModel):
    offer_id: str
    courier_id: str
    status: str = "assigned"
