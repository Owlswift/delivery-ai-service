# Resolved-address coordinates and geocoding fallback

**Status:** implemented  
**Date:** 2026-10-16

Resolves provider-contract decision 2 (address representation). Every address that reaches fare estimation or booking creation has coordinates. If the provider's resolved location has none, the service geocodes the address with Google Maps. This module sits inside address resolution (`app/quoting`); it does not change the workflow states.

## Locked decisions

- **Coordinates are required.** A location counts as `resolved` only when it has `latitude` and `longitude` (decimal degrees, WGS84; latitude -90..90, longitude -180..180). Out-of-range values are invalid, not "close enough".
- **Provider coordinates win.** They are used as given and marked `coordinates_source: provider`. The geocoder is called only when they are absent.
- **Geocode the provider's formatted address** (the location the provider already confirmed), not the user's raw text.
- **Never guess a location.** The geocode is accepted only when Google returns exactly one result and it is not a `partial_match`. Zero results, several results, or a partial match turn the address into `not_found`, and the user is asked for a fuller address. Accepted results record `coordinates_source: geocoded` and Google's `location_type` as `precision`.
- **Outages are not "not found".** Quota, denied key, network error or timeout is a geocoder outage: nothing is stored as resolved, the user is told to try again, and the next turn retries (same handling as a provider outage).
- The Google API key is server-side configuration (`GOOGLE_MAPS_API_KEY`) and is never logged or placed in error text. With no key configured the geocoder reports "unavailable", it does not invent coordinates.
- `Geocoder` is a port; Google is one adapter. A fake geocoder serves tests.

## Flow

1. Provider resolves the address (`resolved` / `ambiguous` / `not_found`).
2. `resolved` with coordinates -> keep (source `provider`).
3. `resolved` without coordinates -> geocode `formatted_address`: one clean result -> add coordinates (source `geocoded`); otherwise `not_found`; outage -> "try again".
4. `ambiguous`: candidates may lack coordinates; the chosen candidate goes through step 3 after the user picks it.
5. Fare estimation and booking creation receive only addresses with coordinates (the booking payload and `ready_to_book` result now include them).
6. Changing an address re-runs resolution and geocoding (resolutions are keyed by input text, as before).

## Precision

Google's `location_type` (`ROOFTOP`, `RANGE_INTERPOLATED`, `GEOMETRIC_CENTER`, `APPROXIMATE`) is stored but not used to reject anything in v1. Street-level validation already runs earlier. Rejecting `APPROXIMATE` is a possible follow-up.

## Tests

Provider coordinates used without calling the geocoder; missing coordinates geocoded and carried into quote request, booking payload and result; zero/ambiguous/partial results ask for a fuller address with no fare request; geocoder outage retries next turn; no geocoder configured behaves as outage; candidate picked without coordinates is geocoded; changed address re-geocodes; coordinate range validation; Google adapter against mocked HTTP (OK, partial match, multiple, ZERO_RESULTS, quota/denied status, HTTP error, timeout, key not leaked).
