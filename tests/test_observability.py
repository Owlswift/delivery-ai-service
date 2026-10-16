import io
import json
import logging

import pytest

from app.config import Settings, get_settings
from app.observability import metrics
from app.observability.audit import audit
from app.observability.context import bind
from app.observability.logs import configure_logging, log
from app.observability.redaction import REDACTED, redact
from app.observability.tracing import span
from tests.executor import Executor
from tests.signing import signed
from tests.test_operations import H, booking_of, new_session, say
from tests.test_quoting import blob

PHONE_DIGITS = ("08031234567", "8030000000", "2348030000000")


@pytest.fixture()
def logs():
    stream = io.StringIO()
    configure_logging("DEBUG", True, stream)
    metrics.REGISTRY.reset()
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    configure_logging("INFO", True)


# ---- redaction ----------------------------------------------------------------------
def test_sensitive_keys_are_removed_and_safe_identifiers_kept() -> None:
    cleaned = redact(
        {
            "recipient_phone": "08031234567",
            "pickup_address": "12 King Street",
            "Authorization": "Bearer abc",
            "api_key": "k",
            "idempotency_key": "idem-1",
            "operation_id": "op-1",
            "content": "hi",
            "nested": {"sender_phone": "+2348030000000", "status": "ok"},
            "items": [{"signature": "s"}],
        }
    )
    assert cleaned["recipient_phone"] == cleaned["pickup_address"] == cleaned["api_key"] == REDACTED
    assert cleaned["Authorization"] == cleaned["content"] == REDACTED
    assert cleaned["idempotency_key"] == "idem-1" and cleaned["operation_id"] == "op-1"
    assert cleaned["nested"] == {"sender_phone": REDACTED, "status": "ok"}
    assert cleaned["items"] == [{"signature": REDACTED}]


def test_free_text_is_masked_and_bounded() -> None:
    cleaned = redact({"note": "call +234 803 123 4567 or 08031234567 with Bearer abcdef123456"})
    assert "803" not in cleaned["note"] and "[phone]" in cleaned["note"]
    assert "abcdef123456" not in cleaned["note"]
    assert len(redact({"note": "x" * 5000})["note"]) < 400
    assert redact({"obj": object()})["obj"].startswith("<object")


# ---- logs ---------------------------------------------------------------------------
def test_log_lines_are_json_with_context_and_redacted_fields(logs) -> None:
    with bind(correlation_id="corr-12345", provider_id="acme"):
        log("something.happened", count=3, recipient_phone="08031234567", route="/v1/x")
    line = logs()[-1]
    assert line["event"] == "something.happened" and line["level"] == "INFO"
    assert line["correlation_id"] == "corr-12345" and line["provider_id"] == "acme"
    assert line["count"] == 3 and line["route"] == "/v1/x"
    assert line["recipient_phone"] == REDACTED and "ts" in line


def test_exceptions_log_type_and_masked_message_never_a_stack(logs) -> None:
    try:
        raise ValueError("bad number 08031234567")
    except ValueError:
        logging.getLogger("app").error("boom", exc_info=True)
    line = logs()[-1]
    assert line["error"].startswith("ValueError") and "08031234567" not in json.dumps(line)
    assert "Traceback" not in json.dumps(line)


def test_configure_logging_is_idempotent(logs) -> None:
    configure_logging("INFO", True, io.StringIO())
    configure_logging("INFO", True, io.StringIO())
    assert (
        len(
            [
                h
                for h in logging.getLogger("app").handlers
                if getattr(h, "_app_observability_handler", False)
            ]
        )
        == 1
    )


def test_audit_and_span_record_metrics_and_logs(logs) -> None:
    audit("thing.done", outcome="ok", pickup_address="12 King Street")
    with span("unit.step", n=1):
        pass
    with pytest.raises(RuntimeError), span("unit.fail"):
        raise RuntimeError("x")
    lines = logs()
    assert next(l for l in lines if l["event"] == "thing.done")["pickup_address"] == REDACTED
    assert metrics.AUDIT_EVENTS.value(event="thing.done") == 1
    assert metrics.SPAN_LATENCY.count(name="unit.step") == 1
    assert [l["outcome"] for l in lines if l["event"] == "span"] == ["ok", "error"]


# ---- metrics ------------------------------------------------------------------------
def test_prometheus_rendering_and_label_escaping() -> None:
    metrics.REGISTRY.reset()
    metrics.EVENTS.inc(type='a"b', result="x\ny")
    metrics.OPERATION_LATENCY.observe(0.7, type="t")
    text = metrics.REGISTRY.render()
    assert 'provider_events_total{result="x\\ny",type="a\\"b"} 1' in text
    assert 'provider_operation_latency_seconds_bucket{type="t",le="1"} 1' in text
    assert 'provider_operation_latency_seconds_bucket{type="t",le="0.5"} 0' in text
    assert 'provider_operation_latency_seconds_count{type="t"} 1' in text
    assert "# TYPE provider_events_total counter" in text


# ---- secrets in configuration -------------------------------------------------------
def test_settings_never_print_their_secrets() -> None:
    s = Settings(
        api_keys="k1:acme",
        provider_secrets="acme:sup3rsecret",
        client_token_secret="tok3nsecret",
        google_maps_api_key="g00gle",
        metrics_token="m3trics",
    )
    shown = repr(s) + str(s.model_dump())
    for secret in ("k1:acme", "sup3rsecret", "tok3nsecret", "g00gle", "m3trics"):
        assert secret not in shown
    assert s.secret_map() == {"acme": "sup3rsecret"} and s.key_map() == {"k1": "acme"}


# ---- HTTP: correlation, request metrics, /metrics, and no personal data in logs ------
def test_correlation_id_is_echoed_or_generated_and_unsafe_ones_replaced(api, logs) -> None:
    ok = api.get("/health", headers={"X-Correlation-Id": "trace-abc12345"})
    assert ok.headers["x-correlation-id"] == "trace-abc12345"
    generated = api.get("/health")
    assert len(generated.headers["x-correlation-id"]) == 32
    bad = api.get("/health", headers={"X-Correlation-Id": 'x y"z'})
    assert (
        bad.headers["x-correlation-id"] not in ('x y"z', "")
        and len(bad.headers["x-correlation-id"]) == 32
    )
    request_lines = [l for l in logs() if l["event"] == "http.request"]
    assert (
        request_lines[0]["correlation_id"] == "trace-abc12345" and request_lines[0]["status"] == 200
    )


def test_requests_are_measured_by_route_template_not_raw_path(api, logs) -> None:
    sid = new_session(api)
    api.get(f"/v1/sessions/{sid}", headers=H)
    assert (
        metrics.HTTP_REQUESTS.value(method="GET", route="/v1/sessions/{session_id}", status="200")
        == 1
    )
    text = metrics.REGISTRY.render()
    assert sid not in text
    assert 'route="unmatched"' not in text or api.get("/nope").status_code == 404


def test_metrics_endpoint_is_off_until_a_token_is_configured(api, monkeypatch) -> None:
    assert api.get("/metrics").status_code == 404
    monkeypatch.setenv("METRICS_TOKEN", "m3trics")
    get_settings.cache_clear()
    assert api.get("/metrics").status_code == 401
    assert api.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = api.get("/metrics", headers={"Authorization": "Bearer m3trics"})
    assert ok.status_code == 200 and "http_requests_total" in ok.text


def test_no_personal_data_or_secrets_reach_the_logs(api, logs) -> None:
    ex = Executor(api)
    sid = new_session(api)
    say(api, sid, blob())
    ex.run()
    say(api, sid, "yes")
    ex.run()
    token = api.post(f"/v1/sessions/{sid}/client-token", headers=H).json()["token"]
    api.get(f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {token}"})
    signed(api, "POST", "/v1/operations/lease", {"limit": 1}, secret="wrong")  # a failed signature
    text = "\n".join(json.dumps(l) for l in logs())
    for forbidden in (
        *PHONE_DIGITS,
        "King Street",
        "Marina Road",
        "Ada Obi",
        "documents",
        token,
        "acme-secret",
        "dev-key",
        "wrong",
    ):
        assert forbidden not in text, f"{forbidden!r} leaked into the logs"
    assert '"event":"booking.transition"' in text.replace(" ", "") or "booking.transition" in text


def test_workflow_and_operation_activity_is_audited_and_counted(api, logs) -> None:
    ex = Executor(api)
    sid = new_session(api)
    say(api, sid, blob())
    ex.run()
    say(api, sid, "yes")
    ex.run()
    assert booking_of(api, sid)["status"] == "booking_created"
    lines = logs()
    transitions = [l for l in lines if l["event"] == "booking.transition"]
    assert [t["to_status"] for t in transitions][-1] == "booking_created"
    assert all("trigger" in t for t in transitions)
    assert metrics.TRANSITIONS.total() == len(transitions)
    assert metrics.OPERATIONS.value(type="create_booking", event="enqueued") == 1
    assert metrics.OPERATIONS.value(type="create_booking", event="leased") == 1
    assert metrics.OPERATIONS.value(type="create_booking", event="succeeded") == 1
    assert metrics.OPERATION_LATENCY.count(type="create_booking") == 1
    assert any(l["event"] == "operation.result" and l["type"] == "create_booking" for l in lines)


def test_auth_failures_and_event_outcomes_are_counted(api, logs) -> None:
    api.post("/v1/operations/lease", json={}, headers=H)
    signed(api, "POST", "/v1/operations/lease", {"limit": 1}, tamper=True)
    api.get(f"/v1/sessions/{new_session(api)}")
    assert metrics.AUTH_FAILURES.value(kind="signature_missing_or_unknown_provider") == 1
    assert metrics.AUTH_FAILURES.value(kind="signature_mismatch") == 1
    assert metrics.AUTH_FAILURES.value(kind="api_key_missing") == 1
    assert any(l["event"] == "auth.failed" for l in logs())
