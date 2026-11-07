"""Narrow, read-only checks shared by the worker and the staff workbench.

The immutable approval snapshot and the persisted operation payload are the
only authority for a ticket's local approval identity. Neither function reads
or writes workflow state; callers decide how a mismatch should be handled.
"""

from __future__ import annotations

from typing import Any, cast

from app.connectors.models import ServiceTicket


def ticket_problems(
    ticket: ServiceTicket,
    snapshot: dict[str, Any],
    payload: dict[str, Any],
    approval_id: str,
) -> list[str]:
    """Compare a remote ticket with the original approval and operation payload."""
    try:
        case_snapshot = cast(dict[str, Any], snapshot["case"])
        order_snapshot = cast(dict[str, Any], snapshot["order"])
    except (KeyError, TypeError):
        return ["snapshot_invalid"]
    problems: list[str] = []
    # Market and order identity come from the immutable approval snapshot;
    # all wire fields come from the persisted operation payload. The mutable
    # case row is never allowed to substitute for either.
    if case_snapshot.get("order_id") != order_snapshot.get("order_id"):
        problems.append("snapshot_identity_mismatch")
    if ticket.market != case_snapshot.get("market"):
        problems.append("ticket_market_mismatch")
    if ticket.order_id != order_snapshot.get("order_id"):
        problems.append("ticket_order_mismatch")
    if ticket.request_id != payload.get("request_id"):
        problems.append("ticket_request_mismatch")
    if ticket.action != payload.get("action"):
        problems.append("ticket_action_mismatch")
    if ticket.reply != payload.get("reply"):
        problems.append("ticket_reply_mismatch")
    if ticket.draft_version != str(payload.get("draft_version")):
        problems.append("ticket_draft_version_mismatch")
    if ticket.approval_id != approval_id or ticket.approval_id != payload.get("approval_id"):
        problems.append("ticket_approval_mismatch")
    if ticket.policy_version != payload.get("policy_version"):
        problems.append("ticket_policy_version_mismatch")
    if ticket.order_version != payload.get("order_version"):
        problems.append("ticket_order_version_mismatch")
    receipt = ticket.receipt
    if (
        receipt.ticket_id != ticket.ticket_id
        or receipt.request_id != ticket.request_id
        or receipt.action != ticket.action
        or receipt.request_status != "ticket_created"
    ):
        problems.append("ticket_receipt_mismatch")
    return list(dict.fromkeys(problems))


def ticket_identity_problems(
    ticket: ServiceTicket,
    *,
    market: str,
    order_id: str,
    request_id: str,
) -> list[str]:
    """Check the external object belongs to this case before exposing it."""
    problems: list[str] = []
    if ticket.market != market:
        problems.append("ticket_market_mismatch")
    if ticket.order_id != order_id:
        problems.append("ticket_order_mismatch")
    if ticket.request_id != request_id:
        problems.append("ticket_request_mismatch")
    receipt = ticket.receipt
    if (
        receipt.ticket_id != ticket.ticket_id
        or receipt.request_id != ticket.request_id
        or receipt.action != ticket.action
        or receipt.request_status != "ticket_created"
    ):
        problems.append("ticket_receipt_mismatch")
    return list(dict.fromkeys(problems))
