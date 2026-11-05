"""Authenticated, read-only after-sales APIs and public staff identity."""

from __future__ import annotations

import re
from typing import NoReturn

from fastapi import APIRouter, Depends, Response

from app.api.dependencies import get_container, get_required_staff
from app.connectors.base import BusinessConnector
from app.connectors.errors import (
    ConnectorError,
    ConnectorGone,
    ConnectorNotFound,
    ConnectorUnavailable,
)
from app.connectors.models import ServiceOrder, ServiceShipment
from app.core.exceptions import (
    AttachmentExpiredError,
    ConnectorContractError,
    ConnectorUnavailableError,
    ForbiddenMarketError,
    ForbiddenRoleError,
    InvalidRequestError,
    NotFoundError,
)
from app.schemas.after_sales import (
    AFTER_SALES_MARKETS,
    READ_ROLES,
    StaffActor,
    StaffIdentityResponse,
    is_valid_opaque_id,
)
from app.services.container import AppContainer

router = APIRouter(prefix="/after-sales", tags=["after-sales"])

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@router.get("/me", response_model=StaffIdentityResponse)
async def me(actor: StaffActor = Depends(get_required_staff)) -> StaffIdentityResponse:
    """Return only the current staff member's public identity."""
    return StaffIdentityResponse(
        staff_id=actor.staff_id,
        display_name=actor.display_name,
        roles=sorted(actor.roles),
        allowed_markets=sorted(actor.allowed_markets),
    )


@router.get("/{market}/orders/{order_id}", response_model=ServiceOrder)
async def get_order(
    market: str,
    order_id: str,
    actor: StaffActor = Depends(get_required_staff),
    container: AppContainer = Depends(get_container),
) -> ServiceOrder:
    _authorize_read(actor, market)
    _validate_id(order_id, "order_id")
    connector = _connector(container)
    try:
        return await connector.get_service_order(market=market, order_id=order_id)
    except ConnectorError as exc:
        _raise_connector_error(exc)


@router.get("/{market}/orders/{order_id}/shipment", response_model=ServiceShipment)
async def get_shipment(
    market: str,
    order_id: str,
    actor: StaffActor = Depends(get_required_staff),
    container: AppContainer = Depends(get_container),
) -> ServiceShipment:
    _authorize_read(actor, market)
    _validate_id(order_id, "order_id")
    connector = _connector(container)
    try:
        return await connector.get_service_order_shipment(market=market, order_id=order_id)
    except ConnectorError as exc:
        _raise_connector_error(exc)


@router.get("/{market}/attachments/{attachment_id}/content")
async def get_attachment_content(
    market: str,
    attachment_id: str,
    actor: StaffActor = Depends(get_required_staff),
    container: AppContainer = Depends(get_container),
) -> Response:
    _authorize_read(actor, market)
    _validate_id(attachment_id, "attachment_id")
    connector = _connector(container)
    try:
        content = await connector.get_attachment_content(
            market=market,
            attachment_id=attachment_id,
        )
    except ConnectorError as exc:
        _raise_connector_error(exc)
    filename = (
        _FILENAME_SAFE.sub("_", content.file_name or "attachment").strip("._") or "attachment"
    )
    return Response(
        content=content.content,
        media_type=content.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _authorize_read(actor: StaffActor, market: str) -> None:
    if market not in AFTER_SALES_MARKETS:
        raise InvalidRequestError("invalid market")
    if not actor.may_read_market(market):
        raise ForbiddenMarketError()
    if not actor.has_role(*READ_ROLES):
        raise ForbiddenRoleError()


def _validate_id(value: str, field: str) -> None:
    if not is_valid_opaque_id(value):
        raise InvalidRequestError(f"invalid {field}")


def _connector(container: AppContainer) -> BusinessConnector:
    connector = container.business_connector
    if connector is None:
        raise ConnectorUnavailableError()
    return connector


def _raise_connector_error(exc: ConnectorError) -> NoReturn:
    if isinstance(exc, ConnectorNotFound):
        raise NotFoundError() from None
    if isinstance(exc, ConnectorGone):
        raise AttachmentExpiredError() from None
    if isinstance(exc, ConnectorUnavailable):
        raise ConnectorUnavailableError() from None
    raise ConnectorContractError() from None
