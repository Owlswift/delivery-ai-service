from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.offers.models import Offer


@dataclass
class Ranking:
    ordered: list[Offer]
    metric: str | None


class RankingPolicy(Protocol):
    version: str

    def rank(self, offers: list[Offer]) -> Ranking:
        """Pure and deterministic: same offers in, same order out."""


_FAR = float("inf")


class NearestPickupPolicy:
    """nearest-pickup-v1. Callers pass only eligible, within-budget offers.

    One metric per call: pickup ETA if every candidate has it, else pickup
    distance (candidates without it last). No tolerance and no weights; ties
    break on price, then received_at, then offer_id.
    """

    version = "nearest-pickup-v1"

    def rank(self, offers: list[Offer]) -> Ranking:
        if offers and all(o.pickup_eta_min is not None for o in offers):
            metric = "pickup_eta_min"
        elif any(o.pickup_distance_km is not None for o in offers):
            metric = "pickup_distance_km"
        else:
            metric = None

        def key(offer: Offer) -> tuple[float, Decimal, datetime, str]:
            value = getattr(offer, metric) if metric else None
            return (
                _FAR if value is None else float(value),
                Decimal(offer.price),
                offer.received_at,
                offer.offer_id,
            )

        return Ranking(ordered=sorted(offers, key=key), metric=metric)
