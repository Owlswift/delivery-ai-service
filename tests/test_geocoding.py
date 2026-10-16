import httpx
import pytest
from pydantic import ValidationError

from app.booking.models import BookingStatus
from app.conversation.turn import handle_user_turn
from app.providers.fake_geocoder import FakeGeocoder
from app.providers.geocoder import GeocoderError, GeocodeResult, GeocodeStatus, UnconfiguredGeocoder
from app.providers.google_geocoder import GoogleMapsGeocoder
from app.providers.models import ResolvedAddress
from tests.test_booking_request import Flow
from tests.test_quoting import EX, blob


class Geo(Flow):
    def __init__(self, geocoder="default", script=None):
        super().__init__(script)
        self.geocoder = FakeGeocoder() if geocoder == "default" else geocoder
        self.provider.coordinates = False  # provider returns locations without coordinates

    def say(self, text: str) -> str:
        return handle_user_turn(
            self.session,
            text,
            EX,
            self.provider,
            self.clock,
            booking_provider=self.bp,
            geocoder=self.geocoder,
        )


def test_provider_coordinates_are_used_and_geocoder_not_called() -> None:
    f = Geo()
    f.provider.coordinates = True
    f.say(blob())
    pickup = f.b.quote.pickup.resolved
    assert pickup.coordinates_source == "provider" and pickup.has_coordinates
    assert f.geocoder.calls == []


def test_missing_coordinates_are_geocoded_from_formatted_address() -> None:
    f = Geo()
    f.say(blob())
    assert f.b.status == BookingStatus.awaiting_confirmation
    pickup, dest = f.b.quote.pickup.resolved, f.b.quote.destination.resolved
    assert pickup.coordinates_source == "geocoded" and pickup.precision == "ROOFTOP"
    assert pickup.has_coordinates and dest.has_coordinates
    assert f.geocoder.calls == [pickup.formatted_address, dest.formatted_address]


def test_coordinates_reach_the_booking_payload_and_result() -> None:
    f = Geo()
    f.say(blob())
    f.say("yes")
    assert f.b.status == BookingStatus.booking_created
    payload = f.bp.payloads[f.b.creation.request.idempotency_key]
    assert payload.pickup.latitude == f.b.quote.pickup.resolved.latitude
    assert payload.destination.longitude == f.b.quote.destination.resolved.longitude
    assert f.b.result.payload.pickup.coordinates_source == "geocoded"


@pytest.mark.parametrize("marker", ["geo-notfound", "geo-ambiguous"])
def test_unclear_geocode_asks_for_a_fuller_address_and_requests_no_fare(marker) -> None:
    f = Geo()
    reply = f.say(blob(pickup=f"8 {marker} Road, Ikeja"))
    assert f.b.status == BookingStatus.address_clarification
    assert "pickup" in reply and f.b.next_actions[0].field == "pickup_address"
    assert f.provider.estimate_calls == 0 and f.b.quote.quote is None
    f.say("3 Allen Avenue, Ikeja")  # corrected address is geocoded and proceeds
    assert f.b.status == BookingStatus.awaiting_confirmation


def test_geocoder_outage_is_retried_not_treated_as_not_found() -> None:
    f = Geo()
    reply = f.say(blob(pickup="8 geo-down Road, Ikeja"))
    assert "try again" in reply
    assert f.b.quote.pickup.resolved is not None  # provider result kept, no coordinates
    assert not f.b.quote.pickup.resolved.has_coordinates
    assert f.provider.estimate_calls == 0

    class Recovered:
        def geocode(self, address):
            return GeocodeResult(status=GeocodeStatus.ok, latitude=6.5, longitude=3.3)

    f.geocoder = Recovered()  # service recovers
    f.say("ok")
    assert f.b.status == BookingStatus.awaiting_confirmation


def test_no_geocoder_or_unconfigured_geocoder_never_invents_coordinates() -> None:
    for geocoder in (None, UnconfiguredGeocoder()):
        f = Geo(geocoder=geocoder)
        reply = f.say(blob())
        assert "try again" in reply and f.provider.estimate_calls == 0
        assert not f.b.quote.pickup.resolved.has_coordinates


def test_ambiguous_candidate_without_coordinates_is_geocoded_after_pick() -> None:
    f = Geo()
    f.say(blob(pickup="7 Ambiguous Close, Ikeja"))
    assert f.b.status == BookingStatus.address_clarification
    f.say("2")
    assert f.b.status == BookingStatus.awaiting_confirmation
    pickup = f.b.quote.pickup.resolved
    assert "(South)" in pickup.formatted_address and pickup.coordinates_source == "geocoded"


def test_changing_an_address_regeocodes_it() -> None:
    f = Geo()
    f.say(blob())
    before = len(f.geocoder.calls)
    f.say("Actually destination: 9 Allen Avenue, Ikeja")
    assert len(f.geocoder.calls) == before + 1
    assert f.geocoder.calls[-1] == "9 Allen Avenue, Ikeja"


def test_coordinate_ranges_are_validated() -> None:
    ResolvedAddress(place_id="p", formatted_address="a", latitude=90, longitude=-180)
    for lat, lng in ((91, 0), (-90.1, 0), (0, 180.1), (0, -181)):
        with pytest.raises(ValidationError):
            ResolvedAddress(place_id="p", formatted_address="a", latitude=lat, longitude=lng)


# ---- Google adapter against mocked HTTP --------------------------------------
KEY = "SECRET-KEY-123"


def google(handler) -> GoogleMapsGeocoder:
    return GoogleMapsGeocoder(KEY, client=httpx.Client(transport=httpx.MockTransport(handler)))


def result(lat=6.5, lng=3.3, kind="ROOFTOP", partial=False):
    r = {"geometry": {"location": {"lat": lat, "lng": lng}, "location_type": kind}}
    if partial:
        r["partial_match"] = True
    return r


def test_google_single_result_is_accepted_and_key_is_sent_as_param() -> None:
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json={"status": "OK", "results": [result()]})

    found = google(handler).geocode("12 King Street, Ikeja")
    assert found.status == GeocodeStatus.ok
    assert (found.latitude, found.longitude, found.precision) == (6.5, 3.3, "ROOFTOP")
    assert seen == {"address": "12 King Street, Ikeja", "key": KEY}


def test_google_partial_or_multiple_results_are_ambiguous() -> None:
    for results in ([result(partial=True)], [result(), result(lat=7)]):
        got = google(
            lambda r, res=results: httpx.Response(200, json={"status": "OK", "results": res})
        )
        assert got.geocode("x").status == GeocodeStatus.ambiguous


def test_google_zero_results_is_not_found() -> None:
    got = google(lambda r: httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []}))
    assert got.geocode("x").status == GeocodeStatus.not_found


@pytest.mark.parametrize("status", ["OVER_QUERY_LIMIT", "REQUEST_DENIED", "UNKNOWN_ERROR"])
def test_google_service_statuses_are_outages(status) -> None:
    got = google(lambda r: httpx.Response(200, json={"status": status, "results": []}))
    with pytest.raises(GeocoderError):
        got.geocode("x")


def test_google_http_errors_timeouts_and_garbage_are_outages_without_leaking_the_key() -> None:
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    handlers = [
        lambda r: httpx.Response(500),
        timeout,
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"status": "OK", "results": [{"geometry": {}}]}),
    ]
    for handler in handlers:
        with pytest.raises(GeocoderError) as err:
            google(handler).geocode("x")
        assert KEY not in str(err.value) and KEY not in repr(err.value.__cause__)
