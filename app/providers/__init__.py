"""Providers: everything about talking to a logistics provider.

models      contract value objects (data module)
protocols   the three provider interfaces and their errors
operations  the operation queue for the provider's server-side executor
selection   deferred (production) or direct (local/test) providers per request
geocoder    geocoding port; google_geocoder is the adapter
fake_*      deterministic in-process fakes for tests and local runs
"""
