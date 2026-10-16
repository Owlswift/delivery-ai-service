from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel


class GeocodeStatus(StrEnum):
    ok = "ok"
    ambiguous = "ambiguous"  # several candidates or only a partial match
    not_found = "not_found"


class GeocodeResult(BaseModel):
    status: GeocodeStatus
    latitude: float | None = None
    longitude: float | None = None
    precision: str | None = None


class GeocoderError(Exception):
    """The geocoder could not answer (quota, auth, network, timeout, not configured)."""


class Geocoder(Protocol):
    def geocode(self, address: str) -> GeocodeResult: ...


class UnconfiguredGeocoder:
    """Used when no geocoding key is configured; never invents coordinates."""

    def geocode(self, address: str) -> GeocodeResult:
        raise GeocoderError("geocoder is not configured")
