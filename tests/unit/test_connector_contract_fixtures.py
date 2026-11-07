"""Run the shared fixtures applicable to the service-side connector receiver."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.connectors.errors import map_wire_error
from app.connectors.models import (
    Attachment,
    CreateServiceTicketInput,
    ErrorResponse,
    ServiceAfterSalesRequest,
    ServiceOrder,
    ServiceRequestFeed,
    ServiceShipment,
    ServiceTicket,
    ServiceTicketCreationPending,
)

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "contracts" / "epig-after-sales" / "v1"
MANIFEST = json.loads((BUNDLE / "fixtures" / "manifest.json").read_text(encoding="utf-8"))

# Fixtures whose DTO is both produced and consumed by this service boundary.
MODEL_FIXTURES: dict[str, type[BaseModel]] = {
    "create_service_ticket_valid": CreateServiceTicketInput,
    "create_service_ticket_unknown_field": CreateServiceTicketInput,
    "create_service_ticket_missing_approval_id": CreateServiceTicketInput,
    "create_service_ticket_blank_reply": CreateServiceTicketInput,
    "upload_attachment_created": Attachment,
    "upload_attachment_storage_url": Attachment,
    "get_service_order_ok": ServiceOrder,
    "get_service_order_full_address": ServiceOrder,
    "get_service_order_full_address_top_level": ServiceOrder,
    "get_service_order_shipment_ok": ServiceShipment,
    "list_service_requests_ok": ServiceRequestFeed,
    "list_service_requests_multipage": ServiceRequestFeed,
    "get_service_request_ok": ServiceAfterSalesRequest,
    "get_service_request_inconsistent_status": ServiceAfterSalesRequest,
    "create_service_ticket_created": ServiceTicket,
    "create_service_ticket_replayed": ServiceTicket,
    "get_service_ticket_ok": ServiceTicket,
    "get_service_ticket_by_idempotency_key_ok": ServiceTicket,
    "get_service_ticket_by_idempotency_key_in_flight": ServiceTicketCreationPending,
}

ERROR_FIXTURES = {
    "create_after_sales_request_validation_error",
    "get_service_order_forbidden_market",
    "get_after_sales_request_ownership_not_found",
    "create_after_sales_request_idempotency_conflict",
    "create_service_ticket_idempotency_conflict",
    "list_service_requests_invalid_cursor",
    "upload_attachment_too_large",
    "upload_attachment_unsupported_media_type",
    "create_after_sales_request_attachment_expired",
    "list_service_requests_rate_limited",
    "create_service_ticket_upstream_unavailable",
    "create_service_ticket_internal_error",
    "create_service_ticket_request_state_conflict",
    "get_attachment_content_unauthorized",
    "get_service_ticket_by_idempotency_key_not_found",
    "create_service_ticket_order_version_conflict",
    "create_after_sales_request_unsupported_contract_version",
    "create_after_sales_request_error_leak",
}

BINARY_FIXTURES = {"get_service_attachment_content_ok"}

# Consumer-only DTOs, inputs and attachment upload/read routes are not called
# by the DTC service connector in DTC-2a. They are recorded here rather than
# counted as covered.
NOT_APPLICABLE = {
    "create_after_sales_request_valid": "consumer request producer DTO",
    "create_after_sales_request_unknown_field": "consumer request producer DTO",
    "create_after_sales_request_too_many_attachments": "consumer request producer DTO",
    "create_after_sales_request_blank_description": "consumer request producer DTO",
    "get_attachment_content_ok": "consumer attachment route is not called by DTC",
    "create_after_sales_request_created": "consumer response DTO is not called by DTC",
    "create_after_sales_request_replayed": "consumer response DTO is not called by DTC",
    "get_after_sales_request_ok": "consumer response DTO is not called by DTC",
    "get_after_sales_request_inconsistent_status": "consumer response DTO is not called by DTC",
    "get_consumer_ticket_ok": "consumer ticket DTO is not called by DTC",
    "get_consumer_ticket_audit_fields": "consumer ticket DTO is not called by DTC",
}


def _entry(fixture_id: str) -> dict:
    return next(item for item in MANIFEST["fixtures"] if item["fixture_id"] == fixture_id)


def _load(entry: dict) -> object:
    return json.loads((BUNDLE / "fixtures" / entry["path"]).read_text(encoding="utf-8"))


def test_every_shared_fixture_is_classified_exactly_once() -> None:
    manifest_ids = {item["fixture_id"] for item in MANIFEST["fixtures"]}
    groups = [set(MODEL_FIXTURES), ERROR_FIXTURES, BINARY_FIXTURES, set(NOT_APPLICABLE)]
    covered = set().union(*groups)
    assert manifest_ids == covered
    assert sum(len(group) for group in groups) == len(covered)


@pytest.mark.parametrize("fixture_id", sorted(MODEL_FIXTURES))
def test_applicable_wire_model_fixture(fixture_id: str) -> None:
    entry = _entry(fixture_id)
    model = MODEL_FIXTURES[fixture_id]
    payload = _load(entry)
    if entry["expected_valid"]:
        model.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.parametrize("fixture_id", sorted(ERROR_FIXTURES))
def test_error_fixture_is_safe_and_classified(fixture_id: str) -> None:
    entry = _entry(fixture_id)
    payload = _load(entry)
    if not entry["expected_valid"]:
        with pytest.raises(ValidationError):
            ErrorResponse.model_validate(payload)
        return
    parsed = ErrorResponse.model_validate(payload)
    create_operation = entry["operation_id"] == "createServiceTicket"
    mapped = map_wire_error(
        entry["http_status"],
        parsed,
        create_operation=create_operation,
    )
    assert mapped.code == parsed.error.code
    assert mapped.message
    if create_operation and entry["http_status"] >= 429:
        assert mapped.retryable is False
        assert mapped.recovery_action == "LOOKUP_FIRST"
        assert mapped.outcome_unknown is True
    assert "full_address" not in mapped.message


def test_service_binary_fixture_matches_fake_content_shape() -> None:
    entry = _entry("get_service_attachment_content_ok")
    data = (BUNDLE / "fixtures" / entry["path"]).read_bytes()
    assert len(data) == entry["size_bytes"]
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert entry["media_type"] == "image/png"
