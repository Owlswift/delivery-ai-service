import json

from app.contract.v1 import build_contract
from app.providers.operations.models import ARGS_BY_TYPE, RESULT_BY_TYPE, OperationType
from scripts.export_contract import TARGET, render


def test_committed_contract_matches_the_models() -> None:
    committed = json.loads(TARGET.read_text())
    assert committed == json.loads(render()), (
        "The wire models changed. If this is intentional, regenerate the contract with "
        "`python -m scripts.export_contract` and review the diff."
    )


def test_every_operation_has_frozen_args_and_result_schemas() -> None:
    contract = build_contract()
    assert set(contract["operations"]) == {t.value for t in OperationType}
    assert set(ARGS_BY_TYPE) == set(RESULT_BY_TYPE) == set(OperationType)
    for spec in contract["operations"].values():
        assert spec["executor"] == "provider_server"
        assert spec["args"] in contract["schemas"] and spec["result"] in contract["schemas"]
    keyed = {n for n, s in contract["operations"].items() if s["idempotency_key"]}
    assert keyed == {"create_booking", "assign_offer"}


def test_contract_requires_the_fields_the_spec_requires() -> None:
    schemas = build_contract()["schemas"]
    assert {"event_id", "booking_reference", "type", "occurred_at"} <= set(
        schemas["ProviderEvent"]["required"]
    )
    assert "confirmation" in schemas["BookingPayload"]["required"]
    assert {"base_fare", "currency", "quote_id", "expires_at"} <= set(
        schemas["FareQuote"]["required"]
    )
    assert set(schemas["ProviderEvent"]["properties"]["type"]["enum"]) == set(
        build_contract()["event_types"]
    )
    assert schemas["OperationResultRequest"]["$defs"]["Outcome"]["enum"] == [
        "succeeded",
        "rejected",
        "unavailable",
        "unknown",
    ]
