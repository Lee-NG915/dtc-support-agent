"""Single business connector protocol shared by Fake and E-pig implementations."""

from __future__ import annotations

from typing import Protocol

from app.connectors.models import (
    AttachmentContent,
    CreateServiceTicketInput,
    ServiceAfterSalesRequest,
    ServiceOrder,
    ServiceRequestFeed,
    ServiceShipment,
    ServiceTicket,
    TicketCreationResult,
    TicketLookupResult,
)


class BusinessConnector(Protocol):
    """Service-side E-pig boundary.

    ``create_ticket`` exists for the later approval-execution slice; DTC-2a
    never exposes it through a public route.
    """

    async def get_service_order(self, *, market: str, order_id: str) -> ServiceOrder: ...

    async def get_service_order_shipment(
        self, *, market: str, order_id: str
    ) -> ServiceShipment: ...

    async def get_attachment_content(
        self, *, market: str, attachment_id: str
    ) -> AttachmentContent: ...

    async def list_service_requests(
        self, *, market: str, cursor: str | None = None, limit: int = 50
    ) -> ServiceRequestFeed: ...

    async def get_service_request(
        self, *, market: str, request_id: str
    ) -> ServiceAfterSalesRequest: ...

    async def create_ticket(
        self,
        *,
        market: str,
        idempotency_key: str,
        payload: CreateServiceTicketInput,
    ) -> TicketCreationResult: ...

    async def get_service_ticket(self, *, market: str, ticket_id: str) -> ServiceTicket: ...

    async def get_ticket_by_idempotency_key(
        self, *, market: str, idempotency_key: str
    ) -> TicketLookupResult: ...

    async def aclose(self) -> None: ...
