"""Tests for the narrow eval tool trace.

These exercise the real registry dispatch path: tools are registered, wrapped
through ``ToolTrace.install`` and invoked through ``registry.get(...).run``.
The report test drives the offline e2e graph, which closes over the same eval
container registry, rather than calling the wrapper directly.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from pydantic import BaseModel, Field, field_validator

from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError, ToolExecutionError
from app.providers.fake import FakeLLMProvider
from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext
from app.tools.factory import build_tool_registry
from app.tools.registry import ToolRegistry
from eval import runner
from eval.tool_trace import ToolTrace, compare_tool_expectation

CANARY = "synthetic-trace-canary-31c2"


class LookupInput(BaseModel):
    order_id: str = Field(pattern=r"^ORD[0-9]+$")
    query: str = Field(default="", max_length=500)


class LookupTool(Tool):
    name = "lookup_order"
    description = "Synthetic lookup used only by trace tests."
    risk = ToolRisk.READ
    input_model = LookupInput

    def __init__(self, *, explode: bool = False, fail: bool = False) -> None:
        self.explode = explode
        self.fail = fail
        self.calls = 0

    async def execute(self, parsed: LookupInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        if self.explode:
            raise RuntimeError(f"backend body {CANARY}")
        if self.fail:
            return ToolResult(name=self.name, success=False, error=f"failure {CANARY}")
        return ToolResult(
            name=self.name,
            success=True,
            output={"order_id": parsed.order_id, "owner": context.user_id},
        )


def _context() -> ToolContext:
    return ToolContext(
        user_id=f"{CANARY}-user",
        session_id=f"{CANARY}-session",
        trace_id=f"{CANARY}-trace",
        idempotency_key=f"{CANARY}-idem",
    )


def _registry_with(tool: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def _serialized(records: list[dict]) -> str:
    return json.dumps(records, ensure_ascii=False)


async def test_registry_dispatch_preserves_tool_contract_and_records_normal_call() -> None:
    tool = LookupTool()
    registry = _registry_with(tool)
    trace = ToolTrace(sanitize=lambda value: str(value))
    trace.install(registry)

    traced = registry.get("lookup_order")
    assert traced is not tool
    assert traced.name == tool.name
    assert traced.description == tool.description
    assert traced.risk == tool.risk
    assert traced.input_model is LookupInput
    assert isinstance(traced.parse({"order_id": "ORD10001"}), LookupInput)
    assert traced.openai_schema() == tool.openai_schema()

    trace.begin_case("normal")
    result = await traced.run({"order_id": "ORD10001", "query": "where"}, _context())
    calls = trace.end_case()

    assert result.success is True
    assert result.output == {"order_id": "ORD10001", "owner": f"{CANARY}-user"}
    assert calls == [
        {
            "name": "lookup_order",
            "arguments": {"order_id": "ORD10001", "query": "where"},
            "status": "ok",
        }
    ]
    text = _serialized(calls)
    assert CANARY not in text
    assert "owner" not in text

    trace.begin_case("bounded")
    await traced.run({"order_id": "ORD10001", "query": "x" * 400}, _context())
    bounded = trace.end_case()
    assert len(bounded[0]["arguments"]["query"]) == 200
    assert bounded[0]["params_truncated"] is True

    trace.uninstall()
    assert registry.get("lookup_order") is tool
    assert trace.installed is False
    assert await registry.get("lookup_order").run({"order_id": "ORD1"}, _context())


async def test_invalid_arguments_are_classified_without_echoing_raw_values() -> None:
    tool = LookupTool()
    registry = _registry_with(tool)
    trace = ToolTrace(sanitize=lambda value: str(value))
    trace.install(registry)
    raw = f"{CANARY}-not-an-order-id"

    trace.begin_case("invalid")
    with pytest.raises(InvalidRequestError):
        await registry.get("lookup_order").run({"order_id": raw}, _context())
    calls = trace.end_case()

    assert calls == [{"name": "lookup_order", "arguments": None, "status": "invalid_input"}]
    assert raw not in _serialized(calls)
    assert CANARY not in _serialized(calls)
    assert tool.calls == 0


async def test_tool_exception_and_business_failure_do_not_leak_result_or_error_text() -> None:
    exploding = LookupTool(explode=True)
    registry = _registry_with(exploding)
    trace = ToolTrace(sanitize=lambda value: str(value))
    trace.install(registry)

    trace.begin_case("explode")
    with pytest.raises(ToolExecutionError) as excinfo:
        await registry.get("lookup_order").run({"order_id": "ORD10002"}, _context())
    assert str(excinfo.value) == "lookup_order failed"
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    calls = trace.end_case()
    assert calls == [
        {
            "name": "lookup_order",
            "arguments": {"order_id": "ORD10002", "query": ""},
            "status": "tool_error",
        }
    ]
    assert CANARY not in _serialized(calls)

    failing = LookupTool(fail=True)
    failure_registry = _registry_with(failing)
    failure_trace = ToolTrace(sanitize=lambda value: str(value))
    failure_trace.install(failure_registry)
    failure_trace.begin_case("failure")
    result = await failure_registry.get("lookup_order").run({"order_id": "ORD10003"}, _context())
    failure_calls = failure_trace.end_case()

    assert result.success is False
    assert failure_calls[0]["status"] == "tool_failure"
    assert CANARY not in _serialized(failure_calls)


async def test_wrapper_parses_once_and_executes_the_same_parsed_object() -> None:
    counts = {"validator": 0, "factory": 0, "execute": 0, "parsed": []}

    def _default() -> str:
        counts["factory"] += 1
        return "default-marker"

    class CountingInput(BaseModel):
        order_id: str

        marker: str = Field(default_factory=_default)

        @field_validator("order_id")
        @classmethod
        def _validate_order_id(cls, value: str) -> str:
            counts["validator"] += 1
            return value

    class CountingTool(Tool):
        name = "counting_tool"
        description = "Counts parse and execute calls."
        risk = ToolRisk.READ
        input_model = CountingInput

        async def execute(self, parsed: CountingInput, context: ToolContext) -> ToolResult:
            counts["execute"] += 1
            counts["parsed"].append(parsed)
            return ToolResult(
                name=self.name,
                success=True,
                output={"marker": parsed.marker},
            )

    registry = _registry_with(CountingTool())
    trace = ToolTrace()
    trace.install(registry)

    trace.begin_case("once")
    result = await registry.get("counting_tool").run({"order_id": "ORD10001"}, _context())
    calls = trace.end_case()

    assert result.output == {"marker": "default-marker"}
    assert counts["validator"] == 1
    assert counts["factory"] == 1
    assert counts["execute"] == 1
    assert len(counts["parsed"]) == 1
    assert calls == [
        {
            "name": "counting_tool",
            "arguments": {"order_id": "ORD10001", "marker": "default-marker"},
            "status": "ok",
        }
    ]


async def test_validation_failure_keeps_base_exception_and_never_executes() -> None:
    counts = {"validator": 0, "execute": 0}

    class StrictInput(BaseModel):
        order_id: str

        @field_validator("order_id")
        @classmethod
        def _validate_order_id(cls, value: str) -> str:
            counts["validator"] += 1
            if not value.startswith("ORD"):
                raise ValueError("bad order")
            return value

    class StrictTool(Tool):
        name = "strict_tool"
        description = "Strict validator."
        risk = ToolRisk.READ
        input_model = StrictInput

        async def execute(self, parsed: StrictInput, context: ToolContext) -> ToolResult:
            counts["execute"] += 1
            return ToolResult(name=self.name, success=True, output={})

    registry = _registry_with(StrictTool())
    trace = ToolTrace()
    trace.install(registry)

    trace.begin_case("invalid")
    with pytest.raises(InvalidRequestError) as excinfo:
        await registry.get("strict_tool").run({"order_id": "BAD"}, _context())
    calls = trace.end_case()

    assert excinfo.value.error_code.value == "INVALID_REQUEST"
    assert str(excinfo.value).startswith("invalid arguments for strict_tool:")
    assert counts == {"validator": 1, "execute": 0}
    assert calls == [{"name": "strict_tool", "arguments": None, "status": "invalid_input"}]


async def test_custom_tool_run_is_refused_instead_of_silently_reimplemented() -> None:
    class CustomRunTool(LookupTool):
        async def run(self, arguments: dict, context: ToolContext) -> ToolResult:
            return ToolResult(name=self.name, success=True, output={"custom": True})

    custom = CustomRunTool()
    registry = _registry_with(custom)
    trace = ToolTrace()

    with pytest.raises(RuntimeError, match="custom Tool.run"):
        trace.install(registry)
    assert registry.get("lookup_order") is custom
    assert trace.installed is False

    built = build_tool_registry(None)  # type: ignore[arg-type]
    assert all(type(tool).run is Tool.run for tool in built.all())
    trace.install(built)
    trace.uninstall()


def test_truncated_arguments_are_unscorable_never_exact_matches() -> None:
    trace = ToolTrace()
    prefix = "x" * 250
    bounded_actual, actual_truncated, actual_sanitized = trace.bound_params(
        {"query": prefix + "ACTUAL"}
    )
    assert actual_truncated is True
    assert actual_sanitized is False
    actual = [
        {
            "name": "lookup_order",
            "arguments": bounded_actual,
            "status": "ok",
            "params_truncated": True,
        }
    ]

    # RED before the fix: both sides bounded to the same 200-char prefix and
    # compared equal even though the real values differed after the cut.
    result = compare_tool_expectation(
        [{"name": "lookup_order", "arguments": {"query": prefix + "DIFFERENT"}}],
        actual,
        trace,
    )

    assert result["matched"] is False
    assert result["reason"] == "arguments_unscorable_truncated"
    assert result["expected"][0]["params_truncated"] is True


async def test_sanitized_values_are_unscorable_never_exact_matches() -> None:
    tool = LookupTool()
    registry = _registry_with(tool)
    trace = ToolTrace(sanitize=runner._redact)
    trace.install(registry)

    trace.begin_case("sanitized-values")
    await registry.get("lookup_order").run(
        {"order_id": "ORD10001", "query": "https://first.invalid/a"}, _context()
    )
    calls = trace.end_case()

    assert calls[0]["params_sanitized"] is True
    assert calls[0]["arguments"]["query"] == "[REDACTED_URL]"
    assert "first.invalid" not in _serialized(calls)

    # RED before the fix: both values redacted to the same marker and matched.
    result = compare_tool_expectation(
        [
            {
                "name": "lookup_order",
                "arguments": {
                    "order_id": "ORD10001",
                    "query": "https://other.invalid/b",
                },
            }
        ],
        calls,
        trace,
    )

    assert result["matched"] is False
    assert result["reason"] == "arguments_unscorable_sanitized"
    assert result["expected"][0]["params_sanitized"] is True
    assert "other.invalid" not in json.dumps(result, ensure_ascii=False)


def test_sanitized_keys_are_unscorable_never_exact_matches() -> None:
    trace = ToolTrace(sanitize=runner._redact)
    bounded_actual, truncated, sanitized = trace.bound_params({"https://first.invalid/a": "same"})
    assert truncated is False
    assert sanitized is True
    actual = [
        {
            "name": "lookup_order",
            "arguments": bounded_actual,
            "status": "ok",
            "params_sanitized": True,
        }
    ]

    result = compare_tool_expectation(
        [
            {
                "name": "lookup_order",
                "arguments": {"https://other.invalid/b": "same"},
            }
        ],
        actual,
        trace,
    )

    assert result["matched"] is False
    assert result["reason"] == "arguments_unscorable_sanitized"
    assert "first.invalid" not in json.dumps(actual, ensure_ascii=False)
    assert "other.invalid" not in json.dumps(result, ensure_ascii=False)


def test_selection_measurement_does_not_require_correctness() -> None:
    flags = runner._selection_quality_flags

    # Fake/offline metadata never grants positive real-model measurement.
    assert flags(
        real=False,
        quality_measured=True,
        agent_tool_total=2,
        positive_scorable_cases_with_calls=1,
        inhibition_scorable_cases=0,
    ) == (False, False)

    # Empty expectations and zero calls are inhibition, not positive selection.
    assert flags(
        real=True,
        quality_measured=True,
        agent_tool_total=0,
        positive_scorable_cases_with_calls=0,
        inhibition_scorable_cases=1,
    ) == (False, True)

    # An actually evaluated scorable positive expectation is measured even
    # when all predictions are wrong; correctness is only the numerator.
    assert flags(
        real=True,
        quality_measured=True,
        agent_tool_total=1,
        positive_scorable_cases_with_calls=1,
        inhibition_scorable_cases=0,
    ) == (True, False)

    # No actual calls means no positive selection measurement.
    assert flags(
        real=True,
        quality_measured=True,
        agent_tool_total=0,
        positive_scorable_cases_with_calls=0,
        inhibition_scorable_cases=0,
    ) == (False, False)

    # Incomplete real runs never establish either claim.
    assert flags(
        real=True,
        quality_measured=False,
        agent_tool_total=1,
        positive_scorable_cases_with_calls=1,
        inhibition_scorable_cases=1,
    ) == (False, False)


async def test_fake_real_mode_zero_call_inhibition_is_not_positive_selection() -> None:
    # Simulate a complete "real" quality scope with an injected fake provider:
    # no provider/network call, but the metadata path is the real-mode one.
    fake = runner.fake_model_run(FakeLLMProvider())
    run = dataclasses.replace(fake, mode="real", provider="fake", model="fake-injected")

    result = await runner.eval_e2e(
        model_run=run,
        rows=[
            {
                "id": "zero-call",
                "query": "你好",
                "user_id": "u_demo",
                "expect_tools": [],
            }
        ],
    )

    expectations = result["agent_tool_expectations"]
    assert result["quality_measured"] is True
    assert result["agent_tool_calls"]["total"] == 0
    assert expectations["positive_cases"] == 0
    assert expectations["positive_cases_scorable"] == 0
    assert expectations["positive_cases_scorable_with_calls"] == 0
    assert expectations["inhibition_cases"] == 1
    assert expectations["inhibition_cases_scorable"] == 1
    assert expectations["inhibition_cases_matched"] == 1
    assert expectations["selection_quality_measured"] is False
    assert expectations["inhibition_measured"] is True
    assert "real_tool_call_accuracy" in result["not_measured"]


async def test_fake_real_mode_all_wrong_positive_is_measured_with_zero_score() -> None:
    fake = runner.fake_model_run(FakeLLMProvider())
    run = dataclasses.replace(fake, mode="real", provider="fake", model="fake-injected")

    result = await runner.eval_e2e(
        model_run=run,
        rows=[
            {
                "id": "wrong-positive",
                "query": "ORD10002的物流到哪了",
                "user_id": "u_demo",
                "expect_tools": [{"name": "cancel_order", "arguments": {}}],
            }
        ],
    )

    expectations = result["agent_tool_expectations"]
    assert result["quality_measured"] is True
    assert result["agent_tool_calls"]["total"] > 0
    assert expectations["cases_scorable"] == 1
    assert expectations["cases_unscorable"] == 0
    assert expectations["positive_cases"] == 1
    assert expectations["positive_cases_scorable"] == 1
    assert expectations["positive_cases_scorable_with_calls"] == 1
    assert expectations["positive_cases_matched"] == 0
    assert expectations["cases_matched"] == 0
    assert expectations["accuracy"] == 0.0
    assert expectations["failures"] == ["wrong-positive"]
    assert expectations["selection_quality_measured"] is True
    assert expectations["inhibition_measured"] is False
    assert "real_tool_call_accuracy" not in result["not_measured"]


async def test_cases_are_isolated_and_recording_stops_outside_a_case() -> None:
    tool = LookupTool()
    registry = _registry_with(tool)
    trace = ToolTrace()
    trace.install(registry)
    traced = registry.get("lookup_order")

    trace.begin_case("first")
    await traced.run({"order_id": "ORD10001"}, _context())
    first = trace.end_case()

    await traced.run({"order_id": "ORD10002"}, _context())
    trace.begin_case("second")
    second = trace.end_case()

    trace.begin_case("third")
    await traced.run({"order_id": "ORD10003"}, _context())
    third = trace.end_case()

    assert [call["arguments"]["order_id"] for call in first] == ["ORD10001"]
    assert second == []
    assert [call["arguments"]["order_id"] for call in third] == ["ORD10003"]

    with pytest.raises(RuntimeError, match="already installed"):
        trace.install(registry)
    second_trace = ToolTrace()
    with pytest.raises(RuntimeError, match="already contains"):
        second_trace.install(registry)
    trace.uninstall()


def test_expected_tools_require_exact_names_arguments_counts_and_known_fields() -> None:
    trace = ToolTrace()
    actual = [
        {
            "name": "lookup_order",
            "arguments": {"order_id": "ORD10001", "query": ""},
            "status": "ok",
        },
        {
            "name": "lookup_order",
            "arguments": {"order_id": "ORD10002", "query": ""},
            "status": "ok",
        },
    ]
    matching = [
        {"name": "lookup_order", "arguments": {"order_id": "ORD10001", "query": ""}},
        {"name": "lookup_order", "arguments": {"order_id": "ORD10002", "query": ""}, "count": 1},
    ]
    assert compare_tool_expectation(matching, actual, trace)["matched"] is True
    assert compare_tool_expectation([], [], trace)["matched"] is True
    assert compare_tool_expectation([], actual, trace)["matched"] is False
    assert (
        compare_tool_expectation(
            [{"name": "lookup_order", "arguments": {"order_id": "ORD10001", "query": ""}}],
            actual,
            trace,
        )["matched"]
        is False
    )
    assert (
        compare_tool_expectation(
            [
                {
                    "name": "lookup_order",
                    "arguments": {"order_id": "ORD10001", "query": ""},
                    "count": 2,
                }
            ],
            actual,
            trace,
        )["matched"]
        is False
    )
    with pytest.raises(ValueError, match="unsupported"):
        compare_tool_expectation(
            [{"name": "lookup_order", "when": "sometimes"}],
            actual,
            trace,
        )
    with pytest.raises(ValueError, match="positive integer"):
        compare_tool_expectation(
            [{"name": "lookup_order", "arguments": {}, "count": 0}],
            [],
            trace,
        )


async def test_e2e_report_carries_actual_per_case_calls_and_expectations(monkeypatch) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    disposed: list[object] = []
    real_dispose = AsyncEngine.dispose

    async def tracked_dispose(self, close: bool = True):
        disposed.append(self)
        return await real_dispose(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)
    rows = [
        {
            "id": "trace-agent",
            "query": "ORD10002的物流到哪了",
            "user_id": "u_demo",
            "expect_tools": [
                {"name": "get_order", "arguments": {"order_id": "ORD10002"}},
                {"name": "track_shipment", "arguments": {"order_id": "ORD10002"}},
            ],
        },
        {
            "id": "trace-direct",
            "query": "你好",
            "user_id": "u_demo",
            "expect_tools": [],
            "ignored_instruction_field": "must not become a tool expectation",
        },
    ]

    result = await runner.eval_e2e(rows=rows)

    assert disposed, "eval container engine must still be disposed"
    assert result["agent_tool_trace_measured"] is True
    assert result["agent_tool_calls"]["total"] == 2
    assert result["agent_tool_calls"]["by_name"] == {"get_order": 1, "track_shipment": 1}
    assert result["agent_tool_calls"]["by_status"] == {"ok": 2}
    assert result["agent_tool_calls"]["cases_with_calls"] == 1
    assert result["agent_tool_expectations"] == {
        "cases_with_expectations": 2,
        "cases_scorable": 2,
        "cases_unscorable": 0,
        "unscorable": [],
        "cases_matched": 2,
        "accuracy": 1.0,
        "failures": [],
        "positive_cases": 1,
        "positive_cases_scorable": 1,
        "positive_cases_scorable_with_calls": 1,
        "positive_cases_matched": 1,
        "inhibition_cases": 1,
        "inhibition_cases_scorable": 1,
        "inhibition_cases_matched": 1,
        "selection_quality_measured": False,
        "inhibition_measured": False,
        "note": result["agent_tool_expectations"]["note"],
    }
    assert result["connector_calls"]["measured"] is False
    assert result["connector_calls"]["counted_as_agent_tool_calls"] is False
    assert result["real_tool_trace_measured"] is False
    assert result["legacy_answer_keyword_proxy"] is not None
    assert "agent_tool_calls" in result["cases"][0]
    assert "agent_tool_calls" in result["cases"][1]
    assert "lookup_order" not in json.dumps(result["agent_tool_calls"])
    assert CANARY not in json.dumps(result, ensure_ascii=False)
