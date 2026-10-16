"""Observability: structured logs, correlation, metrics and audit events.

context    correlation / provider / session ids carried through a request
redaction  removes personal data and secrets from anything that gets logged
logs       JSON log formatter, `configure_logging`, `log`
metrics    in-process counters and histograms, Prometheus text rendering
audit      `audit()`: security- and workflow-relevant events (identifiers only)
tracing    `span()`: timed steps recorded as metrics and logs

This module depends on nothing else in the service so every other module can use it.
Bodies, phone numbers, addresses, tokens and secrets are never logged.
"""
