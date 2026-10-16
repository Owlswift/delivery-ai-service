import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.metrics import router as metrics_router
from app.api.middleware import ObservabilityMiddleware
from app.api.v1 import router as v1_router
from app.booking.machine import IllegalTransition
from app.config import get_settings
from app.errors import (
    AppError,
    app_error_handler,
    http_exception_handler,
    validation_exception_handler,
)
from app.observability.logs import configure_logging
from app.schemas import HealthResponse


async def illegal_transition_handler(request, exc: IllegalTransition):
    return await app_error_handler(
        request,
        AppError(
            409,
            "illegal_transition",
            str(exc),
            {"from": exc.from_status, "to": exc.to_status},
        ),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if get_settings().recover_on_startup:
        from app.api.v1 import sessions as s
        from app.events.recovery import recover_pending

        try:
            from app.providers.selection import Providers, providers_for

            direct = Providers(s.get_provider(), s.get_booking_provider(), s.get_offer_provider())

            def factory(session, now):
                active = providers_for(session.booking, now, direct)
                return active.booking, active.offers

            recover_pending(
                s.get_store(),
                direct.booking,
                direct.offers,
                s.get_ranking(),
                provider_factory=factory,
            )
        except Exception:  # recovery must never block startup; next message/event retries
            logging.getLogger("app").error("startup.recovery_failed", exc_info=True)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    application = FastAPI(
        lifespan=lifespan,
        title="Delivery AI Service",
        version="0.1.0",
        description=(
            "Logistics-agnostic booking conversation API. "
            "The service owns session state; clients send only new user turns."
        ),
    )
    application.add_middleware(ObservabilityMiddleware)
    application.add_exception_handler(AppError, app_error_handler)
    application.add_exception_handler(IllegalTransition, illegal_transition_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.include_router(v1_router)
    application.include_router(metrics_router)

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    def health() -> HealthResponse:
        return HealthResponse()

    return application


app = create_app()
