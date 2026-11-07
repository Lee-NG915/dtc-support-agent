"""Wire models pinned to the E-pig after-sales OpenAPI v1.

These models are receivers/producers of the fixed source contract, never a
second source of truth. Optional additive service fields are ignored; safety
critical DTOs reject unknown fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from app.schemas.after_sales import (
    IDEMPOTENCY_KEY_PATTERN,
    OPAQUE_ID_PATTERN,
    AfterSalesMarket,
)

WIRE_MAJOR = "1"
CONTRACT_HEADER = "X-After-Sales-Contract-Version"
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

RequestType = Literal["damaged", "wrong_item", "return", "other"]
RequestStatus = Literal["pending", "ticket_created"]
TicketAction = Literal["reship", "return", "refund", "reject", "other"]
TicketStatus = Literal["open", "processing", "closed"]
RecoveryAction = Literal[
    "NONE",
    "RETRY_REQUEST",
    "LOOKUP_FIRST",
    "RETRY_CREATE_WITH_SAME_KEY",
    "RECONCILE_MANUALLY",
]
WireErrorCode = Literal[
    "VALIDATION_ERROR",
    "UNSUPPORTED_CONTRACT_VERSION",
    "INVALID_CURSOR",
    "UNAUTHORIZED",
    "FORBIDDEN_MARKET",
    "NOT_FOUND",
    "IDEMPOTENCY_CONFLICT",
    "REQUEST_STATE_CONFLICT",
    "ORDER_VERSION_CONFLICT",
    "ATTACHMENT_EXPIRED",
    "ATTACHMENT_TOO_LARGE",
    "UNSUPPORTED_MEDIA_TYPE",
    "RATE_LIMITED",
    "INTERNAL_ERROR",
    "UPSTREAM_UNAVAILABLE",
]
MediaType = Literal["image/jpeg", "image/png", "image/webp"]

_UTC_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")
_OPAQUE_ID_RE = re.compile(OPAQUE_ID_PATTERN)
_IDEMPOTENCY_KEY_RE = re.compile(IDEMPOTENCY_KEY_PATTERN)


def validate_utc_datetime(value: object) -> datetime:
    """Accept only UTC RFC3339 values ending in Z, including on model construction."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("datetime must be UTC")
        return value.astimezone(UTC)
    if not isinstance(value, str) or _UTC_PATTERN.fullmatch(value) is None:
        raise ValueError("datetime must be UTC RFC3339 ending in Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid UTC RFC3339 datetime") from exc
    return parsed.astimezone(UTC)


def format_utc_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


UTCDatetime = Annotated[
    datetime,
    BeforeValidator(validate_utc_datetime),
    PlainSerializer(format_utc_datetime, return_type=str, when_used="json"),
]
OpaqueId = Annotated[str, Field(min_length=1, max_length=128, pattern=OPAQUE_ID_PATTERN)]
IdempotencyKey = Annotated[
    str, Field(min_length=1, max_length=255, pattern=IDEMPOTENCY_KEY_PATTERN)
]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Sequence = Annotated[str, Field(pattern=r"^[0-9]+$")]
Cursor = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_-]+$")]


def validate_opaque_id(value: str) -> bool:
    return bool(_OPAQUE_ID_RE.fullmatch(value))


def validate_idempotency_key(value: str) -> bool:
    return bool(_IDEMPOTENCY_KEY_RE.fullmatch(value))


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: OpaqueId
    file_name: str = Field(min_length=1, max_length=255)
    media_type: MediaType
    size_bytes: int = Field(ge=1, le=MAX_ATTACHMENT_BYTES)
    status: Literal["active", "expired"]
    created_at: UTCDatetime
    expires_at: UTCDatetime


class RecipientSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    masked_name: str = Field(min_length=1, max_length=128)
    country_code: Literal["AU", "SG", "US"]
    region: str = Field(min_length=1, max_length=128)
    postal_code_masked: str = Field(min_length=1, max_length=32)


class ServiceOrderItem(BaseModel):
    # Additive service field tolerance: keep only fields DTC actually needs.
    model_config = ConfigDict(extra="ignore")

    order_item_id: OpaqueId
    sku: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    quantity: int = Field(ge=1)
    unit_price_minor: int = Field(ge=0)
    line_total_minor: int = Field(ge=0)


class ServiceOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: OpaqueId
    market: AfterSalesMarket
    currency: CurrencyCode
    items: list[ServiceOrderItem]
    subtotal_minor: int = Field(ge=0)
    shipping_minor: int = Field(ge=0)
    tax_minor: int = Field(ge=0)
    discount_minor: int = Field(ge=0)
    total_minor: int = Field(ge=0)
    status: Literal["paid", "fulfilling", "fulfilled", "cancelled"]
    created_at: UTCDatetime
    order_version: str = Field(min_length=1, max_length=128)
    recipient_summary: RecipientSummary


class ShipmentEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: OpaqueId
    status: Literal["pending", "in_transit", "delivered", "exception"]
    description: str = Field(min_length=1, max_length=1000)
    occurred_at: UTCDatetime
    location_summary: str | None = Field(default=None, min_length=1, max_length=128)


class ServiceShipment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shipment_id: OpaqueId
    order_id: OpaqueId
    status: Literal["pending", "in_transit", "delivered", "exception"]
    carrier: str = Field(min_length=1, max_length=128)
    events: list[ShipmentEvent]


class ServiceAfterSalesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_id: OpaqueId
    sequence: Sequence
    order_id: OpaqueId
    market: AfterSalesMarket
    type: RequestType
    description: str = Field(min_length=1, max_length=5000, pattern=r"\S")
    attachments: list[Attachment] = Field(max_length=3)
    status: RequestStatus
    ticket_id: OpaqueId | None
    created_at: UTCDatetime
    updated_at: UTCDatetime

    @model_validator(mode="after")
    def validate_ticket_linkage(self) -> ServiceAfterSalesRequest:
        if self.status == "pending" and self.ticket_id is not None:
            raise ValueError("pending request cannot have a ticket_id")
        if self.status == "ticket_created" and self.ticket_id is None:
            raise ValueError("ticket_created request requires a ticket_id")
        return self


class ServiceRequestFeed(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[ServiceAfterSalesRequest]
    high_watermark: Annotated[str, Field(pattern=r"^[0-9]+$")]
    next_cursor: Cursor
    has_more: bool


class CreateServiceTicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: OpaqueId
    action: TicketAction
    reply: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    draft_version: str = Field(min_length=1, max_length=128)
    approval_id: OpaqueId
    policy_version: str = Field(min_length=1, max_length=128)
    order_version: str = Field(min_length=1, max_length=128)


class ServiceTicketCreationPending(BaseModel):
    model_config = ConfigDict(extra="ignore")

    outcome: Literal["IN_FLIGHT"]
    retry_after_ms: int = Field(ge=0)


class TicketReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    receipt_id: OpaqueId
    ticket_id: OpaqueId
    request_id: OpaqueId
    action: TicketAction
    request_status: Literal["ticket_created"]
    created_at: UTCDatetime


class ServiceTicket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticket_id: OpaqueId
    request_id: OpaqueId
    order_id: OpaqueId
    market: AfterSalesMarket
    action: TicketAction
    reply: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    status: TicketStatus
    draft_version: str = Field(min_length=1, max_length=128)
    approval_id: OpaqueId
    policy_version: str = Field(min_length=1, max_length=128)
    order_version: str = Field(min_length=1, max_length=128)
    receipt: TicketReceipt
    created_at: UTCDatetime
    updated_at: UTCDatetime


class ErrorDetails(BaseModel):
    """Producer-allowlisted non-PII diagnostics."""

    model_config = ConfigDict(extra="forbid")

    field: str | None = Field(default=None, min_length=1, max_length=128)
    retry_after_seconds: int | None = Field(default=None, ge=0)
    allowed_media_types: list[MediaType] | None = Field(default=None, max_length=3)
    attachment_id: OpaqueId | None = None
    operation: Literal["createAfterSalesRequest", "createServiceTicket"] | None = None
    max_size_bytes: int | None = Field(default=None, ge=1)
    request_id: OpaqueId | None = None
    status: RequestStatus | None = None
    supported_major: Literal["1"] | None = None
    market: AfterSalesMarket | None = None


class Error(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: WireErrorCode
    message: str = Field(min_length=1, max_length=1000)
    request_id: OpaqueId
    retryable: bool
    recovery_action: RecoveryAction
    details: ErrorDetails | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: Error


@dataclass(frozen=True)
class AttachmentContent:
    content: bytes
    media_type: str
    file_name: str | None = None


@dataclass(frozen=True)
class TicketCreationResult:
    ticket: ServiceTicket
    replayed: bool


@dataclass(frozen=True)
class TicketLookupFound:
    ticket: ServiceTicket


@dataclass(frozen=True)
class TicketLookupInFlight:
    retry_after_ms: int


@dataclass(frozen=True)
class TicketLookupNotVisible:
    """A temporary 404. Never proof that no ticket was ever created."""


TicketLookupResult = TicketLookupFound | TicketLookupInFlight | TicketLookupNotVisible


def dump_wire_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


__all__ = [
    "Attachment",
    "AttachmentContent",
    "CONTRACT_HEADER",
    "CreateServiceTicketInput",
    "Cursor",
    "Error",
    "ErrorDetails",
    "ErrorResponse",
    "IdempotencyKey",
    "MAX_ATTACHMENT_BYTES",
    "OpaqueId",
    "RecipientSummary",
    "ServiceAfterSalesRequest",
    "ServiceOrder",
    "ServiceOrderItem",
    "ServiceRequestFeed",
    "ServiceShipment",
    "ServiceTicket",
    "ServiceTicketCreationPending",
    "TicketAction",
    "TicketCreationResult",
    "TicketLookupFound",
    "TicketLookupInFlight",
    "TicketLookupNotVisible",
    "TicketLookupResult",
    "TicketReceipt",
    "TicketStatus",
    "UTCDatetime",
    "WIRE_MAJOR",
    "dump_wire_model",
    "format_utc_datetime",
    "validate_idempotency_key",
    "validate_opaque_id",
    "validate_utc_datetime",
]
