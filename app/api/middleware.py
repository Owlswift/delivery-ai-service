import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.context import bind, new_correlation_id
from app.observability.logs import log
from app.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

CORRELATION_HEADER = "x-correlation-id"
SAFE_CORRELATION_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def route_template(scope: Scope) -> str:
    """The matched path with parameter values replaced by their names; `unmatched` for
    anything that matched no route, so label cardinality stays bounded."""
    if scope.get("route") is None:
        return "unmatched"
    template = scope["path"]
    for name, value in (scope.get("path_params") or {}).items():
        template = template.replace(str(value), "{" + name + "}")
    return template


class ObservabilityMiddleware:
    """Correlation id in and out, plus one structured log line and metrics per request.
    Only the route template is recorded (never raw paths, queries or bodies)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(CORRELATION_HEADER.encode(), b"").decode()
        correlation_id = (
            incoming if SAFE_CORRELATION_ID.fullmatch(incoming) else new_correlation_id()
        )
        started = time.perf_counter()
        status = 500

        async def send_with_header(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-correlation-id", correlation_id.encode()),
                ]
            await send(message)

        with bind(correlation_id=correlation_id):
            try:
                await self.app(scope, receive, send_with_header)
            finally:
                route = route_template(scope)
                elapsed = time.perf_counter() - started
                HTTP_REQUESTS.inc(method=scope["method"], route=route, status=str(status))
                HTTP_LATENCY.observe(elapsed, method=scope["method"], route=route)
                log(
                    "http.request",
                    method=scope["method"],
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                )
