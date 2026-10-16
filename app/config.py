from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_keys: SecretStr = SecretStr("dev-key:acme")
    database_path: str = "delivery_ai.db"
    recover_on_startup: bool = True
    google_maps_api_key: SecretStr | None = None
    geocoder_timeout_s: float = 5.0
    provider_mode: str = "deferred"  # "deferred" (operations) or "direct" (in-process fakes)
    provider_secrets: SecretStr = SecretStr("")  # "provider_id:secret,..." (signed calls)
    client_token_secret: SecretStr = SecretStr("")
    metrics_token: SecretStr | None = None  # /metrics is disabled when unset
    log_level: str = "INFO"
    log_json: bool = True
    client_token_ttl_s: int = 3600
    signature_window_s: int = 300
    operation_max_attempts: int = 5
    operation_backoff_base_s: float = 2.0
    operation_backoff_cap_s: float = 60.0
    operation_lease_s: int = 60
    operation_timeout_s: int = 30
    operation_exhausted_cooldown_s: int = 300

    def secret_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for pair in self.provider_secrets.get_secret_value().split(","):
            provider_id, _, secret = pair.strip().partition(":")
            if provider_id.strip() and secret.strip():
                mapping[provider_id.strip()] = secret.strip()
        return mapping

    def client_secret(self) -> str:
        return self.client_token_secret.get_secret_value()

    def key_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for pair in self.api_keys.get_secret_value().split(","):
            pair = pair.strip()
            if not pair:
                continue
            key, _, company_id = pair.partition(":")
            key = key.strip()
            company_id = company_id.strip()
            if key and company_id:
                mapping[key] = company_id
        return mapping


@lru_cache
def get_settings() -> Settings:
    return Settings()
