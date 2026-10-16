import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import PlainTextResponse

from app.config import Settings, get_settings
from app.errors import AppError
from app.observability.metrics import REGISTRY

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def metrics(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Prometheus text format. Disabled unless METRICS_TOKEN is set; then requires it."""
    if settings.metrics_token is None:
        raise AppError(404, "not_found", "Not found")
    expected = f"Bearer {settings.metrics_token.get_secret_value()}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise AppError(401, "unauthorized", "Invalid metrics token")
    return REGISTRY.render()
