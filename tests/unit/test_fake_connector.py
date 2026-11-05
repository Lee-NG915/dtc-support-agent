from __future__ import annotations

import hashlib

import pytest

from app.connectors.errors import (
    ConnectorConflict,
    ConnectorGone,
    ConnectorNotFound,
    ConnectorUnavailable,
    ConnectorValidationError,
)
from app.connectors.fake import FakeConnector
from app.connectors.models import (
    CreateServiceTicketInput,
    TicketLookupFound,
    TicketLookupInFlight,
    TicketLookupNotVisible,
)


def _payload(**overrides: str) -> CreateServiceTicketInput:
    values = {
        "request_id": "asr_000000000042",
        "action": "reship",
        "reply": "We will send a replacement for the damaged synthetic item.",
        "draft_version": "drv_000000000003",
        "approval_id": "appr_dtc_00000042",
        "policy_version": "policy_after_sales_2026_01",
        "order_version": "ov_000000000007",
    }
    values.update(overrides)
    return CreateServiceTicketInput.model_validate(values)


@pytest.mark.asyncio
async def test_fake_reads_match_synthetic_contract_shapes() -> None:
    connector = FakeConnector()
    try:
        order = await connector.get_service_order(
            market="au",
            order_id="ord_au_00000042",
        )
        assert order.market == "au"
        assert order.currency == "AUD"
        assert order.recipient_summary.masked_name == "A*** B***"

        shipment = await connector.get_service_order_shipment(
            market="au",
            order_id="ord_au_00000042",
        )
        assert shipment.events[-1].status == "delivered"

        request = await connector.get_service_request(
            market="au",
            request_id="asr_000000000042",
        )
        assert request.status == "pending"
        assert request.ticket_id is None

        feed = await connector.list_service_requests(market="au")
        assert feed.items[0].request_id == request.request_id
        assert feed.next_cursor
        assert feed.has_more is False
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_fake_attachment_is_the_synthetic_png_and_expired_is_gone() -> None:
    connector = FakeConnector()
    try:
        content = await connector.get_attachment_content(
            market="au",
            attachment_id="att_0000000000a1",
        )
        assert content.media_type == "image/png"
        assert content.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert hashlib.sha256(content.content).hexdigest() == (
            "431ced6916a2a21a156e38701afe55bbd7f88969fbbfc56d7fe099d47f265460"
        )

        with pytest.raises(ConnectorGone) as expired:
            await connector.get_attachment_content(
                market="au",
                attachment_id="att_0000000000e1",
            )
        assert expired.value.code == "ATTACHMENT_EXPIRED"

        with pytest.raises(ConnectorNotFound):
            await connector.get_attachment_content(
                market="au",
                attachment_id="att_0000000000ff",
            )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_fake_replays_same_fingerprint_and_conflicts_on_different_fingerprint() -> None:
    connector = FakeConnector()
    try:
        first = await connector.create_ticket(
            market="au",
            idempotency_key="ticket-key-1",
            payload=_payload(),
        )
        assert first.replayed is False
        assert first.ticket.ticket_id == "tic_000000000042"

        replay = await connector.create_ticket(
            market="au",
            idempotency_key="ticket-key-1",
            payload=_payload(),
        )
        assert replay.replayed is True
        assert replay.ticket.ticket_id == first.ticket.ticket_id

        with pytest.raises(ConnectorConflict) as conflict:
            await connector.create_ticket(
                market="au",
                idempotency_key="ticket-key-1",
                payload=_payload(reply="A different synthetic reply."),
            )
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

        with pytest.raises(ConnectorConflict) as request_conflict:
            await connector.create_ticket(
                market="au",
                idempotency_key="ticket-key-2",
                payload=_payload(reply="A different synthetic reply."),
            )
        assert request_conflict.value.code == "REQUEST_STATE_CONFLICT"
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_fake_guards_order_version_and_tracks_request_state() -> None:
    connector = FakeConnector()
    try:
        with pytest.raises(ConnectorConflict) as version_conflict:
            await connector.create_ticket(
                market="au",
                idempotency_key="ticket-key-version",
                payload=_payload(order_version="ov_000000000999"),
            )
        assert version_conflict.value.code == "ORDER_VERSION_CONFLICT"

        created = await connector.create_ticket(
            market="au",
            idempotency_key="ticket-key-state",
            payload=_payload(),
        )
        assert created.ticket.receipt.request_status == "ticket_created"
        request = await connector.get_service_request(
            market="au",
            request_id="asr_000000000042",
        )
        assert request.status == "ticket_created"
        assert request.ticket_id == created.ticket.ticket_id
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_fake_idempotency_lookup_distinguishes_found_in_flight_and_not_visible() -> None:
    connector = FakeConnector()
    try:
        not_visible = await connector.get_ticket_by_idempotency_key(
            market="au",
            idempotency_key="missing-key",
        )
        assert isinstance(not_visible, TicketLookupNotVisible)

        connector.set_in_flight("au", "slow-key")
        in_flight = await connector.get_ticket_by_idempotency_key(
            market="au",
            idempotency_key="slow-key",
        )
        assert isinstance(in_flight, TicketLookupInFlight)
        assert in_flight.retry_after_ms == 250

        created = await connector.create_ticket(
            market="au",
            idempotency_key="found-key",
            payload=_payload(),
        )
        found = await connector.get_ticket_by_idempotency_key(
            market="au",
            idempotency_key="found-key",
        )
        assert isinstance(found, TicketLookupFound)
        assert found.ticket.ticket_id == created.ticket.ticket_id
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_fake_paginates_with_market_bound_cursors_and_rejects_bad_cursor() -> None:
    connector = FakeConnector()
    try:
        first = await connector.list_service_requests(market="au", limit=1)
        assert len(first.items) == 1
        assert first.next_cursor

        with pytest.raises(ConnectorValidationError) as error:
            await connector.list_service_requests(market="au", cursor="not-a-valid-cursor")
        assert error.value.code == "INVALID_CURSOR"

        with pytest.raises(ConnectorValidationError):
            await connector.list_service_requests(market="au", cursor="%")
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_fake_controllable_failure_is_consumed_once() -> None:
    connector = FakeConnector()
    failure = ConnectorUnavailable(create_operation=False)
    connector.queue_failure("get_service_order", failure)
    try:
        with pytest.raises(ConnectorUnavailable):
            await connector.get_service_order(market="au", order_id="ord_au_00000042")
        order = await connector.get_service_order(market="au", order_id="ord_au_00000042")
        assert order.order_id == "ord_au_00000042"
    finally:
        await connector.aclose()
        assert connector.closed is True
