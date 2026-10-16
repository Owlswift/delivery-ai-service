import pytest
from fastapi.testclient import TestClient

from app.api.v1 import sessions as sessions_module
from app.config import get_settings
from app.main import create_app
from app.persistence.session_store import InMemorySessionStore
from app.providers.fake_booking import FakeBookingProvider
from app.providers.fake_logistics import FakeLogisticsProvider
from app.providers.fake_offers import FakeOfferProvider


@pytest.fixture()
def provider() -> FakeLogisticsProvider:
    return FakeLogisticsProvider()


@pytest.fixture()
def booking_provider() -> FakeBookingProvider:
    return FakeBookingProvider()


@pytest.fixture()
def offer_provider() -> FakeOfferProvider:
    return FakeOfferProvider()


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch,
    provider: FakeLogisticsProvider,
    booking_provider: FakeBookingProvider,
    offer_provider: FakeOfferProvider,
) -> TestClient:
    monkeypatch.setenv("API_KEYS", "dev-key:acme,other-key:beta")
    monkeypatch.setenv("RECOVER_ON_STARTUP", "false")
    monkeypatch.setenv("PROVIDER_MODE", "direct")
    monkeypatch.setenv("PROVIDER_SECRETS", "acme:acme-secret,beta:beta-secret")
    monkeypatch.setenv("CLIENT_TOKEN_SECRET", "client-secret")
    get_settings.cache_clear()
    app = create_app()
    store = InMemorySessionStore()
    app.dependency_overrides[sessions_module.get_store] = lambda: store
    app.dependency_overrides[sessions_module.get_provider] = lambda: provider
    app.dependency_overrides[sessions_module.get_booking_provider] = lambda: booking_provider
    app.dependency_overrides[sessions_module.get_offer_provider] = lambda: offer_provider
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setenv("API_KEYS", "dev-key:acme,other-key:beta")
    monkeypatch.setenv("RECOVER_ON_STARTUP", "false")
    monkeypatch.setenv("PROVIDER_MODE", "deferred")
    monkeypatch.setenv("PROVIDER_SECRETS", "acme:acme-secret,beta:beta-secret")
    monkeypatch.setenv("CLIENT_TOKEN_SECRET", "client-secret")
    monkeypatch.setenv("OPERATION_BACKOFF_BASE_S", "0")
    get_settings.cache_clear()
    app = create_app()
    store = InMemorySessionStore()
    app.dependency_overrides[sessions_module.get_store] = lambda: store
    with TestClient(app) as client:
        client.store = store
        yield client
    get_settings.cache_clear()
