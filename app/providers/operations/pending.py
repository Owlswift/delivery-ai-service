class OperationPending(Exception):
    """A provider call was queued as an operation; the turn must suspend and resume
    when the executor posts the result."""

    def __init__(self, operation_id: str, operation_type: str) -> None:
        super().__init__(f"{operation_type} pending ({operation_id})")
        self.operation_id = operation_id
        self.operation_type = operation_type
