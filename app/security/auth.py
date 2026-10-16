import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request, Security
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings
from app.errors import AppError
from app.observability.audit import audit
from app.observability.metrics import AUTH_FAILURES

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def _failed(kind: str, **fields: str) -> None:
    AUTH_FAILURES.inc(kind=kind)
    audit("auth.failed", kind=kind, **fields)


def require_company_id(
    api_key: Annotated[str | None, Security(API_KEY_HEADER)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    if not api_key:
        _failed("api_key_missing")
        raise AppError(401, "unauthorized", "Missing X-API-Key header")
    company_id = settings.key_map().get(api_key)
    if not company_id:
        _failed("api_key_invalid")
        raise AppError(401, "unauthorized", "Invalid API key")
    return company_id


# ---- signed provider-server requests -----------------------------------------
@dataclass
class ProviderPrincipal:
    provider_id: str  # also the tenant (company) id
    correlation_id: str | None


def sign_request(secret: str, timestamp: str, method: str, path_query: str, body: bytes) -> str:
    """HMAC-SHA256 over timestamp, method, path+query and the body hash."""
    message = "\n".join([timestamp, method.upper(), path_query, hashlib.sha256(body).hexdigest()])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


async def require_provider(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProviderPrincipal:
    """Authenticates a provider's server-side component by signature. Reveals nothing
    about which check failed."""
    provider_id = request.headers.get("X-Provider-Id", "")
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")
    secret = settings.secret_map().get(provider_id)
    denied = AppError(401, "unauthorized", "Invalid provider signature")
    if not (secret and timestamp and signature):
        _failed("signature_missing_or_unknown_provider")
        raise denied
    try:
        age = abs(time.time() - float(timestamp))
    except ValueError:
        _failed("signature_bad_timestamp")
        raise denied from None
    if age > settings.signature_window_s:
        _failed("signature_stale", provider_id=provider_id[:64])
        raise denied
    path_query = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    expected = sign_request(secret, timestamp, request.method, path_query, await request.body())
    if not hmac.compare_digest(expected, signature):
        _failed("signature_mismatch", provider_id=provider_id[:64])
        raise denied
    return ProviderPrincipal(provider_id, request.headers.get("X-Correlation-Id"))


# ---- client tokens for the frontend SDK ----------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_client_token(
    settings: Settings, session_id: UUID, company_id: str, now: datetime
) -> tuple[str, datetime]:
    if not settings.client_secret():
        raise AppError(501, "not_configured", "Client tokens are not configured")
    expires = now + timedelta(seconds=settings.client_token_ttl_s)
    body = _b64(
        json.dumps({"sid": str(session_id), "cid": company_id, "exp": expires.timestamp()}).encode()
    )
    sig = _b64(hmac.new(settings.client_secret().encode(), body.encode(), hashlib.sha256).digest())
    return f"v1.{body}.{sig}", expires


def verify_client_token(settings: Settings, token: str, now: datetime) -> tuple[UUID, str]:
    denied = AppError(401, "unauthorized", "Invalid or expired client token")
    if not settings.client_secret():
        _failed("client_token_unconfigured")
        raise denied  # never verify against an empty key
    try:
        version, body, sig = token.split(".")
        expected = _b64(
            hmac.new(settings.client_secret().encode(), body.encode(), hashlib.sha256).digest()
        )
        if version != "v1":
            raise denied
        if not hmac.compare_digest(expected, sig):
            raise denied
        claims = json.loads(_unb64(body))
        if float(claims["exp"]) <= now.timestamp():
            raise denied
        return UUID(claims["sid"]), claims["cid"]
    except AppError:
        _failed("client_token_invalid")
        raise
    except Exception:
        _failed("client_token_invalid")
        raise denied from None


def require_session_access(
    session_id: UUID,
    api_key: Annotated[str | None, Security(API_KEY_HEADER)],
    authorization: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> str:
    """Frontend access to one session: a client token scoped to it, or the server API key."""
    if authorization and authorization.lower().startswith("bearer "):
        sid, company_id = verify_client_token(
            settings, authorization[7:].strip(), datetime.now(UTC)
        )
        if sid != session_id:
            raise AppError(401, "unauthorized", "Invalid or expired client token")
        return company_id
    return require_company_id(api_key, settings)
