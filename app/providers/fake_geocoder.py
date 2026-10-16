from app.providers.geocoder import GeocoderError, GeocodeResult, GeocodeStatus


def coordinates_for(text: str) -> tuple[float, float]:
    """Deterministic pseudo-coordinates, used by the fakes only."""
    h = sum(ord(c) * (i + 1) for i, c in enumerate(text.lower()))
    return 6.40 + (h % 1000) / 10000, 3.30 + (h % 997) / 10000


class FakeGeocoder:
    """Markers in the address drive the outcome: "geo-down" -> GeocoderError,
    "geo-notfound", "geo-ambiguous"; anything else geocodes deterministically."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def geocode(self, address: str) -> GeocodeResult:
        self.calls.append(address)
        lowered = address.lower()
        if "geo-down" in lowered:
            raise GeocoderError("down")
        if "geo-notfound" in lowered:
            return GeocodeResult(status=GeocodeStatus.not_found)
        if "geo-ambiguous" in lowered:
            return GeocodeResult(status=GeocodeStatus.ambiguous)
        lat, lng = coordinates_for(address)
        return GeocodeResult(
            status=GeocodeStatus.ok, latitude=lat, longitude=lng, precision="ROOFTOP"
        )
