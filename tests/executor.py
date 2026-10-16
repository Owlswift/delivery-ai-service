"""A stand-in for the provider's server-side executor: leases operations, runs them
against the in-process fakes, and posts the formatted result back."""

from app.providers.fake_booking import FakeBookingProvider
from app.providers.fake_logistics import FakeLogisticsProvider
from app.providers.fake_offers import FakeOfferProvider
from app.providers.models import BookingPayload, ResolvedAddress
from app.providers.protocols import (
    BookingProviderError,
    BookingRejected,
    BookingTimeout,
    OfferProviderError,
    OfferTimeout,
    OfferUnavailable,
    ProviderError,
)
from tests.signing import signed


class Executor:
    def __init__(self, client, logistics=None, bookings=None, offers=None, provider="acme"):
        self.client = client
        self.logistics = logistics or FakeLogisticsProvider()
        self.bookings = bookings or FakeBookingProvider()
        self.offers = offers or FakeOfferProvider()
        self.provider = provider
        self.executed: list[dict] = []

    def lease(self, limit=10):
        r = signed(
            self.client, "POST", "/v1/operations/lease", {"limit": limit}, provider=self.provider
        )
        assert r.status_code == 200, r.text
        return r.json()

    def execute(self, env) -> dict:
        args = env["args"]
        try:
            kind = env["type"]
            if kind == "resolve_address":
                result = self.logistics.resolve_address(args["text"]).model_dump(mode="json")
            elif kind == "estimate_fare":
                result = self.logistics.estimate_fare(
                    ResolvedAddress(**args["pickup"]), ResolvedAddress(**args["destination"])
                ).model_dump(mode="json")
            elif kind == "create_booking":
                result = self.bookings.create_booking(
                    BookingPayload.model_validate(args["payload"]), env["idempotency_key"]
                ).model_dump(mode="json")
            elif kind == "assign_offer":
                result = self.offers.assign_offer(
                    args["booking_reference"], args["offer_id"], env["idempotency_key"]
                ).model_dump(mode="json")
            else:
                raise AssertionError(kind)
            return {"outcome": "succeeded", "result": result}
        except (BookingRejected, OfferUnavailable) as exc:
            return {"outcome": "rejected", "error": {"code": "rejected", "message": str(exc)}}
        except (BookingTimeout, OfferTimeout):
            return {"outcome": "unknown"}
        except (ProviderError, BookingProviderError, OfferProviderError):
            return {"outcome": "unavailable"}

    def post(self, env, outcome: dict):
        body = {"attempt": env["attempt"], **outcome}
        return signed(
            self.client,
            "POST",
            f"/v1/operations/{env['operation_id']}/result",
            body,
            provider=self.provider,
        )

    def run(self, rounds=20) -> int:
        """Lease, execute and post until the queue is empty. Returns operations run."""
        count = 0
        for _ in range(rounds):
            batch = self.lease()
            if not batch:
                return count
            for env in batch:
                self.executed.append(env)
                r = self.post(env, self.execute(env))
                assert r.status_code == 200, r.text
                count += 1
        raise AssertionError("executor did not drain the queue")
