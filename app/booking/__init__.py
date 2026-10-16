"""Booking: the booking document, its state machine and the deterministic workflow.

models            the booking document (`BookingState`) and its parts (data module)
machine           logical states, legal transitions, audit history, `transition()`
quoting           address resolution (with coordinates), fare quote, budget, consent
creation          booking creation with the provider: confirmation, idempotency, reconcile
money             budget parsing
prompts           wording for cancel / failure / provider-closure messages
quoting_prompts   wording for the quote and its clarifications
creation_prompts  wording for booking creation outcomes

Package `__init__` files stay empty of re-exports so importing a data module never
imports logic (see tests/test_architecture.py).
"""
