from dataclasses import dataclass
from datetime import datetime

from app.booking.models import BookingState
from app.config import get_settings
from app.providers.operations.book import ops_config
from app.providers.operations.deferred import DeferredProvider
from app.providers.protocols import BookingProvider, LogisticsProvider, OfferProvider


@dataclass
class Providers:
    logistics: LogisticsProvider
    booking: BookingProvider
    offers: OfferProvider


def providers_for(booking: BookingState, now: datetime, direct: Providers) -> Providers:
    """Deferred (operations for the provider's server-side executor) in production;
    the in-process `direct` providers for local runs and tests."""
    if get_settings().provider_mode == "deferred":
        deferred = DeferredProvider(booking, now, ops_config())
        return Providers(deferred, deferred, deferred)
    return direct
