"""Provider operations published to the provider's trusted server-side executor.

pending   `OperationPending`, raised when a provider call is queued (leaf module)
models    operation state, argument/result schemas, wire envelopes (data module)
book      `OperationBook`: enqueue, lease, retry/backoff, apply results
deferred  `DeferredProvider`: the provider protocols over recorded operations
"""
