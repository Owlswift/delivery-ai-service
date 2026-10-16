"""Events: asynchronous input from providers.

models    event envelope and results (data module)
handler   validate, deduplicate, state-guard and dispatch provider events
resume    continue the workflow when an executor posts an operation result
recovery  sweep for bookings left mid-flight after a restart or outage
"""
