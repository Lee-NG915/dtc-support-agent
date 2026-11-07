"""Tool registry construction."""

from app.retrieval.hybrid import HybridRetriever
from app.tools.aftersale_tools import (
    CancelOrderTool,
    CreateRefundRequestTool,
    CreateReturnRequestTool,
    GetRefundStatusTool,
)
from app.tools.base import Tool
from app.tools.knowledge_tools import (
    EscalateToHumanTool,
    GetCouponTool,
    GetWarrantyTool,
    SearchKnowledgeTool,
)
from app.tools.logistics_tools import TrackShipmentTool
from app.tools.order_tools import GetOrderItemsTool, GetOrderTool
from app.tools.product_tools import (
    CheckInventoryTool,
    GetProductDetailTool,
    RecommendProductsTool,
    SearchProductsTool,
)
from app.tools.registry import ToolRegistry


def build_tool_registry(
    retriever: HybridRetriever,
    *,
    include_demo_writes: bool = True,
) -> ToolRegistry:
    """Process-scoped registry. DB-backed tools resolve the request session lazily."""
    registry = ToolRegistry()
    # Explicit annotation: the concrete classes mix RepositoryBackedTool with Tool,
    # so an unannotated list literal widens to `object` and fails type checking.
    tools: list[Tool] = [
        GetOrderTool(),
        GetOrderItemsTool(),
        TrackShipmentTool(),
        GetRefundStatusTool(),
        SearchProductsTool(),
        GetProductDetailTool(),
        RecommendProductsTool(),
        GetCouponTool(),
        CheckInventoryTool(),
        GetWarrantyTool(),
        SearchKnowledgeTool(retriever),
        EscalateToHumanTool(),
    ]
    if include_demo_writes:
        # Upstream demo only. An enabled staff flow must not be able to reach
        # generic local refund, return or cancellation writes.
        tools.extend(
            [
                CreateRefundRequestTool(),
                CreateReturnRequestTool(),
                CancelOrderTool(),
            ]
        )
    for tool in tools:
        registry.register(tool)
    return registry
