"""HTTP implementation of the fixed E-pig after-sales service contract."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import ValidationError

from app.config.settings import Settings
from app.connectors.errors import (
    ConnectorConfigurationError,
    ConnectorError,
    ConnectorPermissionError,
    ConnectorProtocolError,
    ConnectorUnavailable,
    ConnectorValidationError,
    map_wire_error,
)
from app.connectors.models import (
    CONTRACT_HEADER,
    MAX_ATTACHMENT_BYTES,
    WIRE_MAJOR,
    AttachmentContent,
    CreateServiceTicketInput,
    ErrorResponse,
    ServiceAfterSalesRequest,
    ServiceOrder,
    ServiceRequestFeed,
    ServiceShipment,
    ServiceTicket,
    ServiceTicketCreationPending,
    TicketCreationResult,
    TicketLookupFound,
    TicketLookupInFlight,
    TicketLookupNotVisible,
    TicketLookupResult,
    dump_wire_model,
    validate_idempotency_key,
    validate_opaque_id,
)
from app.schemas.after_sales import AFTER_SALES_MARKETS, AfterSalesMarket

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_READ_RETRY_STATUSES = {429, 502, 503, 504}
_ALLOWED_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/octet-stream",
}
_CONTENT_DISPOSITION_FILENAME = re.compile(r'filename="([^"]{1,255})"')
_ATTACHMENT_CHUNK_BYTES = 64 * 1024
_MAX_ERROR_BODY_BYTES = 64 * 1024


class EpigConnector:
    """HTTP connector with fail-closed configuration and no blind write retries."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._base_url = _validate_base_url(settings.epig_base_url, settings.app_env)
        token = settings.epig_service_token.get_secret_value().strip()
        if not token:
            raise ConnectorConfigurationError("EPIG_SERVICE_TOKEN is required for EpigConnector")
        self._token = token
        self._allowed_markets = _parse_markets(settings.epig_allowed_markets)
        self._timeout = settings.epig_timeout_seconds
        self._max_connections = settings.epig_max_connections
        self._sleep = sleep or asyncio.sleep
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            limits=httpx.Limits(
                max_connections=self._max_connections,
                max_keepalive_connections=self._max_connections,
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._closed = False

    # ------------------------------------------------------------------ reads

    async def get_service_order(self, *, market: str, order_id: str) -> ServiceOrder:
        self._validate_market(market)
        self._validate_id(order_id)
        response = await self._read(self._url(market, f"service/orders/{quote(order_id, safe='')}"))
        if response.status_code == 200:
            order = self._model(ServiceOrder, response)
            if order.market != market or order.order_id != order_id:
                raise ConnectorProtocolError()
            return order
        raise self._error(response, create_operation=False)

    async def get_service_order_shipment(self, *, market: str, order_id: str) -> ServiceShipment:
        self._validate_market(market)
        self._validate_id(order_id)
        response = await self._read(
            self._url(market, f"service/orders/{quote(order_id, safe='')}/shipment")
        )
        if response.status_code == 200:
            shipment = self._model(ServiceShipment, response)
            if shipment.order_id != order_id:
                raise ConnectorProtocolError()
            return shipment
        raise self._error(response, create_operation=False)

    async def get_attachment_content(self, *, market: str, attachment_id: str) -> AttachmentContent:
        self._validate_market(market)
        self._validate_id(attachment_id)
        return await self._read_attachment(
            self._url(
                market,
                f"service/attachments/{quote(attachment_id, safe='')}/content",
            ),
            headers={"Accept": ", ".join(sorted(_ALLOWED_MEDIA_TYPES))},
        )

    async def list_service_requests(
        self, *, market: str, cursor: str | None = None, limit: int = 50
    ) -> ServiceRequestFeed:
        self._validate_market(market)
        if not 1 <= limit <= 200:
            raise ConnectorValidationError(details={"field": "limit"})
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = await self._read(
            self._url(market, "service/requests"),
            params=params,
        )
        if response.status_code == 200:
            feed = self._model(ServiceRequestFeed, response)
            if any(item.market != market for item in feed.items):
                raise ConnectorProtocolError()
            return feed
        raise self._error(response, create_operation=False)

    async def get_service_request(
        self, *, market: str, request_id: str
    ) -> ServiceAfterSalesRequest:
        self._validate_market(market)
        self._validate_id(request_id)
        response = await self._read(
            self._url(market, f"service/requests/{quote(request_id, safe='')}")
        )
        if response.status_code == 200:
            request = self._model(ServiceAfterSalesRequest, response)
            if request.market != market or request.request_id != request_id:
                raise ConnectorProtocolError()
            return request
        raise self._error(response, create_operation=False)

    # ----------------------------------------------------------------- writes

    async def create_ticket(
        self,
        *,
        market: str,
        idempotency_key: str,
        payload: CreateServiceTicketInput,
    ) -> TicketCreationResult:
        self._validate_market(market)
        self._validate_idempotency_key(idempotency_key)
        try:
            response = await self._write(
                self._url(market, "service/tickets"),
                idempotency_key=idempotency_key,
                json_body=dump_wire_model(payload),
            )
            if response.status_code == 201:
                ticket = self._model(ServiceTicket, response)
                self._verify_created_ticket(ticket, market=market, payload=payload)
                return TicketCreationResult(ticket=ticket, replayed=False)
            if response.status_code == 200:
                if response.headers.get("idempotency-replayed", "").lower() != "true":
                    raise ConnectorProtocolError()
                ticket = self._model(ServiceTicket, response)
                self._verify_created_ticket(ticket, market=market, payload=payload)
                return TicketCreationResult(ticket=ticket, replayed=True)
            raise self._error(response, create_operation=True)
        except ConnectorProtocolError as error:
            # The POST may already have reached the upstream, so a protocol
            # violation cannot prove that no ticket was created. Resolve it by
            # looking up the original idempotency key; never resend blindly.
            error.recovery_action = "LOOKUP_FIRST"
            error.outcome_unknown = True
            raise

    async def get_service_ticket(self, *, market: str, ticket_id: str) -> ServiceTicket:
        self._validate_market(market)
        self._validate_id(ticket_id)
        response = await self._read(
            self._url(market, f"service/tickets/{quote(ticket_id, safe='')}")
        )
        if response.status_code == 200:
            ticket = self._model(ServiceTicket, response)
            if ticket.market != market or ticket.ticket_id != ticket_id:
                raise ConnectorProtocolError()
            self._verify_ticket_receipt(ticket)
            return ticket
        raise self._error(response, create_operation=False)

    async def get_ticket_by_idempotency_key(
        self, *, market: str, idempotency_key: str
    ) -> TicketLookupResult:
        self._validate_market(market)
        self._validate_idempotency_key(idempotency_key)
        response = await self._read(
            self._url(
                market,
                f"service/tickets/by-idempotency-key/{quote(idempotency_key, safe='')}",
            )
        )
        if response.status_code == 200:
            ticket = self._model(ServiceTicket, response)
            if ticket.market != market:
                raise ConnectorProtocolError()
            # The opaque key does not identify the ticket or request, so this
            # read can only prove internal consistency. The create operation
            # must still compare the ticket with its original payload.
            self._verify_ticket_receipt(ticket)
            return TicketLookupFound(ticket=ticket)
        if response.status_code == 202:
            pending = self._model(ServiceTicketCreationPending, response)
            return TicketLookupInFlight(retry_after_ms=pending.retry_after_ms)
        if response.status_code == 404:
            # Validate the documented temporary-absence error, but keep it a
            # nonterminal union member rather than raising a permanent 404.
            parsed = self._parsed_error(response)
            if (
                parsed.error.code != "NOT_FOUND"
                or parsed.error.recovery_action != "RETRY_CREATE_WITH_SAME_KEY"
            ):
                raise ConnectorProtocolError()
            return TicketLookupNotVisible()
        raise self._error(response, create_operation=False)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    @property
    def closed(self) -> bool:
        return self._closed

    # ---------------------------------------------------------------- requests

    def _url(self, market: str, suffix: str) -> str:
        return f"{self._base_url}/api/{quote(market, safe='')}/after-sales/{suffix}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            CONTRACT_HEADER: WIRE_MAJOR,
            "Accept": "application/json",
        }

    async def _read(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {**self._headers(), **(headers or {})}
        for attempt in range(2):
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=request_headers,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 0:
                    await self._sleep(self._retry_delay(None))
                    continue
                raise ConnectorUnavailable(create_operation=False) from None
            self._check_version(response)
            if response.status_code in _READ_RETRY_STATUSES and attempt == 0:
                delay = self._retry_delay(response.headers.get("retry-after"))
                response.close()
                await self._sleep(delay)
                continue
            return response
        raise ConnectorUnavailable(create_operation=False)

    async def _read_attachment(
        self,
        url: str,
        *,
        headers: dict[str, str],
    ) -> AttachmentContent:
        """Stream one attachment with the same bounded read-retry semantics."""
        request_headers = {**self._headers(), **headers}
        for attempt in range(2):
            retry_delay: float | None = None
            try:
                async with self._client.stream(
                    "GET",
                    url,
                    headers=request_headers,
                ) as response:
                    self._check_version(response)
                    if response.status_code in _READ_RETRY_STATUSES and attempt == 0:
                        retry_delay = self._retry_delay(response.headers.get("retry-after"))
                    else:
                        return await self._attachment_response(response)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 0:
                    await self._sleep(self._retry_delay(None))
                    continue
                raise ConnectorUnavailable(create_operation=False) from None
            # The streaming response is closed before waiting and retrying.
            if retry_delay is None:
                raise ConnectorUnavailable(create_operation=False)
            await self._sleep(retry_delay)
        raise ConnectorUnavailable(create_operation=False)

    async def _attachment_response(self, response: httpx.Response) -> AttachmentContent:
        if response.status_code != 200:
            content = await self._read_bounded_body(
                response,
                limit=_MAX_ERROR_BODY_BYTES,
                on_overflow=ConnectorProtocolError,
            )
            # Reuse the existing error mapping, but only over the bounded,
            # already-decoded body. Transport headers (notably
            # Content-Encoding/Content-Length) no longer describe these bytes,
            # and _error only needs the status and JSON body.
            error_response = httpx.Response(response.status_code, content=content)
            raise self._error(error_response, create_operation=False)
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in _ALLOWED_MEDIA_TYPES:
            raise ConnectorValidationError(
                "UNSUPPORTED_MEDIA_TYPE",
                details={"allowed_media_types": ["image/jpeg", "image/png", "image/webp"]},
                http_status=415,
            )
        content = await self._read_bounded_body(
            response,
            limit=MAX_ATTACHMENT_BYTES,
            on_overflow=lambda: ConnectorValidationError(
                "ATTACHMENT_TOO_LARGE",
                details={"max_size_bytes": MAX_ATTACHMENT_BYTES},
                http_status=413,
            ),
        )
        disposition = response.headers.get("content-disposition", "")
        match = _CONTENT_DISPOSITION_FILENAME.search(disposition)
        return AttachmentContent(
            content=content,
            media_type=media_type,
            file_name=match.group(1) if match else None,
        )

    async def _read_bounded_body(
        self,
        response: httpx.Response,
        *,
        limit: int,
        on_overflow: Callable[[], ConnectorError],
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes(chunk_size=_ATTACHMENT_CHUNK_BYTES):
            total += len(chunk)
            if total > limit:
                raise on_overflow()
            chunks.append(chunk)
        return b"".join(chunks)

    async def _write(
        self,
        url: str,
        *,
        idempotency_key: str,
        json_body: dict[str, Any],
    ) -> httpx.Response:
        headers = {
            **self._headers(),
            "Idempotency-Key": idempotency_key,
            "Accept": "application/json",
        }
        try:
            # Writes are attempted exactly once. Any uncertain outcome must be
            # resolved by idempotency lookup with the original key.
            response = await self._client.post(url, headers=headers, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError):
            raise ConnectorUnavailable(create_operation=True) from None
        self._check_version(response)
        return response

    def _check_version(self, response: httpx.Response) -> None:
        if response.headers.get("x-after-sales-contract-version") != WIRE_MAJOR:
            raise ConnectorProtocolError()

    def _error(self, response: httpx.Response, *, create_operation: bool) -> ConnectorError:
        parsed = self._parsed_error(response)
        return map_wire_error(
            response.status_code,
            parsed,
            create_operation=create_operation,
        )

    @staticmethod
    def _parsed_error(response: httpx.Response) -> ErrorResponse:
        try:
            return ErrorResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ConnectorProtocolError() from None

    @staticmethod
    def _model(model: type[Any], response: httpx.Response) -> Any:
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ConnectorProtocolError() from None

    @staticmethod
    def _verify_ticket_receipt(ticket: ServiceTicket) -> None:
        receipt = ticket.receipt
        if (
            receipt.ticket_id != ticket.ticket_id
            or receipt.request_id != ticket.request_id
            or receipt.action != ticket.action
        ):
            raise ConnectorProtocolError()

    @classmethod
    def _verify_created_ticket(
        cls,
        ticket: ServiceTicket,
        *,
        market: str,
        payload: CreateServiceTicketInput,
    ) -> None:
        if ticket.market != market or (
            ticket.request_id != payload.request_id
            or ticket.action != payload.action
            or ticket.reply != payload.reply
            or ticket.draft_version != payload.draft_version
            or ticket.approval_id != payload.approval_id
            or ticket.policy_version != payload.policy_version
            or ticket.order_version != payload.order_version
        ):
            raise ConnectorProtocolError()
        cls._verify_ticket_receipt(ticket)

    @staticmethod
    def _retry_delay(retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 1.0))
            except ValueError:
                pass
        return 0.1

    # --------------------------------------------------------------- validation

    def _validate_market(self, market: str) -> None:
        if market not in AFTER_SALES_MARKETS:
            raise ConnectorPermissionError("FORBIDDEN_MARKET", http_status=403)
        if market not in self._allowed_markets:
            raise ConnectorPermissionError("FORBIDDEN_MARKET", http_status=403)

    @staticmethod
    def _validate_id(value: str) -> None:
        if not validate_opaque_id(value):
            raise ConnectorValidationError(details={"field": "id"})

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not validate_idempotency_key(value):
            raise ConnectorValidationError(details={"field": "Idempotency-Key"})


def _parse_markets(raw: str) -> frozenset[AfterSalesMarket]:
    values = [value.strip().lower() for value in raw.split(",") if value.strip()]
    if not values:
        raise ConnectorConfigurationError("EPIG_ALLOWED_MARKETS is required for EpigConnector")
    invalid = sorted(set(values) - AFTER_SALES_MARKETS)
    if invalid:
        raise ConnectorConfigurationError(
            f"EPIG_ALLOWED_MARKETS contains unsupported markets: {invalid}"
        )
    return frozenset(values)  # type: ignore[arg-type]


def _validate_base_url(raw: str, app_env: str) -> str:
    value = raw.strip().rstrip("/")
    if not value:
        raise ConnectorConfigurationError("EPIG_BASE_URL is required for EpigConnector")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConnectorConfigurationError("EPIG_BASE_URL must be an absolute HTTP(S) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConnectorConfigurationError(
            "EPIG_BASE_URL must not contain credentials, query parameters or fragments"
        )
    if app_env == "production" and parts.scheme != "https":
        raise ConnectorConfigurationError("EPIG_BASE_URL must use HTTPS in production")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        raise ConnectorConfigurationError(
            "plain HTTP EPIG_BASE_URL is only allowed for explicit loopback tests"
        )
    return value
