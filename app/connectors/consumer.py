"""Consumer self-service data access with server-enforced ownership.

The connector protocol mirrors the stage-2 E-pig service calls. The Fake
implementation maps explicitly configured demo GitHub ids onto the synthetic
u_demo orders; unmapped identities see empty data, never someone else's.
"""

from __future__ import annotations

from typing import Protocol

from app.schemas.order import Order


class ConsumerConnector(Protocol):
    async def list_orders(self, github_id: str) -> list[Order]: ...

    async def get_order(self, github_id: str, order_id: str) -> Order | None: ...

    async def list_after_sales(self, github_id: str) -> list[dict[str, object]]: ...


def demo_user_for(github_id: str, demo_ids: frozenset[str]) -> str | None:
    """Return the synthetic owner visible to this GitHub id, if any."""
    return "u_demo" if github_id in demo_ids else None


def shipment_events_for(order: Order) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    if order.paid_at:
        events.append({"status": "warehouse", "at": order.paid_at.isoformat()})
    if order.shipped_at:
        events.append(
            {
                "status": "in_transit",
                "at": order.shipped_at.isoformat(),
                "tracking_no": order.tracking_no or "",
            }
        )
    if order.delivered_at:
        events.append({"status": "delivered", "at": order.delivered_at.isoformat()})
    return events
