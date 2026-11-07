"""Server-authoritative order amounts in the after-sales suggestion payload."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.connectors.models import RecipientSummary, ServiceOrder, ServiceOrderItem
from app.core.exceptions import LLMOutputError
from app.services.after_sales.evidence import EvidenceResult
from app.services.after_sales.service import (
    _format_money,
    _order_amounts,
    _parse_suggestion_output,
    _suggestion_messages,
)


def _order() -> ServiceOrder:
    return ServiceOrder(
        order_id="ord_sg_00000042",
        market="sg",
        currency="SGD",
        items=[
            ServiceOrderItem(
                order_item_id="oit_000000000042",
                sku="SYNTH-MATTRESS-008",
                name="Synthetic mattress",
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
        order_version="ov_000000000007",
        recipient_summary=RecipientSummary(
            masked_name="A*** B***",
            country_code="SG",
            region="Central",
            postal_code_masked="2***",
        ),
    )


def _suggestion_payload(**overrides: Any) -> str:
    payload = {
        "action": "reship",
        "internal_summary": "Order total is SGD 137.95.",
        "consumer_reply": "We received your materials and will review them.",
        "evidence_chunk_ids": ["faults-missing_abc12345_02"],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _context() -> SimpleNamespace:
    order = _order()
    return SimpleNamespace(
        case=SimpleNamespace(
            case_id=42,
            market="sg",
            request_type="damaged",
            description="Synthetic damaged mattress request.",
        ),
        order=order,
        remote=SimpleNamespace(
            request_id="asr_000000000042",
            order_id=order.order_id,
            type="damaged",
            status="pending",
            attachments=[],
        ),
        materials=[],
        categories=[],
        evidence=EvidenceResult(
            chunks=[
                {
                    "chunk_id": "faults-missing_abc12345_02",
                    "title": "E-pig SG 损坏、错发与缺件处理材料",
                    "section": "材料清单",
                    "content": "缺件：已收到的商品与配件合照、包装清单、全部包材照片。",
                    "source_path": "epig/sg/faults-missing.md",
                    "policy_version": "demo-2026-11-05",
                    "effective_from": "2026-11-05",
                    "effective_to": None,
                }
            ],
            policy_version="demo-2026-11-05",
            as_of=datetime(2026, 11, 5).date(),
        ),
    )


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys |= _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _all_keys(item)
    return keys


def test_minor_amounts_format_exactly_without_floating_point() -> None:
    assert _format_money(13795, "SGD") == "SGD 137.95"
    assert _format_money(895, "SGD") == "SGD 8.95"
    assert _format_money(0, "SGD") == "SGD 0.00"
    assert _format_money(100, "AUD") == "AUD 1.00"
    assert _format_money(999999999999, "USD") == "USD 9999999999.99"


def test_order_amounts_are_server_display_strings() -> None:
    amounts = _order_amounts(_order())

    assert amounts == {
        "currency": "SGD",
        "items": [
            {
                "order_item_id": "oit_000000000042",
                "sku": "SYNTH-MATTRESS-008",
                "quantity": 1,
                "unit_price_display": "SGD 129.00",
                "line_total_display": "SGD 129.00",
            }
        ],
        "subtotal_display": "SGD 129.00",
        "shipping_display": "SGD 8.95",
        "tax_display": "SGD 0.00",
        "discount_display": "SGD 0.00",
        "total_display": "SGD 137.95",
    }


def test_suggestion_payload_excludes_minor_units_and_carries_exact_total() -> None:
    messages = _suggestion_messages(_context())  # type: ignore[arg-type]
    payload = json.loads(messages[1]["content"])

    assert not {key for key in _all_keys(payload) if key.endswith("_minor")}
    assert payload["order_amounts"]["total_display"] == "SGD 137.95"
    assert payload["order_amounts"]["items"][0]["unit_price_display"] == "SGD 129.00"
    assert "SGD 137.95" in messages[1]["content"]
    assert "order_amounts" in messages[0]["content"]
    assert "never convert minor units" in messages[0]["content"]


@pytest.mark.parametrize(
    "internal_summary",
    [
        "订单总额为 SGD 13795。",
        "Total: S$13,795.",
        "13795 SGD is the total.",
        "AUD 12900 unit price.",
    ],
)
def test_raw_minor_units_with_currency_fail_closed(internal_summary: str) -> None:
    with pytest.raises(LLMOutputError):
        _parse_suggestion_output(
            _suggestion_payload(internal_summary=internal_summary),
            {"faults-missing_abc12345_02"},
            order=_order(),
        )


def test_raw_minor_units_in_consumer_reply_fail_closed() -> None:
    with pytest.raises(LLMOutputError):
        _parse_suggestion_output(
            _suggestion_payload(consumer_reply="Your SGD 13795 refund is ready."),
            {"faults-missing_abc12345_02"},
            order=_order(),
        )


def test_internal_summary_must_include_the_server_total_display() -> None:
    with pytest.raises(LLMOutputError):
        _parse_suggestion_output(
            _suggestion_payload(internal_summary="SG damaged-item request for the mattress."),
            {"faults-missing_abc12345_02"},
            order=_order(),
        )


def test_exact_display_amounts_pass_the_amount_guard() -> None:
    candidate = _parse_suggestion_output(
        _suggestion_payload(
            internal_summary="订单总额 SGD 137.95，商品小计 SGD 129.00，运费 SGD 8.95。",
            consumer_reply="Total SGD 137.95; shipping was SGD 8.95.",
        ),
        {"faults-missing_abc12345_02"},
        order=_order(),
    )

    assert candidate.internal_summary.startswith("订单总额 SGD 137.95")
    assert candidate.consumer_reply == "Total SGD 137.95; shipping was SGD 8.95."
