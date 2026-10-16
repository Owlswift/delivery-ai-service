from uuid import uuid4

from fastapi.testclient import TestClient

HEADERS = {"X-API-Key": "dev-key"}
SENDER = {"sender_phone": "+2348030000000"}
OTHER = {"X-API-Key": "other-key"}


def test_health_does_not_require_auth(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_api_key_is_401(client: TestClient) -> None:
    response = client.post("/v1/sessions", json=SENDER)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_invalid_api_key_is_401(client: TestClient) -> None:
    response = client.post("/v1/sessions", json=SENDER, headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_create_message_get_flow(client: TestClient) -> None:
    created = client.post(
        "/v1/sessions",
        json={**SENDER, "locale": "en-US", "external_ref": "cust-1"},
        headers=HEADERS,
    )
    assert created.status_code == 201
    session = created.json()
    assert session["company_id"] == "acme"
    assert session["status"] == "active"
    assert session["messages"][0]["role"] == "assistant"
    assert session["booking"]["status"] == "collecting"
    assert session["booking"]["missing_fields"][0] == "pickup_address"
    assert session["booking"]["details"]["sender_phone"]["status"] == "valid"
    assert session["booking"]["next_actions"][0]["type"] == "ask_user"
    session_id = session["id"]

    turned = client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "Pickup: 12 King Street, Ikeja", "channel": "text"},
        headers=HEADERS,
    )
    assert turned.status_code == 200
    body = turned.json()
    assert body["message"]["role"] == "assistant"
    assert "pickup_address" not in body["session"]["booking"]["missing_fields"]
    assert body["session"]["booking"]["next_actions"][0]["field"] == "destination_address"
    roles = [m["role"] for m in body["session"]["messages"]]
    assert roles == ["assistant", "user", "assistant"]

    fetched = client.get(f"/v1/sessions/{session_id}", headers=HEADERS)
    assert fetched.status_code == 200
    assert len(fetched.json()["messages"]) == 3
    assert fetched.json()["external_ref"] == "cust-1"


def test_tenant_isolation(client: TestClient) -> None:
    created = client.post("/v1/sessions", json=SENDER, headers=HEADERS)
    session_id = created.json()["id"]

    other_get = client.get(f"/v1/sessions/{session_id}", headers=OTHER)
    assert other_get.status_code == 404

    other_msg = client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "hello"},
        headers=OTHER,
    )
    assert other_msg.status_code == 404


def test_unknown_session_is_404(client: TestClient) -> None:
    response = client.get(f"/v1/sessions/{uuid4()}", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_empty_message_is_422(client: TestClient) -> None:
    created = client.post("/v1/sessions", json=SENDER, headers=HEADERS)
    session_id = created.json()["id"]
    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": ""},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_client_message_id_is_idempotent(client: TestClient) -> None:
    created = client.post("/v1/sessions", json=SENDER, headers=HEADERS)
    session_id = created.json()["id"]
    client_message_id = str(uuid4())
    payload = {"content": "Pickup: 12 King Street, Ikeja", "client_message_id": client_message_id}

    first = client.post(f"/v1/sessions/{session_id}/messages", json=payload, headers=HEADERS)
    second = client.post(f"/v1/sessions/{session_id}/messages", json=payload, headers=HEADERS)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["message"]["id"] == second.json()["message"]["id"]
    assert len(second.json()["session"]["messages"]) == len(first.json()["session"]["messages"])


def test_create_requires_sender_phone(client: TestClient) -> None:
    assert client.post("/v1/sessions", json={}, headers=HEADERS).status_code == 422
    bad = client.post("/v1/sessions", json={"sender_phone": "abcdefgh"}, headers=HEADERS)
    assert bad.status_code == 422


def test_full_message_goes_on_to_quote(client: TestClient) -> None:
    session_id = client.post("/v1/sessions", json=SENDER, headers=HEADERS).json()["id"]
    text = (
        "Pickup: 12 King Street, Ikeja\nDestination: 5 Marina Road, Lagos Island\n"
        "Package: documents\nRecipient: Ada Obi\nRecipient phone: 08031234567\n"
        "Budget: 8000 NGN"
    )
    body = client.post(
        f"/v1/sessions/{session_id}/messages", json={"content": text}, headers=HEADERS
    ).json()
    booking = body["session"]["booking"]
    assert booking["status"] == "awaiting_confirmation"
    assert booking["quote"]["quote"]["base_fare"] == "5000"
    assert booking["result"]["sender_max_budget"] == {"amount": "8000", "currency": "NGN"}
    assert booking["result"]["sender_phone"] == "+2348030000000"
    assert "5000 NGN" in body["message"]["content"]


def test_completed_session_still_accepts_correction_and_reverts(client: TestClient) -> None:
    session_id = client.post("/v1/sessions", json=SENDER, headers=HEADERS).json()["id"]
    text = (
        "Pickup: 12 King Street, Ikeja\nDestination: 5 Marina Road, Lagos Island\n"
        "Package: documents\nRecipient: Ada Obi\nRecipient phone: 08031234567\n"
        "Budget: 8000 NGN"
    )
    url = f"/v1/sessions/{session_id}/messages"
    client.post(url, json={"content": text}, headers=HEADERS)
    body = client.post(
        url, json={"content": "Actually the pickup: the mall"}, headers=HEADERS
    ).json()
    booking = body["session"]["booking"]
    assert booking["status"] == "collecting"
    assert booking["result"] is None
    assert booking["ambiguous_fields"] == ["pickup_address"]


def test_non_active_session_is_409(client: TestClient) -> None:
    from app.api.v1 import sessions as sessions_module

    session_id = client.post("/v1/sessions", json=SENDER, headers=HEADERS).json()["id"]
    store = client.app.dependency_overrides[sessions_module.get_store]()
    from uuid import UUID

    session = store.get(UUID(session_id), "acme")
    session.status = "cancelled"
    store.save(session)
    response = client.post(
        f"/v1/sessions/{session_id}/messages", json={"content": "hi"}, headers=HEADERS
    )
    assert response.status_code == 409
