import httpx

from app.providers.geocoder import GeocoderError, GeocodeResult, GeocodeStatus

URL = "https://maps.googleapis.com/maps/api/geocode/json"


class GoogleMapsGeocoder:
    """Google Geocoding API adapter. Accepts a result only when it is unambiguous."""

    def __init__(
        self,
        api_key: str,
        timeout: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def geocode(self, address: str) -> GeocodeResult:
        try:
            response = self._client.get(URL, params={"address": address, "key": self._key})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # never include the request URL: it carries the API key
            raise GeocoderError(f"geocoding request failed ({type(exc).__name__})") from None
        status = body.get("status")
        if status == "ZERO_RESULTS":
            return GeocodeResult(status=GeocodeStatus.not_found)
        if status != "OK":
            raise GeocoderError(f"geocoding unavailable ({status})")
        results = body.get("results") or []
        if len(results) != 1 or results[0].get("partial_match"):
            return GeocodeResult(status=GeocodeStatus.ambiguous)
        geometry = results[0].get("geometry", {})
        location = geometry.get("location", {})
        try:
            return GeocodeResult(
                status=GeocodeStatus.ok,
                latitude=float(location["lat"]),
                longitude=float(location["lng"]),
                precision=geometry.get("location_type"),
            )
        except (KeyError, TypeError, ValueError):
            raise GeocoderError("geocoding response was malformed") from None
