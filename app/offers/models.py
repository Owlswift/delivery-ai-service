"""State owned by the offers module. Must not import from app.collection."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class OfferStatus(StrEnum):
    open = "open"
    withdrawn = "withdrawn"
    expired = "expired"
    unavailable = "unavailable"  # set locally when the provider refuses assignment


class Courier(BaseModel):
    courier_id: str
    name: str | None = None
    phone: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)  # provider data, passed through


class OfferEventPayload(BaseModel):
    """Payload of offer.created / offer.updated / offer.withdrawn / offer.expired."""

    offer_id: str
    courier: Courier
    price: str
    currency: str
    expires_at: datetime | None = None
    pickup_distance_km: float | None = None
    pickup_eta_min: int | None = None


class OfferEvent(BaseModel):
    booking_reference: str
    offer_id: str
    courier: Courier
    price: str
    currency: str
    status: str
    expires_at: datetime | None = None
    pickup_distance_km: float | None = None
    pickup_eta_min: int | None = None
    updated_at: datetime | None = None


class Offer(BaseModel):
    offer_id: str
    courier: Courier
    price: str
    currency: str
    status: OfferStatus
    expires_at: datetime | None = None
    pickup_distance_km: float | None = None
    pickup_eta_min: int | None = None
    received_at: datetime
    updated_at: datetime | None = None


class BudgetChange(BaseModel):
    amount: str
    at: datetime
    reason: str


class Budget(BaseModel):
    amount: str
    currency: str
    history: list[BudgetChange] = Field(default_factory=list)


class PendingApproval(BaseModel):
    offer_id: str
    price: str
    requested_at: datetime


class Decision(BaseModel):
    policy_version: str
    metric: str | None
    ranked_offer_ids: list[str]
    chosen_offer_id: str
    at: datetime


class AssignmentStatus(StrEnum):
    requested = "requested"
    confirmed = "confirmed"


class AssignmentState(BaseModel):
    offer_id: str
    courier_id: str
    idempotency_key: str
    status: AssignmentStatus = AssignmentStatus.requested
    attempts: int = 0
    approved_above_budget: bool = False
    confirmed_at: datetime | None = None


class OfferState(BaseModel):
    offers: dict[str, Offer] = Field(default_factory=dict)
    budget: Budget
    pending_approval: PendingApproval | None = None
    declined: dict[str, str] = Field(default_factory=dict)  # offer_id -> declined price
    decision: Decision | None = None
    assignment: AssignmentState | None = None
