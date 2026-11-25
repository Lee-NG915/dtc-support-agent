"""HTTP consumer connector: DTC BFF -> E-pig service consumer orders API."""

from __future__ import annotations

import httpx

from app.schemas.order import OrderItem, OrderStatus

_STATUS_MAP = {
    "paid": OrderStatus.PAID,
    "created": OrderStatus.CREATED,
    "shipped": OrderStatus.SHIPPED,
    "delivered": OrderStatus.DELIVERED,
    "cancelled": OrderStatus.CANCELLED,
    "refunding": OrderStatus.REFUNDING,
    "refunded": OrderStatus.REFUNDED,
}


class EpigConsumerConnector:
    def __init__(self, base_url: str, service_token: str, timeout: float = 8.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = service_token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token}",
            "x-after-sales-contract-version": "1",
            "accept": "application/json",
        }

    async def list_orders(self, github_id: str) -> list[dict[str, object]]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/api/au/after-sales/service/consumer/orders",
                params={"github_id": github_id},
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()
        return [_map_order(item) for item in payload.get("orders") or []]

    async def get_order(self, github_id: str, order_id: str) -> dict[str, object] | None:
        orders = await self.list_orders(github_id)
        return next((order for order in orders if order["order_id"] == order_id), None)

    async def get_shipment(self, github_id: str, order_id: str) -> dict[str, object] | None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/api/au/after-sales/service/orders/{order_id}/shipment",
                headers=self._headers(),
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()


def _map_order(raw: dict[str, object]) -> dict[str, object]:
    total = raw.get("total") or {}
    amount = total.get("amount") if isinstance(total, dict) else None
    raw_lines = raw.get("lines")
    if not isinstance(raw_lines, list):
        raw_lines = []
    items: list[OrderItem] = []
    for line in raw_lines:
        if not isinstance(line, dict):
            continue
        variant = line.get("variant") or {}
        product = line.get("product") or {}
        unit = line.get("price") or variant.get("price") or {}
        items.append(
            OrderItem(
                sku=str(variant.get("sku") or variant.get("id") or line.get("productId") or ""),
                name=str(product.get("name") or variant.get("label") or "item"),
                quantity=int(line.get("quantity") or 1),
                unit_price_cents=int(round(float(unit.get("amount") or 0) * 100)),
                customized=False,
            )
        )
    created = str(raw.get("createdAt") or "")
    address = raw.get("address")
    return {
        "order_id": str(raw.get("id") or ""),
        "status": _STATUS_MAP.get(str(raw.get("status") or "paid"), OrderStatus.PAID).value,
        "total_cents": int(round(float(amount or 0) * 100)),
        "created_at": created,
        "paid_at": created,
        "shipped_at": None,
        "delivered_at": None,
        "tracking_no": None,
        "receiver_city": address.get("city") if isinstance(address, dict) else None,
        "items": [item.model_dump() for item in items],
    }


def order_view_from_epig(order: dict[str, object]) -> dict[str, object]:
    return order
