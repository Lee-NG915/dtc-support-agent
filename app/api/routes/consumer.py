"""Consumer self-service endpoints: own orders, shipments and after-sales."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import ConsumerActor, get_container, get_required_consumer
from app.connectors.consumer import shipment_events_for
from app.core.exceptions import InvalidRequestError
from app.core.request_scope import request_session
from app.repositories.order_repository import OrderRepository
from app.schemas.order import Order
from app.services.container import AppContainer

router = APIRouter(prefix="/consumer", tags=["consumer"])


def _order_view(order: Order) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "status": order.status.value,
        "total_cents": order.total_cents,
        "created_at": order.created_at.isoformat(),
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "tracking_no": order.tracking_no,
        "receiver_city": order.receiver_city,
        "items": [item.model_dump() for item in order.items],
    }


@router.get("/orders")
async def list_orders(
    actor: ConsumerActor = Depends(get_required_consumer),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if actor.demo_user_id is None:
        return {"items": [], "total": 0}
    async with request_session(container.session_factory) as session:
        orders = await OrderRepository(session).list_orders_for_user(actor.demo_user_id, limit=50)
    return {"items": [_order_view(order) for order in orders], "total": len(orders)}


async def _visible_order(actor: ConsumerActor, container: AppContainer, order_id: str) -> Order:
    if actor.demo_user_id is None:
        raise InvalidRequestError("order not found")
    async with request_session(container.session_factory) as session:
        order = await OrderRepository(session).get_visible_order(order_id, actor.demo_user_id)
    return order


@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    actor: ConsumerActor = Depends(get_required_consumer),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    order = await _visible_order(actor, container, order_id)
    return _order_view(order)


@router.get("/orders/{order_id}/shipment")
async def get_shipment(
    order_id: str,
    actor: ConsumerActor = Depends(get_required_consumer),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    order = await _visible_order(actor, container, order_id)
    return {
        "order_id": order.order_id,
        "tracking_no": order.tracking_no,
        "receiver_city": order.receiver_city,
        "events": shipment_events_for(order),
    }


@router.get("/after-sales")
async def list_after_sales(
    actor: ConsumerActor = Depends(get_required_consumer),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if actor.demo_user_id is None:
        return {"items": [], "total": 0}
    async with request_session(container.session_factory) as session:
        repository = OrderRepository(session)
        orders = await repository.list_orders_for_user(actor.demo_user_id, limit=50)
        items: list[dict[str, object]] = []
        for order in orders:
            for refund in await repository.get_refunds(order.order_id):
                items.append(
                    {
                        "refund_id": refund.refund_id,
                        "order_id": refund.order_id,
                        "status": refund.status,
                        "amount_cents": refund.amount_cents,
                        "reason": refund.reason,
                        "created_at": refund.created_at.isoformat(),
                    }
                )
    return {"items": items, "total": len(items)}
