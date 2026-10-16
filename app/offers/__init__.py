"""Offers: validate, store, rank and select courier offers.

models   offer events and the offer state on the booking (data module)
ranking  `RankingPolicy` and the deterministic `NearestPickupPolicy`
prompts  user-facing wording
engine   `ingest`, `evaluate`, `on_user_message`, assignment with reconciliation
"""
