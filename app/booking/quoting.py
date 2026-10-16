from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from app.booking import quoting_prompts as prompts
from app.booking.machine import transition
from app.booking.models import (
    USER_DETAIL_FIELDS,
    Acceptance,
    BookingState,
    BookingStatus,
    DetailsCompleteResult,
    NextAction,
    NextActionType,
    PresentedFare,
    QuoteState,
    QuoteSummary,
    ReadyToBookResult,
)
from app.errors import AppError
from app.providers.geocoder import Geocoder, GeocoderError, GeocodeStatus
from app.providers.models import AddressResolution, CoordinatesSource, FareQuote, ResolutionStatus
from app.providers.protocols import LogisticsProvider, ProviderError

if TYPE_CHECKING:
    from app.schemas import Session

AFFIRMATIVE = re.compile(
    r"^\s*(yes|yep|yeah|y|ok|okay|sure|proceed|go ahead|confirm|confirmed|"
    r"please do|do it|book it)\b",
    re.IGNORECASE,
)
NEGATIVE = re.compile(r"\b(no|nope|cancel|don'?t|do not|stop|not now|wait)\b", re.IGNORECASE)

SIDES = (("pickup", "pickup_address"), ("destination", "destination_address"))


class _Unavailable(Exception):
    pass


def detail_values(booking: BookingState) -> tuple[str | None, ...]:
    return tuple(getattr(booking.details, n).value for n in USER_DETAIL_FIELDS)


def _fingerprint(booking: BookingState, quote: FareQuote) -> str:
    return json.dumps(
        [
            detail_values(booking),
            quote.pickup_place_id,
            quote.destination_place_id,
            quote.base_fare,
            quote.currency,
        ]
    )


def _set(
    booking: BookingState,
    status: BookingStatus,
    action: NextAction,
) -> None:
    transition(booking, status)
    booking.next_actions = [action]


def _ask(field: str, prompt: str) -> NextAction:
    return NextAction(type=NextActionType.ask_user, field=field, prompt=prompt)


def select_candidate(booking: BookingState, text: str) -> bool:
    """Apply a pick among provider candidates. Returns True if the turn was consumed."""
    if booking.status != BookingStatus.address_clarification or booking.quote is None:
        return False
    if not booking.next_actions:
        return False
    field = booking.next_actions[0].field
    side = next((s for s, f in SIDES if f == field), None)
    if side is None:
        return False
    resolution: AddressResolution | None = getattr(booking.quote, side)
    if resolution is None or resolution.status != ResolutionStatus.ambiguous:
        return False
    if resolution.input != getattr(booking.details, field).value:
        return False
    stripped = text.strip().rstrip(".")
    chosen = None
    if stripped.isdigit() and 1 <= int(stripped) <= len(resolution.candidates):
        chosen = resolution.candidates[int(stripped) - 1]
    else:
        matches = [
            c
            for c in resolution.candidates
            if stripped and stripped.lower() in c.formatted_address.lower()
        ]
        if len(matches) == 1:
            chosen = matches[0]
    if chosen is None:
        return False
    setattr(
        booking.quote,
        side,
        AddressResolution(
            input=resolution.input, status=ResolutionStatus.resolved, resolved=chosen
        ),
    )
    return True


def _ensure_coordinates(
    resolution: AddressResolution, geocoder: Geocoder | None
) -> AddressResolution:
    """A `resolved` location must carry coordinates: keep the provider's, else geocode."""
    address = resolution.resolved
    if resolution.status != ResolutionStatus.resolved or address is None:
        return resolution
    if address.has_coordinates:
        if address.coordinates_source is None:
            address = address.model_copy(update={"coordinates_source": CoordinatesSource.provider})
            return resolution.model_copy(update={"resolved": address})
        return resolution
    if geocoder is None:
        raise _Unavailable
    try:
        found = geocoder.geocode(address.formatted_address)
    except GeocoderError as exc:
        raise _Unavailable from exc
    if found.status != GeocodeStatus.ok or found.latitude is None or found.longitude is None:
        return AddressResolution(input=resolution.input, status=ResolutionStatus.not_found)
    address = address.model_copy(
        update={
            "latitude": found.latitude,
            "longitude": found.longitude,
            "coordinates_source": CoordinatesSource.geocoded,
            "precision": found.precision,
        }
    )
    return resolution.model_copy(update={"resolved": address})


def _resolve_sides(
    booking: BookingState, provider: LogisticsProvider, geocoder: Geocoder | None
) -> str | None:
    """Resolve pickup then destination. Returns a reply if one needs the user."""
    quote_state = booking.quote
    assert quote_state is not None
    for side, field_name in SIDES:
        text = getattr(booking.details, field_name).value or ""
        resolution = getattr(quote_state, side)
        if resolution is None or resolution.input != text:
            try:
                resolution = provider.resolve_address(text)
            except ProviderError as exc:
                raise _Unavailable from exc
            setattr(quote_state, side, resolution)
        ensured = _ensure_coordinates(resolution, geocoder)
        if ensured is not resolution:
            resolution = ensured
            setattr(quote_state, side, resolution)
        label = "pickup" if side == "pickup" else "destination"
        if resolution.status == ResolutionStatus.ambiguous:
            prompt = prompts.address_choices(label, resolution.candidates)
        elif resolution.status == ResolutionStatus.not_found:
            prompt = prompts.address_not_found(label)
        else:
            continue
        _set(booking, BookingStatus.address_clarification, _ask(field_name, prompt))
        return prompt
    return None


def _fresh_quote(booking: BookingState, provider: LogisticsProvider, now: datetime) -> FareQuote:
    state = booking.quote
    assert state is not None and state.pickup and state.destination
    pickup, destination = state.pickup.resolved, state.destination.resolved
    assert pickup is not None and destination is not None
    assert pickup.has_coordinates and destination.has_coordinates
    quote = state.quote
    stale = (
        quote is None
        or (quote.pickup_place_id, quote.destination_place_id)
        != (pickup.place_id, destination.place_id)
        or quote.expires_at <= now
    )
    if stale:
        try:
            quote = provider.estimate_fare(pickup, destination)
        except ProviderError as exc:
            raise _Unavailable from exc
        state.quote = quote
    assert quote is not None
    return quote


def _budget_problem(booking: BookingState, quote: FareQuote) -> str | None:
    details = booking.details
    if details.budget_currency and details.budget_currency != quote.currency:
        return prompts.budget_currency_mismatch(quote)
    if Decimal(details.budget_amount or "0") < Decimal(quote.base_fare):
        return prompts.budget_too_low(details.sender_max_budget.value or "", quote)
    return None


def _ready(booking: BookingState, quote: FareQuote) -> str:
    state = booking.quote
    assert state and state.pickup and state.destination
    pickup, destination = state.pickup.resolved, state.destination.resolved
    assert pickup is not None and destination is not None
    details_result = booking.result
    assert isinstance(details_result, DetailsCompleteResult)
    booking.result = ReadyToBookResult(
        pickup=pickup,
        destination=destination,
        quote=QuoteSummary(
            quote_id=quote.quote_id,
            base_fare=quote.base_fare,
            currency=quote.currency,
            expires_at=quote.expires_at,
        ),
        details=details_result,
    )
    _set(booking, BookingStatus.ready_to_book, NextAction(type=NextActionType.none))
    return prompts.READY


def advance(
    session: Session,
    text: str,
    *,
    details_changed: bool,
    prev_status: BookingStatus,
    provider: LogisticsProvider,
    now: datetime,
    geocoder: Geocoder | None = None,
) -> str:
    """Quoting turn. Call only when collection reports `details_complete`."""
    booking = session.booking
    if booking.quote is None:
        booking.quote = QuoteState()
    if details_changed:
        booking.quote.acceptance = None
    try:
        return _advance(booking, text, details_changed, prev_status, provider, now, geocoder)
    except _Unavailable:
        _set(booking, BookingStatus.details_complete, NextAction(type=NextActionType.none))
        return prompts.PROVIDER_UNAVAILABLE


def _advance(
    booking: BookingState,
    text: str,
    details_changed: bool,
    prev_status: BookingStatus,
    provider: LogisticsProvider,
    now: datetime,
    geocoder: Geocoder | None,
) -> str:
    state = booking.quote
    assert state is not None
    reply = _resolve_sides(booking, provider, geocoder)
    if reply is not None:
        return reply

    quote = _fresh_quote(booking, provider, now)

    problem = _budget_problem(booking, quote)
    if problem is not None:
        _set(booking, BookingStatus.budget_revision, _ask("sender_max_budget", problem))
        return problem

    fingerprint = _fingerprint(booking, quote)
    acceptance = state.acceptance
    if acceptance is not None and acceptance.fingerprint == fingerprint:
        acceptance.quote_id = quote.quote_id
        return _ready(booking, quote)
    state.acceptance = None

    current = PresentedFare(base_fare=quote.base_fare, currency=quote.currency)
    awaiting = prev_status == BookingStatus.awaiting_confirmation and not details_changed
    if awaiting and state.presented_fare == current:
        if AFFIRMATIVE.search(text) and not NEGATIVE.search(text):
            state.acceptance = Acceptance(
                quote_id=quote.quote_id,
                base_fare=quote.base_fare,
                currency=quote.currency,
                fingerprint=fingerprint,
                accepted_at=now,
            )
            return _ready(booking, quote)
        prompt = prompts.DECLINED if NEGATIVE.search(text) else prompts.REASK
        _set(
            booking,
            BookingStatus.awaiting_confirmation,
            NextAction(type=NextActionType.confirm, field="proceed", prompt=prompt),
        )
        return prompt

    old = state.presented_fare
    state.presented_fare = current
    if awaiting and old is not None:
        prompt = prompts.fare_changed(f"{old.base_fare} {old.currency}", quote)
    else:
        assert state.pickup and state.destination
        assert state.pickup.resolved and state.destination.resolved
        prompt = prompts.present_quote(
            state.pickup.resolved,
            state.destination.resolved,
            quote,
            booking.details.sender_max_budget.value or "",
            booking.details.package_description.value or "",
            f"{booking.details.recipient_name.value} ({booking.details.recipient_phone.value})",
        )
    _set(
        booking,
        BookingStatus.awaiting_confirmation,
        NextAction(type=NextActionType.confirm, field="proceed", prompt=prompt),
    )
    return prompt


def ensure_bookable(session: Session, provider: LogisticsProvider, now: datetime) -> FareQuote:
    """Guard for booking creation. Mutates the session on a stale or changed
    quote before raising, so callers must persist the session even on error."""
    booking = session.booking
    state = booking.quote
    if (
        booking.status != BookingStatus.ready_to_book
        or state is None
        or state.acceptance is None
        or state.quote is None
        or state.pickup is None
        or state.destination is None
        or state.pickup.resolved is None
        or state.destination.resolved is None
    ):
        raise AppError(409, "not_confirmed", "The user has not confirmed a quote")
    acceptance, quote = state.acceptance, state.quote
    if _fingerprint(booking, quote) != acceptance.fingerprint:
        raise AppError(409, "not_confirmed", "Details changed since the user confirmed")
    if quote.expires_at > now:
        return quote
    try:
        fresh = provider.estimate_fare(state.pickup.resolved, state.destination.resolved)
    except ProviderError as exc:
        raise AppError(503, "provider_unavailable", "Could not revalidate the quote") from exc
    state.quote = fresh
    if (fresh.base_fare, fresh.currency) == (acceptance.base_fare, acceptance.currency):
        acceptance.quote_id = fresh.quote_id
        return fresh
    state.acceptance = None
    _set(
        booking,
        BookingStatus.awaiting_confirmation,
        NextAction(type=NextActionType.confirm, field="proceed"),
    )
    raise AppError(
        409,
        "quote_changed",
        "The fare changed; the user must confirm again",
        {
            "previous": f"{acceptance.base_fare} {acceptance.currency}",
            "current": f"{fresh.base_fare} {fresh.currency}",
        },
    )
