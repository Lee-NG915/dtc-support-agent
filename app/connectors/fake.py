"""Deterministic synthetic after-sales connector for tests and local demos."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

from app.connectors.errors import (
    ConnectorConflict,
    ConnectorGone,
    ConnectorNotFound,
    ConnectorPermissionError,
    ConnectorValidationError,
)
from app.connectors.models import (
    Attachment,
    AttachmentContent,
    CreateServiceTicketInput,
    RecipientSummary,
    ServiceAfterSalesRequest,
    ServiceOrder,
    ServiceOrderItem,
    ServiceRequestFeed,
    ServiceShipment,
    ServiceTicket,
    ShipmentEvent,
    TicketCreationResult,
    TicketLookupFound,
    TicketLookupInFlight,
    TicketLookupNotVisible,
    TicketLookupResult,
    TicketReceipt,
    dump_wire_model,
    validate_idempotency_key,
    validate_opaque_id,
)
from app.schemas.after_sales import AFTER_SALES_MARKETS, AfterSalesMarket

_SYNTHETIC_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAA"
    "SUVORK5CYII="
)
_REQUEST_ID = "asr_000000000042"
_ATTACHMENT_ID = "att_0000000000a1"
_EXPIRED_ATTACHMENT_ID = "att_0000000000e1"
_ORDER_VERSION = "ov_000000000007"
_SEQUENCE = "00000000000000000042"
_CREATED_AT = datetime(2026, 1, 15, 3, 4, 5, tzinfo=UTC)
_CREATED_ATTACHMENT_AT = datetime(2026, 1, 15, 3, 3, 30, tzinfo=UTC)
_ATTACHMENT_EXPIRES_AT = datetime(2030, 1, 22, 3, 3, 30, tzinfo=UTC)
_TICKET_AT = datetime(2026, 1, 15, 4, 0, 0, tzinfo=UTC)

_CURRENCY: dict[AfterSalesMarket, str] = {"au": "AUD", "sg": "SGD", "us": "USD"}
_COUNTRY: dict[AfterSalesMarket, Literal["AU", "SG", "US"]] = {
    "au": "AU",
    "sg": "SG",
    "us": "US",
}
_REGION: dict[AfterSalesMarket, str] = {"au": "NSW", "sg": "Central", "us": "CA"}


@dataclass
class _RequestState:
    request: ServiceAfterSalesRequest
    fingerprint: str | None = None
    ticket_id: str | None = None


@dataclass
class _FakeState:
    orders: dict[tuple[str, str], ServiceOrder] = field(default_factory=dict)
    shipments: dict[tuple[str, str], ServiceShipment] = field(default_factory=dict)
    requests: dict[tuple[str, str], _RequestState] = field(default_factory=dict)
    tickets: dict[tuple[str, str], ServiceTicket] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    in_flight: set[tuple[str, str]] = field(default_factory=set)
    failures: dict[str, list[Exception]] = field(default_factory=dict)


class FakeConnector:
    """Synthetic implementation with replay, conflict, version and failure controls."""

    def __init__(self) -> None:
        self._state = _FakeState()
        self._lock = asyncio.Lock()
        self._closed = False
        self._create_ticket_calls = 0
        for market in sorted(AFTER_SALES_MARKETS):
            self._add_market(market)

    def _add_market(self, market: AfterSalesMarket) -> None:
        order_id = f"ord_{market}_00000042"
        order = ServiceOrder(
            order_id=order_id,
            market=market,
            currency=_CURRENCY[market],
            items=[
                ServiceOrderItem(
                    order_item_id="oit_000000000042",
                    sku="SYNTH-MUG-001",
                    name="Synthetic ceramic mug",
                    quantity=1,
                    unit_price_minor=12900,
                    line_total_minor=12900,
                )
            ],
            subtotal_minor=12900,
            shipping_minor=895,
            tax_minor=0,
            discount_minor=0,
            total_minor=13795,
            status="fulfilled",
            created_at=datetime(2026, 1, 10, 2, 30, tzinfo=UTC),
            order_version=_ORDER_VERSION,
            recipient_summary=RecipientSummary(
                masked_name="A*** B***",
                country_code=_COUNTRY[market],
                region=_REGION[market],
                postal_code_masked="2***",
            ),
        )
        self._state.orders[(market, order_id)] = order
        self._state.shipments[(market, order_id)] = ServiceShipment(
            shipment_id="shp_000000000042",
            order_id=order_id,
            status="delivered",
            carrier="Synthetic Parcel Carrier",
            events=[
                ShipmentEvent(
                    event_id="she_000000000041",
                    status="in_transit",
                    description="The synthetic parcel departed the local depot.",
                    occurred_at=datetime(2026, 1, 13, 1, 15, tzinfo=UTC),
                    location_summary=_REGION[market],
                ),
                ShipmentEvent(
                    event_id="she_000000000042",
                    status="delivered",
                    description="The synthetic parcel was recorded as delivered.",
                    occurred_at=datetime(2026, 1, 14, 6, 45, tzinfo=UTC),
                    location_summary=_REGION[market],
                ),
            ],
        )
        attachment = Attachment(
            attachment_id=_ATTACHMENT_ID,
            file_name="synthetic-damage.png",
            media_type="image/png",
            size_bytes=len(_SYNTHETIC_PNG),
            status="active",
            created_at=_CREATED_ATTACHMENT_AT,
            expires_at=_ATTACHMENT_EXPIRES_AT,
        )
        request = ServiceAfterSalesRequest(
            request_id=_REQUEST_ID,
            sequence=_SEQUENCE,
            order_id=order_id,
            market=market,
            type="damaged",
            description=(
                "The synthetic parcel arrived with a crushed corner and the item inside is cracked."
            ),
            attachments=[attachment],
            status="pending",
            ticket_id=None,
            created_at=_CREATED_AT,
            updated_at=_CREATED_AT,
        )
        self._state.requests[(market, _REQUEST_ID)] = _RequestState(request=request)

    # ------------------------------------------------------------------ reads

    async def get_service_order(self, *, market: str, order_id: str) -> ServiceOrder:
        self._maybe_fail("get_service_order")
        self._validate_market(market)
        self._validate_id(order_id)
        order = self._state.orders.get((market, order_id))
        if order is None:
            raise ConnectorNotFound()
        return order

    async def get_service_order_shipment(self, *, market: str, order_id: str) -> ServiceShipment:
        self._maybe_fail("get_service_order_shipment")
        self._validate_market(market)
        self._validate_id(order_id)
        shipment = self._state.shipments.get((market, order_id))
        if shipment is None:
            raise ConnectorNotFound()
        return shipment

    async def get_attachment_content(self, *, market: str, attachment_id: str) -> AttachmentContent:
        self._maybe_fail("get_attachment_content")
        self._validate_market(market)
        self._validate_id(attachment_id)
        if attachment_id == _ATTACHMENT_ID:
            return AttachmentContent(
                content=_SYNTHETIC_PNG,
                media_type="image/png",
                file_name="synthetic-damage.png",
            )
        if attachment_id == _EXPIRED_ATTACHMENT_ID:
            raise ConnectorGone()
        raise ConnectorNotFound()

    async def list_service_requests(
        self, *, market: str, cursor: str | None = None, limit: int = 50
    ) -> ServiceRequestFeed:
        self._maybe_fail("list_service_requests")
        self._validate_market(market)
        if not 1 <= limit <= 200:
            raise ConnectorValidationError(details={"field": "limit"})
        current = 0 if cursor is None else self._decode_cursor(market, cursor)
        requests = sorted(
            (
                state.request
                for (request_market, _), state in self._state.requests.items()
                if request_market == market
            ),
            key=lambda request: int(request.sequence),
        )
        remaining = [request for request in requests if int(request.sequence) > current]
        page = remaining[:limit]
        next_position = int(page[-1].sequence) if page else current
        high_watermark = str(max((int(request.sequence) for request in requests), default=0))
        return ServiceRequestFeed(
            items=page,
            high_watermark=high_watermark,
            next_cursor=self._encode_cursor(market, next_position),
            has_more=len(remaining) > len(page),
        )

    async def get_service_request(
        self, *, market: str, request_id: str
    ) -> ServiceAfterSalesRequest:
        self._maybe_fail("get_service_request")
        self._validate_market(market)
        self._validate_id(request_id)
        state = self._state.requests.get((market, request_id))
        if state is None:
            raise ConnectorNotFound()
        return state.request

    # ----------------------------------------------------------------- writes

    async def create_ticket(
        self,
        *,
        market: str,
        idempotency_key: str,
        payload: CreateServiceTicketInput,
    ) -> TicketCreationResult:
        self._create_ticket_calls += 1
        self._maybe_fail("create_ticket")
        self._validate_market(market)
        if not validate_idempotency_key(idempotency_key):
            raise ConnectorValidationError(details={"field": "Idempotency-Key"})
        async with self._lock:
            state = self._state.requests.get((market, payload.request_id))
            if state is None:
                raise ConnectorNotFound()
            order = self._state.orders.get((market, state.request.order_id))
            if order is None:
                raise ConnectorNotFound()

            fingerprint = self._fingerprint(payload)
            existing = self._state.idempotency.get((market, idempotency_key))
            if existing is not None:
                existing_fingerprint, ticket_id = existing
                if existing_fingerprint != fingerprint:
                    raise ConnectorConflict(
                        "IDEMPOTENCY_CONFLICT",
                        details={"operation": "createServiceTicket"},
                    )
                return TicketCreationResult(
                    ticket=self._state.tickets[(market, ticket_id)],
                    replayed=True,
                )

            # A replay of the original request is settled above even if the
            # order version has advanced. Only new creations require the
            # caller to hold the current version.
            if payload.order_version != order.order_version:
                raise ConnectorConflict(
                    "ORDER_VERSION_CONFLICT", details={"field": "order_version"}
                )

            if state.ticket_id is not None:
                if state.fingerprint == fingerprint:
                    self._state.idempotency[(market, idempotency_key)] = (
                        fingerprint,
                        state.ticket_id,
                    )
                    return TicketCreationResult(
                        ticket=self._state.tickets[(market, state.ticket_id)],
                        replayed=True,
                    )
                raise ConnectorConflict(
                    "REQUEST_STATE_CONFLICT",
                    details={
                        "request_id": payload.request_id,
                        "status": state.request.status,
                    },
                )

            ticket = self._new_ticket(cast(AfterSalesMarket, market), payload)
            self._state.tickets[(market, ticket.ticket_id)] = ticket
            self._state.idempotency[(market, idempotency_key)] = (
                fingerprint,
                ticket.ticket_id,
            )
            state.ticket_id = ticket.ticket_id
            state.fingerprint = fingerprint
            state.request = state.request.model_copy(
                update={
                    "status": "ticket_created",
                    "ticket_id": ticket.ticket_id,
                    "updated_at": ticket.updated_at,
                }
            )
            return TicketCreationResult(ticket=ticket, replayed=False)

    async def get_service_ticket(self, *, market: str, ticket_id: str) -> ServiceTicket:
        self._maybe_fail("get_service_ticket")
        self._validate_market(market)
        self._validate_id(ticket_id)
        ticket = self._state.tickets.get((market, ticket_id))
        if ticket is None:
            raise ConnectorNotFound()
        return ticket

    async def get_ticket_by_idempotency_key(
        self, *, market: str, idempotency_key: str
    ) -> TicketLookupResult:
        self._maybe_fail("get_ticket_by_idempotency_key")
        self._validate_market(market)
        if not validate_idempotency_key(idempotency_key):
            raise ConnectorValidationError(details={"field": "Idempotency-Key"})
        if (market, idempotency_key) in self._state.in_flight:
            return TicketLookupInFlight(retry_after_ms=250)
        existing = self._state.idempotency.get((market, idempotency_key))
        if existing is None:
            return TicketLookupNotVisible()
        return TicketLookupFound(ticket=self._state.tickets[(market, existing[1])])

    # ---------------------------------------------------------------- controls

    def queue_failure(self, operation: str, error: Exception) -> None:
        """Inject one deterministic failure for local tests only."""
        self._state.failures.setdefault(operation, []).append(error)

    def set_in_flight(self, market: str, idempotency_key: str) -> None:
        self._state.in_flight.add((market, idempotency_key))

    def clear_in_flight(self, market: str, idempotency_key: str) -> None:
        self._state.in_flight.discard((market, idempotency_key))

    async def aclose(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def create_ticket_calls(self) -> int:
        """Number of actual create attempts, including injected failures."""
        return self._create_ticket_calls

    # ----------------------------------------------------------------- helpers

    def _new_ticket(
        self, market: AfterSalesMarket, payload: CreateServiceTicketInput
    ) -> ServiceTicket:
        suffix = payload.request_id.removeprefix("asr_")
        ticket_id = f"tic_{suffix}"
        receipt_id = f"rcp_{suffix}"
        state = self._state.requests[(market, payload.request_id)]
        receipt = TicketReceipt(
            receipt_id=receipt_id,
            ticket_id=ticket_id,
            request_id=payload.request_id,
            action=payload.action,
            request_status="ticket_created",
            created_at=_TICKET_AT,
        )
        return ServiceTicket(
            ticket_id=ticket_id,
            request_id=payload.request_id,
            order_id=state.request.order_id,
            market=market,
            action=payload.action,
            reply=payload.reply,
            status="open",
            draft_version=payload.draft_version,
            approval_id=payload.approval_id,
            policy_version=payload.policy_version,
            order_version=payload.order_version,
            receipt=receipt,
            created_at=_TICKET_AT,
            updated_at=_TICKET_AT,
        )

    @staticmethod
    def _fingerprint(payload: CreateServiceTicketInput) -> str:
        encoded = json.dumps(
            dump_wire_model(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_market(market: str) -> None:
        if market not in AFTER_SALES_MARKETS:
            raise ConnectorPermissionError("FORBIDDEN_MARKET", http_status=403)

    @staticmethod
    def _validate_id(value: str) -> None:
        if not validate_opaque_id(value):
            raise ConnectorValidationError(details={"field": "id"})

    @staticmethod
    def _encode_cursor(market: str, sequence: int) -> str:
        payload = f"fake|{market}|{sequence}".encode()
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    def _decode_cursor(self, market: str, cursor: str) -> int:
        try:
            padded = cursor + ("=" * (-len(cursor) % 4))
            prefix, cursor_market, sequence = (
                base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").split("|", 2)
            )
            if prefix != "fake" or cursor_market != market:
                raise ValueError
            return int(sequence)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise ConnectorValidationError("INVALID_CURSOR", details={"field": "cursor"}) from None

    def _maybe_fail(self, operation: str) -> None:
        failures = self._state.failures.get(operation)
        if failures:
            raise failures.pop(0)
