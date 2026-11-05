from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config.settings import Settings
from app.connectors.epig import EpigConnector
from app.connectors.errors import (
    ConnectorConfigurationError,
    ConnectorPermissionError,
    ConnectorProtocolError,
    ConnectorUnavailable,
    ConnectorValidationError,
)
from app.connectors.factory import build_business_connector
from app.connectors.models import (
    CreateServiceTicketInput,
    TicketLookupFound,
    TicketLookupInFlight,
    TicketLookupNotVisible,
)

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "contracts" / "epig-after-sales" / "v1"
HEADERS = {"X-After-Sales-Contract-Version": "1"}


def _fixture(path: str) -> Any:
    return json.loads((BUNDLE / "fixtures" / path).read_text(encoding="utf-8"))


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "testing",
        "business_connector": "epig",
        "epig_base_url": "http://127.0.0.1:18080",
        "epig_service_token": "synthetic-service-token",
        "epig_allowed_markets": "au,sg",
        "epig_timeout_seconds": 1.0,
        "epig_max_connections": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _payload(**overrides: str) -> CreateServiceTicketInput:
    values = _fixture("requests/create_service_ticket.valid.json")
    values.update(overrides)
    return CreateServiceTicketInput.model_validate(values)


def _connector(
    handler: Any,
    *,
    settings: Settings | None = None,
    sleep: Any = None,
) -> EpigConnector:
    return EpigConnector(
        settings or _settings(),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )


@pytest.mark.asyncio
async def test_service_order_uses_exact_contract_path_and_headers() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers=HEADERS,
            json=_fixture("responses/get_service_order.ok.json"),
        )

    connector = _connector(handler)
    try:
        order = await connector.get_service_order(
            market="au",
            order_id="ord_au_00000042",
        )
    finally:
        await connector.aclose()

    assert order.order_id == "ord_au_00000042"
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "GET"
    assert str(request.url) == (
        "http://127.0.0.1:18080/api/au/after-sales/service/orders/ord_au_00000042"
    )
    assert request.headers["authorization"] == "Bearer synthetic-service-token"
    assert request.headers["x-after-sales-contract-version"] == "1"


@pytest.mark.asyncio
async def test_feed_sends_cursor_and_limit_query_parameters() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers=HEADERS,
            json=_fixture("responses/list_service_requests.ok.json"),
        )

    connector = _connector(handler)
    try:
        feed = await connector.list_service_requests(
            market="au",
            cursor="Y3Vyc29yX2F1XzAwMDAwMDAwMDAwMDAwMDAwMDA0Mg",
            limit=25,
        )
    finally:
        await connector.aclose()

    assert feed.next_cursor
    assert calls[0].url.params["cursor"] == "Y3Vyc29yX2F1XzAwMDAwMDAwMDAwMDAwMDAwMDA0Mg"
    assert calls[0].url.params["limit"] == "25"


@pytest.mark.asyncio
async def test_create_ticket_posts_exact_body_and_preserves_replay_marker() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            201,
            headers=HEADERS,
            json=_fixture("responses/create_service_ticket.created.json"),
        )

    payload = _payload()
    connector = _connector(handler)
    try:
        result = await connector.create_ticket(
            market="au",
            idempotency_key="ticket-key-1",
            payload=payload,
        )
    finally:
        await connector.aclose()

    assert result.replayed is False
    assert result.ticket.ticket_id == "tic_000000000042"
    request = calls[0]
    assert request.method == "POST"
    assert request.headers["idempotency-key"] == "ticket-key-1"
    assert json.loads(request.content) == payload.model_dump(mode="json")


@pytest.mark.asyncio
async def test_replay_requires_the_contract_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=HEADERS,
            json=_fixture("responses/create_service_ticket.replayed.json"),
        )

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorProtocolError):
            await connector.create_ticket(
                market="au",
                idempotency_key="ticket-key-1",
                payload=_payload(),
            )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_replay_with_marker_is_reported_as_replayed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={**HEADERS, "Idempotency-Replayed": "true"},
            json=_fixture("responses/create_service_ticket.replayed.json"),
        )

    connector = _connector(handler)
    try:
        result = await connector.create_ticket(
            market="au",
            idempotency_key="ticket-key-1",
            payload=_payload(),
        )
    finally:
        await connector.aclose()
    assert result.replayed is True


@pytest.mark.asyncio
async def test_writes_are_never_retried_after_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorUnavailable) as error:
            await connector.create_ticket(
                market="au",
                idempotency_key="ticket-key-timeout",
                payload=_payload(),
            )
    finally:
        await connector.aclose()

    assert calls == 1
    assert error.value.outcome_unknown is True
    assert error.value.retryable is False
    assert error.value.recovery_action == "LOOKUP_FIRST"


@pytest.mark.asyncio
async def test_reads_retry_once_with_a_bounded_delay() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("synthetic timeout", request=request)
        return httpx.Response(
            200,
            headers=HEADERS,
            json=_fixture("responses/get_service_order.ok.json"),
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    connector = _connector(handler, sleep=fake_sleep)
    try:
        order = await connector.get_service_order(market="au", order_id="ord_au_00000042")
    finally:
        await connector.aclose()

    assert order.order_id == "ord_au_00000042"
    assert calls == 2
    assert sleeps == [0.1]


@pytest.mark.asyncio
async def test_redirect_is_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={**HEADERS, "Location": "https://elsewhere.invalid/"},
        )

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorProtocolError):
            await connector.get_service_order(market="au", order_id="ord_au_00000042")
    finally:
        await connector.aclose()
    assert calls == 1


@pytest.mark.asyncio
async def test_error_body_is_validated_and_never_echoed() -> None:
    leaked = {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "The request is invalid.",
            "request_id": "req_err_000000000099",
            "retryable": False,
            "recovery_action": "NONE",
            "details": {
                "field": "description",
                "full_address": "100 Synthetic Avenue, Sydney NSW 2000",
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, headers=HEADERS, json=leaked)

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorProtocolError) as error:
            await connector.get_service_order(market="au", order_id="ord_au_00000042")
    finally:
        await connector.aclose()
    assert "Synthetic Avenue" not in error.value.message


@pytest.mark.asyncio
async def test_schema_mismatch_is_classified_without_raw_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=HEADERS,
            json={"order_id": "ord_au_00000042", "secret_storage_url": "https://invalid/"},
        )

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorProtocolError) as error:
            await connector.get_service_order(market="au", order_id="ord_au_00000042")
    finally:
        await connector.aclose()
    assert "invalid/" not in error.value.message


@pytest.mark.asyncio
async def test_idempotency_lookup_distinguishes_202_and_temporary_404() -> None:
    responses = [
        httpx.Response(
            202,
            headers=HEADERS,
            json=_fixture("responses/get_service_ticket_by_idempotency_key.in_flight.json"),
        ),
        httpx.Response(
            404,
            headers=HEADERS,
            json=_fixture("responses/get_service_ticket_by_idempotency_key.not_found.json"),
        ),
        httpx.Response(
            200,
            headers=HEADERS,
            json=_fixture("responses/get_service_ticket_by_idempotency_key.ok.json"),
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    connector = _connector(handler)
    try:
        pending = await connector.get_ticket_by_idempotency_key(
            market="au",
            idempotency_key="ticket-key-1",
        )
        assert isinstance(pending, TicketLookupInFlight)
        assert pending.retry_after_ms == 250

        not_visible = await connector.get_ticket_by_idempotency_key(
            market="au",
            idempotency_key="ticket-key-1",
        )
        assert isinstance(not_visible, TicketLookupNotVisible)

        result = await connector.get_ticket_by_idempotency_key(
            market="au",
            idempotency_key="ticket-key-1",
        )
        assert isinstance(result, TicketLookupFound)
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_attachment_media_type_and_size_are_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={**HEADERS, "Content-Type": "text/html"},
            content=b"<html>not an image</html>",
        )

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorValidationError) as error:
            await connector.get_attachment_content(
                market="au",
                attachment_id="att_0000000000a1",
            )
    finally:
        await connector.aclose()
    assert error.value.code == "UNSUPPORTED_MEDIA_TYPE"


@pytest.mark.asyncio
async def test_attachment_over_five_mib_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={**HEADERS, "Content-Type": "image/png"},
            content=b"\x89PNG\r\n\x1a\n" + (b"\0" * (5 * 1024 * 1024)),
        )

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorValidationError) as error:
            await connector.get_attachment_content(
                market="au",
                attachment_id="att_0000000000a1",
            )
    finally:
        await connector.aclose()
    assert error.value.code == "ATTACHMENT_TOO_LARGE"


@pytest.mark.asyncio
async def test_market_must_be_locally_allowed_before_any_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers=HEADERS, json={})

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorPermissionError):
            await connector.get_service_order(market="us", order_id="ord_us_00000042")
    finally:
        await connector.aclose()
    assert calls == 0


def test_epig_configuration_fails_closed_and_never_falls_back() -> None:
    with pytest.raises(ConnectorConfigurationError):
        build_business_connector(_settings(epig_service_token=""))
    with pytest.raises(ConnectorConfigurationError):
        build_business_connector(_settings(epig_allowed_markets=""))
    with pytest.raises(ConnectorConfigurationError):
        build_business_connector(_settings(epig_allowed_markets="au,xx"))
    with pytest.raises(ConnectorConfigurationError):
        build_business_connector(_settings(app_env="production"))


def test_plain_http_is_only_allowed_for_explicit_loopback() -> None:
    with pytest.raises(ConnectorConfigurationError):
        EpigConnector(_settings(epig_base_url="http://epig.example.com"))
    local = EpigConnector(_settings(epig_base_url="http://localhost:18080"))
    remote = EpigConnector(_settings(epig_base_url="https://epig.example.com"))
    asyncio.run(local.aclose())
    asyncio.run(remote.aclose())


# ------------------------------------------------------- business identity


def _response(path: str, **mutations: Any) -> dict[str, Any]:
    payload = copy.deepcopy(_fixture(path))
    payload.update(mutations)
    return payload


async def _expect_protocol_error(
    connector: EpigConnector, operation: Any, **kwargs: Any
) -> ConnectorProtocolError:
    with pytest.raises(ConnectorProtocolError) as error:
        await operation(**kwargs)
    return error.value


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["market", "order_id"])
async def test_order_identity_must_match_the_request(field: str) -> None:
    payload = _response("responses/get_service_order.ok.json")
    payload[field] = "sg" if field == "market" else "ord_au_00000043"

    connector = _connector(lambda request: httpx.Response(200, headers=HEADERS, json=payload))
    try:
        await _expect_protocol_error(
            connector,
            connector.get_service_order,
            market="au",
            order_id="ord_au_00000042",
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_shipment_order_id_must_match_the_path() -> None:
    payload = _response("responses/get_service_order_shipment.ok.json")
    payload["order_id"] = "ord_au_00000043"

    connector = _connector(lambda request: httpx.Response(200, headers=HEADERS, json=payload))
    try:
        await _expect_protocol_error(
            connector,
            connector.get_service_order_shipment,
            market="au",
            order_id="ord_au_00000042",
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["market", "request_id"])
async def test_request_identity_must_match_the_request(field: str) -> None:
    payload = _response("responses/get_service_request.ok.json")
    payload[field] = "sg" if field == "market" else "asr_000000000043"

    connector = _connector(lambda request: httpx.Response(200, headers=HEADERS, json=payload))
    try:
        await _expect_protocol_error(
            connector,
            connector.get_service_request,
            market="au",
            request_id="asr_000000000042",
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_feed_rejects_an_item_from_another_market() -> None:
    payload = _response("responses/list_service_requests.ok.json")
    payload["items"][0]["market"] = "sg"

    connector = _connector(lambda request: httpx.Response(200, headers=HEADERS, json=payload))
    try:
        await _expect_protocol_error(
            connector,
            connector.list_service_requests,
            market="au",
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["market", "ticket_id"])
async def test_ticket_read_identity_must_match_the_request(field: str) -> None:
    payload = _response("responses/get_service_ticket.ok.json")
    payload[field] = "sg" if field == "market" else "tic_000000000043"

    connector = _connector(lambda request: httpx.Response(200, headers=HEADERS, json=payload))
    try:
        await _expect_protocol_error(
            connector,
            connector.get_service_ticket,
            market="au",
            ticket_id="tic_000000000042",
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticket_id", "tic_000000000043"),
        ("request_id", "asr_000000000043"),
        ("action", "refund"),
    ],
)
async def test_ticket_receipt_must_match_the_outer_ticket(field: str, value: str) -> None:
    payload = _response("responses/get_service_ticket.ok.json")
    payload["receipt"][field] = value

    connector = _connector(lambda request: httpx.Response(200, headers=HEADERS, json=payload))
    try:
        error = await _expect_protocol_error(
            connector,
            connector.get_service_ticket,
            market="au",
            ticket_id="tic_000000000042",
        )
        assert "tic_000000000043" not in error.message
    finally:
        await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "asr_000000000043"),
        ("action", "refund"),
        ("reply", "A different but schema-valid synthetic reply."),
        ("draft_version", "drv_000000000004"),
        ("approval_id", "appr_dtc_00000043"),
        ("policy_version", "policy_after_sales_2026_02"),
        ("order_version", "ov_000000000008"),
    ],
)
async def test_create_ticket_must_echo_every_approved_payload_field(field: str, value: str) -> None:
    payload = _response("responses/create_service_ticket.created.json")
    payload[field] = value
    if field in {"request_id", "action"}:
        # Keep the receipt internally consistent so only the payload mismatch
        # can be responsible for the protocol error.
        payload["receipt"][field] = value

    connector = _connector(lambda request: httpx.Response(201, headers=HEADERS, json=payload))
    try:
        await _expect_protocol_error(
            connector,
            connector.create_ticket,
            market="au",
            idempotency_key="ticket-key-1",
            payload=_payload(),
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_create_ticket_rejects_a_foreign_market_or_broken_receipt() -> None:
    for mutate in (
        lambda payload: payload.update({"market": "sg"}),
        lambda payload: payload["receipt"].update({"ticket_id": "tic_000000000043"}),
    ):
        payload = _response("responses/create_service_ticket.created.json")
        mutate(payload)

        connector = _connector(
            lambda request, body=payload: httpx.Response(201, headers=HEADERS, json=body)
        )
        try:
            await _expect_protocol_error(
                connector,
                connector.create_ticket,
                market="au",
                idempotency_key="ticket-key-1",
                payload=_payload(),
            )
        finally:
            await connector.aclose()


# ------------------------------------------- create recovery classification


def _created(**mutations: Any) -> dict[str, Any]:
    return _response("responses/create_service_ticket.created.json", **mutations)


def _payload_identity_mismatch() -> dict[str, Any]:
    payload = _created()
    payload["request_id"] = "asr_000000000043"
    payload["receipt"]["request_id"] = "asr_000000000043"
    return payload


def _receipt_identity_mismatch() -> dict[str, Any]:
    payload = _created()
    payload["receipt"]["ticket_id"] = "tic_000000000043"
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_factory",
    [
        pytest.param(
            lambda: httpx.Response(
                201,
                json=_fixture("responses/create_service_ticket.created.json"),
            ),
            id="missing-contract-version",
        ),
        pytest.param(
            lambda: httpx.Response(
                201,
                headers={"X-After-Sales-Contract-Version": "2"},
                json=_fixture("responses/create_service_ticket.created.json"),
            ),
            id="unsupported-contract-version",
        ),
        pytest.param(
            lambda: httpx.Response(
                200,
                headers=HEADERS,
                json=_fixture("responses/create_service_ticket.replayed.json"),
            ),
            id="replay-marker-missing",
        ),
        pytest.param(
            lambda: httpx.Response(201, headers=HEADERS, content=b'{"ticket_id":'),
            id="malformed-json",
        ),
        pytest.param(
            lambda: httpx.Response(
                201,
                headers=HEADERS,
                json={"ticket_id": "tic_000000000042"},
            ),
            id="schema-mismatch",
        ),
        pytest.param(
            lambda: httpx.Response(201, headers=HEADERS, json=_payload_identity_mismatch()),
            id="payload-identity-mismatch",
        ),
        pytest.param(
            lambda: httpx.Response(201, headers=HEADERS, json=_receipt_identity_mismatch()),
            id="receipt-identity-mismatch",
        ),
        pytest.param(
            lambda: httpx.Response(400, headers=HEADERS, content=b"not-json"),
            id="malformed-error-body",
        ),
        pytest.param(
            lambda: httpx.Response(
                500,
                headers=HEADERS,
                json={
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "Synthetic validation error.",
                        "request_id": "req_err_000000000099",
                        "retryable": False,
                        "recovery_action": "NONE",
                    }
                },
            ),
            id="illegal-error-status-combination",
        ),
    ],
)
async def test_create_protocol_violation_after_post_requires_lookup(
    response_factory: Any,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response_factory()

    connector = _connector(handler)
    idempotency_key = "ticket-key-uncertain"
    try:
        with pytest.raises(ConnectorProtocolError) as error:
            await connector.create_ticket(
                market="au",
                idempotency_key=idempotency_key,
                payload=_payload(),
            )
    finally:
        await connector.aclose()

    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].headers["idempotency-key"] == idempotency_key
    classified = error.value
    assert classified.recovery_action == "LOOKUP_FIRST"
    assert classified.outcome_unknown is True
    assert classified.contract_violation is True
    assert classified.retryable is False


@pytest.mark.asyncio
async def test_read_protocol_error_keeps_ordinary_classification() -> None:
    payload = _response("responses/get_service_order.ok.json")
    payload["order_id"] = "ord_au_00000043"

    connector = _connector(lambda request: httpx.Response(200, headers=HEADERS, json=payload))
    try:
        error = await _expect_protocol_error(
            connector,
            connector.get_service_order,
            market="au",
            order_id="ord_au_00000042",
        )
    finally:
        await connector.aclose()

    assert error.recovery_action == "NONE"
    assert error.outcome_unknown is False
    assert error.contract_violation is True


@pytest.mark.asyncio
async def test_input_validation_failure_is_not_an_unknown_write() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            201,
            headers=HEADERS,
            json=_fixture("responses/create_service_ticket.created.json"),
        )

    connector = _connector(handler)
    try:
        with pytest.raises(ConnectorValidationError) as error:
            await connector.create_ticket(
                market="au",
                idempotency_key="invalid key",
                payload=_payload(),
            )
    finally:
        await connector.aclose()

    assert calls == 0
    assert error.value.recovery_action == "NONE"
    assert error.value.outcome_unknown is False
    assert error.value.contract_violation is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"market": "sg"}),
        lambda payload: payload["receipt"].update({"request_id": "asr_000000000043"}),
        lambda payload: payload["receipt"].update({"action": "reject"}),
    ],
)
async def test_idempotency_lookup_ticket_is_internally_consistent(mutate: Any) -> None:
    payload = _response("responses/get_service_ticket_by_idempotency_key.ok.json")
    mutate(payload)

    connector = _connector(lambda request: httpx.Response(200, headers=HEADERS, json=payload))
    try:
        await _expect_protocol_error(
            connector,
            connector.get_ticket_by_idempotency_key,
            market="au",
            idempotency_key="ticket-key-1",
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", "INTERNAL_ERROR"),
        ("recovery_action", "LOOKUP_FIRST"),
    ],
)
async def test_temporary_404_lookup_requires_the_documented_error_identity(
    field: str, value: str
) -> None:
    payload = _response("responses/get_service_ticket_by_idempotency_key.not_found.json")
    payload["error"][field] = value

    connector = _connector(lambda request: httpx.Response(404, headers=HEADERS, json=payload))
    try:
        error = await _expect_protocol_error(
            connector,
            connector.get_ticket_by_idempotency_key,
            market="au",
            idempotency_key="ticket-key-1",
        )
        assert error.contract_violation is True
        assert "No ticket creation record" not in error.message
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_malformed_lookup_error_body_is_safe_protocol_error() -> None:
    marker = "DO-NOT-ECHO-8f2a"
    payload = {
        "error": {
            "code": "NOT_FOUND",
            "message": f"{marker} synthetic leak",
            "request_id": "req_err_000000000015",
            "retryable": False,
            "recovery_action": "RETRY_CREATE_WITH_SAME_KEY",
            "unexpected": marker,
        }
    }

    connector = _connector(lambda request: httpx.Response(404, headers=HEADERS, json=payload))
    try:
        error = await _expect_protocol_error(
            connector,
            connector.get_ticket_by_idempotency_key,
            market="au",
            idempotency_key="ticket-key-1",
        )
        assert error.contract_violation is True
        assert marker not in error.message
    finally:
        await connector.aclose()
