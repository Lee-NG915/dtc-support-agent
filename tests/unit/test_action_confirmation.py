from datetime import date

import pytest

from app.agents.nodes.planner import Planner, planner_node
from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError
from app.providers.fake import OfflineLLMProvider
from app.schemas.chat import ActionProposal, ChatRequest, ChatResponse
from app.schemas.retrieval import PolicyScope
from app.services.chat_service import ChatService, _request_policy_scope
from app.tools.aftersale_tools import CancelOrderTool
from app.tools.order_tools import GetOrderTool
from app.tools.registry import ToolRegistry


def _action() -> ActionProposal:
    return ActionProposal(
        action_id="act_test",
        tool_name="cancel_order",
        arguments={"order_id": "ORD10003"},
        risk=ToolRisk.DESTRUCTIVE,
        summary="确认后将取消订单 ORD10003。",
    )


@pytest.mark.asyncio
async def test_cancellation_needs_explicit_request_and_confirmation() -> None:
    registry = ToolRegistry()
    registry.register(GetOrderTool())
    registry.register(CancelOrderTool())
    planner = Planner(OfflineLLMProvider(), registry)

    proposal = await planner_node({"user_query": "取消订单 ORD10003"}, planner)
    assert proposal["pending_action"].tool_name == "cancel_order"
    assert "cancel_order" not in proposal["pending_action"].summary

    question = await planner_node({"user_query": "取消订单有什么规则？"}, planner)
    assert question["pending_action"] is None

    confirmed = await planner_node(
        {"user_query": "确认执行", "confirmed_action": _action()}, planner
    )
    assert len(confirmed["plan"].steps) == 1
    assert confirmed["plan"].steps[0].arguments == {"order_id": "ORD10003"}


def test_confirmation_is_bound_to_session_and_user() -> None:
    service = ChatService.__new__(ChatService)
    service._pending_actions = {}
    request = ChatRequest(message="取消订单 ORD10003", user_id="u_demo")
    response = ChatResponse(
        answer="请确认",
        session_id="sess_demo",
        trace_id="trace_demo",
        pending_action=_action(),
    )
    service._remember_pending(request, response)

    confirmed = ChatRequest(
        message="确认执行",
        user_id="u_demo",
        session_id="sess_demo",
        confirm_action_id="act_test",
    )
    assert service._resolve_confirmation(confirmed, "sess_demo", PolicyScope()) == _action()

    forged = confirmed.model_copy(update={"user_id": "someone_else"})
    with pytest.raises(InvalidRequestError):
        service._resolve_confirmation(forged, "sess_demo", PolicyScope())
    with pytest.raises(InvalidRequestError):
        service._resolve_confirmation(confirmed, "another_session", PolicyScope())


def test_epig_default_policy_date_uses_the_local_calendar_date() -> None:
    request = ChatRequest(
        message="家具保修多少年",
        brand="epig",
        market="au",
        product_category="furniture",
    )
    assert _request_policy_scope(request).as_of == date.today()


@pytest.mark.asyncio
async def test_stateless_policy_turn_is_not_persisted_as_generic_history() -> None:
    service = ChatService.__new__(ChatService)
    request = ChatRequest(
        message="家具保修多少年",
        user_id="u_demo",
        session_id="sess_demo",
        brand="epig",
        market="au",
        product_category="furniture",
    )
    response = ChatResponse(
        answer="演示回答",
        session_id="sess_demo",
        trace_id="trace_demo",
    )

    # No repository is available: returning without touching it proves that the
    # policy question and answer cannot leak into a later generic session history.
    await service._persist(None, request, response)  # type: ignore[arg-type]


def test_pending_action_preserves_scope_from_the_originating_turn() -> None:
    service = ChatService.__new__(ChatService)
    service._pending_actions = {}
    request = ChatRequest(
        message="取消订单 ORD10003",
        user_id="u_demo",
        session_id="sess_demo",
        brand="epig",
        market="au",
        product_category="furniture",
    )
    original_scope = PolicyScope(
        brand="epig", market="au", as_of=date(2026, 11, 4), product_category="furniture"
    )
    response = ChatResponse(
        answer="请确认",
        session_id="sess_demo",
        trace_id="trace_demo",
        pending_action=_action(),
    )

    service._remember_pending(request, response, policy_scope=original_scope)

    confirmed = request.model_copy(update={"confirm_action_id": "act_test"})
    assert service._resolve_confirmation(confirmed, "sess_demo", original_scope) == _action()
    with pytest.raises(InvalidRequestError, match="政策范围"):
        service._resolve_confirmation(confirmed, "sess_demo", _request_policy_scope(request))


def test_confirmation_cannot_cross_brand_market_or_product_scope() -> None:
    service = ChatService.__new__(ChatService)
    service._pending_actions = {}
    request = ChatRequest(message="取消订单 ORD10003", user_id="u_demo")
    response = ChatResponse(
        answer="请确认",
        session_id="sess_demo",
        trace_id="trace_demo",
        pending_action=_action(),
    )
    service._remember_pending(request, response)

    epig = ChatRequest(
        message="确认执行",
        user_id="u_demo",
        session_id="sess_demo",
        confirm_action_id="act_test",
        brand="epig",
        market="au",
        policy_as_of="2026-11-05",
        product_category="furniture",
    )
    with pytest.raises(InvalidRequestError, match="政策范围"):
        service._resolve_confirmation(epig, "sess_demo", _request_policy_scope(epig))

    service._remember_pending(epig, response)
    assert (
        service._resolve_confirmation(epig, "sess_demo", _request_policy_scope(epig)) == _action()
    )

    different_product = epig.model_copy(update={"product_category": "mattress"})
    with pytest.raises(InvalidRequestError, match="政策范围"):
        service._resolve_confirmation(
            different_product, "sess_demo", _request_policy_scope(different_product)
        )

    different_market = epig.model_copy(update={"market": "us"})
    with pytest.raises(InvalidRequestError, match="政策范围"):
        service._resolve_confirmation(
            different_market, "sess_demo", _request_policy_scope(different_market)
        )
