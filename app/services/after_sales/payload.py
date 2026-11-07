"""Canonical JSON and digests for approval/operation payloads.

The same bytes are hashed at submission and reused for the operation payload;
nothing in this module accepts request-supplied policy versions or categories.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def approval_snapshot_digest(snapshot: dict[str, Any]) -> str:
    return sha256_json(snapshot)


def operation_payload(snapshot: dict[str, Any], *, approval_id: str) -> dict[str, Any]:
    """Build the fixed wire payload from the approved snapshot only."""
    draft = snapshot["draft"]
    return {
        "request_id": snapshot["case"]["request_id"],
        "action": draft["action"],
        "reply": draft["consumer_reply"],
        "draft_version": str(draft["version"]),
        "approval_id": approval_id,
        "policy_version": snapshot["evidence"]["policy_version"],
        "order_version": snapshot["order"]["order_version"],
    }
